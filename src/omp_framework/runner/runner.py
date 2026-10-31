"""AgentRunner: Loads `agents/omp/<agent>.md`, enforces prompt security guards, and runs omp RPC sessions.

Security & Governance Guards:
- Rejects prompts starting with `/` (after stripping whitespace/BOM/zero-width chars) so users cannot
  trigger built-in omp slash commands like `/export` that bypass the LLM and PolicyGate.
- Rejects prompts containing `@<path>` file mentions so users cannot trick omp into reading arbitrary
  local files into the session context (`user@example.com` or `node@2026` are allowed).
- Renders `agents/omp/agent/models.yml` into `var/omp-home/agent/models.yml` with `${OMP_LLM_BASE_URL}`
  substituted, keeping credentials and runtime state out of the git tree.
- Discovers agent definitions dynamically from `agents/omp/<agent>.md` (marker line `omp-agent: <id>` +
  YAML frontmatter `domain`, `label`, `tools`, `max_tool_calls`, `timeout_s` + `---` + system prompt).
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

import yaml

from omp_framework.contracts import AgentRunAnswer, ScopeContext
from omp_framework.control.gate import PolicyGate
from omp_framework.llm.backend import agent_marker
from omp_framework.rpc.process import (
    DEFAULT_OMP_EXE,
    OmpLaunch,
    OmpPaths,
    OmpSession,
    RunLimits,
    repo_root,
)
from omp_framework.tools.registry import ToolRegistry

__all__ = [
    "COMMAND_PROMPT_MESSAGE",
    "FILE_MENTION_PROMPT_MESSAGE",
    "AgentConfigError",
    "AgentProfile",
    "AgentRunner",
    "is_command_prompt",
    "is_file_mention_prompt",
    "load_agent_profile",
    "load_all_agent_profiles",
]

COMMAND_PROMPT_MESSAGE: Final = (
    "Prompts starting with '/' are rejected because omp interprets leading slash commands locally."
)
FILE_MENTION_PROMPT_MESSAGE: Final = (
    "Prompts containing '@<path>' file mentions are rejected to prevent local file exfiltration."
)
_ZERO_WIDTH: Final = "\ufeff\u200b\u200c\u200d\u2060"
_FILE_MENTION_RE: Final = re.compile(r"(?:^|[\s(\[{,;\"'])@[A-Za-z0-9_./~\\-]")


class AgentConfigError(ValueError):
    """Raised when an agent `.md` profile or `agents/omp/config.yml` is invalid."""


def is_command_prompt(prompt: str) -> bool:
    """Return True if stripping leading whitespace and zero-width characters leaves a leading `/`."""
    cleaned = prompt.lstrip().lstrip(_ZERO_WIDTH).lstrip()
    return cleaned.startswith("/")


def is_file_mention_prompt(prompt: str) -> bool:
    """Return True if prompt contains an omp `@<path>` mention (while allowing emails like `a@b.com`)."""
    return bool(_FILE_MENTION_RE.search(prompt))


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """Parsed agent specification from `agents/omp/<agent_id>.md`."""

    agent_id: str
    label: str
    domain: str
    description: str
    tools: tuple[str, ...]
    max_tool_calls: int
    timeout_s: float
    system_prompt: str
    source_path: Path


def load_agent_profile(path: Path) -> AgentProfile:
    """Parse an `agents/omp/<agent_id>.md` file into an `AgentProfile`."""
    if not path.is_file():
        raise AgentConfigError(f"Agent profile file does not exist: {path}")
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    if not lines:
        raise AgentConfigError(f"Agent profile {path} is empty")
    first = lines[0].strip()
    if not first.startswith("omp-agent:"):
        raise AgentConfigError(f"First line of {path} must be 'omp-agent: <agent_id>', got {first!r}")
    agent_id = first.split(":", 1)[1].strip()
    if not agent_id:
        raise AgentConfigError(f"Missing agent_id in first line of {path}")

    rest = "\n".join(lines[1:])
    if "\n---" not in f"\n{rest}":
        raise AgentConfigError(f"Agent profile {path} is missing '---' separator between YAML header and prompt body")
    yaml_part, body_part = rest.split("---", 1)
    meta = yaml.safe_load(yaml_part) or {}
    if not isinstance(meta, dict):
        raise AgentConfigError(f"YAML frontmatter in {path} must be a mapping")

    tools = meta.get("tools") or []
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise AgentConfigError(f"`tools` in {path} must be a list of tool names")

    max_calls = int(meta.get("max_tool_calls", 8))
    timeout_s = float(meta.get("timeout_s", 90.0))
    label = str(meta.get("label", agent_id))
    domain = str(meta.get("domain", "general"))
    description = str(meta.get("description", f"{label} ({domain})"))

    full_prompt = f"{agent_marker(agent_id)}\n\n{body_part.strip()}\n"
    return AgentProfile(
        agent_id=agent_id,
        label=label,
        domain=domain,
        description=description,
        tools=tuple(tools),
        max_tool_calls=max_calls,
        timeout_s=timeout_s,
        system_prompt=full_prompt,
        source_path=path.resolve(),
    )


def load_all_agent_profiles(agents_dir: Path | None = None) -> dict[str, AgentProfile]:
    directory = agents_dir if agents_dir is not None else (repo_root() / "agents" / "omp")
    profiles: dict[str, AgentProfile] = {}
    if not directory.is_dir():
        return profiles
    for md_file in sorted(directory.glob("*.md")):
        profile = load_agent_profile(md_file)
        profiles[profile.agent_id] = profile
    return profiles


class AgentRunner:
    """Orchestrates `omp` RPC runs with dynamic Host Tools, PolicyGate enforcement, and prompt guards."""

    def __init__(
        self,
        *,
        llm_base_url: str,
        tool_registry: ToolRegistry,
        gate: PolicyGate,
        agents_dir: Path | None = None,
        omp_Paths: OmpPaths | None = None,
        omp_command: Sequence[str] | None = None,
        mode_getter: Any = None,
    ) -> None:
        self.llm_base_url = llm_base_url.rstrip("/")
        self.tool_registry = tool_registry
        self.gate = gate
        self.agents_dir = agents_dir if agents_dir is not None else (repo_root() / "agents" / "omp")
        self.paths = omp_Paths if omp_Paths is not None else OmpPaths.under()
        self.omp_command: tuple[str, ...] = (
            tuple(omp_command) if omp_command is not None else (str(DEFAULT_OMP_EXE),)
        )
        self._mode_getter = mode_getter
        self.profiles: dict[str, AgentProfile] = load_all_agent_profiles(self.agents_dir)
        for aid, prof in self.profiles.items():
            self.gate.register_agent_tools(aid, prof.tools)
        self._prepare_runtime_files()

    @property
    def current_mode(self) -> str:
        env_model = os.environ.get("OMP_MODEL", "").strip()
        if env_model and not env_model.startswith("omp/"):
            return f"provider:{env_model}"
        if callable(self._mode_getter):
            return str(self._mode_getter())
        if isinstance(self._mode_getter, str) and self._mode_getter:
            return self._mode_getter
        return "offline"

    def _prepare_runtime_files(self) -> None:
        self.paths.prepare()
        models_tpl_path = self.agents_dir / "agent" / "models.yml"
        if models_tpl_path.is_file():
            raw = models_tpl_path.read_text(encoding="utf-8")
            rendered_lines: list[str] = []
            for line in raw.splitlines():
                if line.lstrip().startswith("#"):
                    rendered_lines.append(line)
                else:
                    rendered_lines.append(line.replace("${OMP_LLM_BASE_URL}", self.llm_base_url))
            target = self.paths.agent / "models.yml"
            target.write_text("\n".join(rendered_lines) + "\n", encoding="utf-8")

    def validate_prompt(self, prompt: str) -> None:
        if not prompt or not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        if is_command_prompt(prompt):
            raise ValueError(COMMAND_PROMPT_MESSAGE)
        if is_file_mention_prompt(prompt):
            raise ValueError(FILE_MENTION_PROMPT_MESSAGE)

    def run(
        self,
        agent_id: str,
        prompt: str,
        *,
        scope: ScopeContext | None = None,
        resume_session: Path | str | None = None,
    ) -> AgentRunAnswer:
        self.validate_prompt(prompt)
        profile = self.profiles.get(agent_id)
        if profile is None:
            raise KeyError(f"Unknown agent_id: {agent_id!r}. Available: {sorted(self.profiles)}")

        effective_scope = scope if scope is not None else ScopeContext(scope_id="default")
        tool_session = self.tool_registry.session(
            agent_id=profile.agent_id,
            agent_domain=profile.domain,
            allowed_tools=profile.tools,
            scope=effective_scope,
            gate=self.gate,
        )

        # Write rendered system prompt file into isolated omp home
        prompt_dir = self.paths.home / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompt_dir / f"{profile.agent_id}.md"
        prompt_file.write_text(profile.system_prompt, encoding="utf-8")

        overlay_file = self.agents_dir / "overlay.yml"
        model_name = os.environ.get("OMP_MODEL", "").strip() or "omp/omp-offline"
        agent_sessions_dir = self.paths.sessions / profile.agent_id if self.paths.sessions else None
        run_paths = replace(self.paths, sessions=agent_sessions_dir)
        resume_path = Path(resume_session) if resume_session is not None else None

        launch = OmpLaunch(
            paths=run_paths,
            command=self.omp_command,
            model=model_name,
            system_prompt=prompt_file,
            config=overlay_file if overlay_file.is_file() else None,
            resume_session=resume_path,
        )
        limits = RunLimits(
            max_tool_calls=profile.max_tool_calls,
            timeout_s=profile.timeout_s,
        )

        started = time.monotonic()
        session = OmpSession(launch, limits=limits, handler=tool_session.handle_rpc_call)
        rpc_res = session.run(prompt, tools=tool_session.declarations())

        elapsed = round(time.monotonic() - started, 4)
        run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"

        if rpc_res.status == "completed":
            status_val = "ok"
            answer_text = (rpc_res.assistant_text or "").strip() or "Completed."
        elif rpc_res.status == "aborted":
            status_val = "aborted"
            answer_text = f"Execution aborted ({rpc_res.reason}): {rpc_res.detail}"
        else:
            status_val = "error"
            answer_text = f"Execution error: {rpc_res.detail}"

        return AgentRunAnswer(
            run_id=run_id,
            agent_id=profile.agent_id,
            domain=profile.domain,
            scope_id=effective_scope.scope_id,
            as_of=effective_scope.as_of,
            mode=self.current_mode,
            status=status_val,
            answer=answer_text,
            session_id=rpc_res.session_id,
            session_file=rpc_res.session_file,
            tool_calls=list(tool_session.records),
            citations=list(tool_session.citations),
            abort_reason=rpc_res.reason,
            duration_s=elapsed,
        )
