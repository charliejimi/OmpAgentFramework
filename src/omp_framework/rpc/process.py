"""Sandboxed omp child-process manager with credential stripping, concurrency limiting, and RunLimits."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any, Final, Literal, TypeVar

from omp_framework.rpc.client import (
    HostToolExchange,
    HostToolHandler,
    PromptOutcome,
    ReadyInfo,
    RpcClient,
    RpcError,
    RpcEvent,
)

__all__ = [
    "ABORT_REASON_LABELS",
    "DEFAULT_EXTENSIONS_DIR",
    "DEFAULT_LIMITER",
    "DEFAULT_MODEL",
    "DEFAULT_OMP_EXE",
    "MAX_CONCURRENT_RUNS",
    "NO_PROXY_HOSTS",
    "OMP_RPC_ARGS",
    "OMP_SHA256",
    "OMP_VERSION",
    "PROVIDER_KEY_ENV_VARS",
    "REMOVED_ENV_SUBSTRINGS",
    "REMOVED_ENV_SUFFIXES",
    "REMOVED_ENV_VARS",
    "THINKING_LEVELS",
    "UNREACHABLE_BUILD_URL",
    "AbortReason",
    "ConcurrencyLimiter",
    "OmpAborted",
    "OmpConfigError",
    "OmpLaunch",
    "OmpPaths",
    "OmpRunResult",
    "OmpSession",
    "RunLimits",
    "RunStatus",
    "active_pids",
    "isolated_env",
    "repo_root",
    "var_dir",
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def var_dir() -> Path:
    env_var = os.environ.get("OMP_VAR_DIR", "").strip()
    if env_var:
        return Path(env_var).resolve()
    return repo_root() / "var"


DEFAULT_OMP_EXE: Final = repo_root() / "tools" / "omp" / "omp.exe"
OMP_VERSION: Final = "18.11.2"
OMP_SHA256: Final = "97f5c9d534141cd6c4fc8abe85818de550772ce397da08623b0aa25d9f944946"
DEFAULT_EXTENSIONS_DIR: Final = DEFAULT_OMP_EXE.parent / "extensions"

OMP_RPC_ARGS: Final[tuple[str, ...]] = (
    "--mode",
    "rpc",
    "--no-ui",
    "--no-tools",
    "--no-lsp",
    "--no-extensions",
    "--no-skills",
    "--no-rules",
    "--no-title",
    "--no-pty",
)
THINKING_LEVELS: Final[tuple[str, ...]] = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")
DEFAULT_MODEL: Final = "omp/omp-offline"
MAX_CONCURRENT_RUNS: Final = 2
UNREACHABLE_BUILD_URL: Final = "http://127.0.0.1:9"

REMOVED_ENV_VARS: Final = frozenset(
    {
        "OMP_PROFILE",
        "PI_PROFILE",
        "PI_CONFIG_DIR",
        "PI_CONFIG_FILES",
        "PI_CODING_AGENT_SESSION_DIR",
        "OMP_AUTH_BROKER_URL",
    }
)
REMOVED_ENV_SUFFIXES: Final = ("_API_KEY", "_TOKEN")
REMOVED_ENV_SUBSTRINGS: Final = ("SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE_KEY")
NO_PROXY_HOSTS: Final = ("127.0.0.1", "localhost")
PROVIDER_KEY_ENV_VARS: Final = frozenset({"CURSOR_API_KEY"})
_NO_PROXY: Final = "NO_PROXY"

AbortReason = Literal[
    "timeout",
    "tool_limit",
    "cancelled",
    "process_exit",
    "protocol_error",
    "command_failed",
    "busy",
    "launch_failed",
    "prompt_aborted",
]
ABORT_REASON_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "timeout": "Execution timed out",
        "tool_limit": "Exceeded max tool call limit",
        "cancelled": "Cancelled",
        "process_exit": "omp process exited unexpectedly",
        "protocol_error": "Invalid RPC frame",
        "command_failed": "RPC command failed",
        "busy": "Max concurrent omp sessions reached",
        "launch_failed": "Failed to launch omp binary",
        "prompt_aborted": "omp reported prompt aborted",
    }
)
RunStatus = Literal["completed", "error", "aborted"]

_CREATIONFLAGS: Final = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_T = TypeVar("_T")


class OmpConfigError(ValueError):
    """Raised when omp launch configuration or environment isolation is invalid."""


class OmpAborted(Exception):
    """Raised when an omp session is aborted; the child process is guaranteed to be cleaned up."""

    def __init__(self, reason: str, detail: str, *, exit_code: int | None = None, stderr: str = "") -> None:
        super().__init__(f"{ABORT_REASON_LABELS.get(reason, reason)}: {detail}")
        self.reason = reason
        self.detail = detail
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class OmpPaths:
    """Isolated directory layout for omp child processes."""

    home: Path
    agent: Path
    natives: Path
    work: Path
    sessions: Path | None

    def __post_init__(self) -> None:
        for name in ("home", "agent", "natives", "work", "sessions"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Path(os.path.abspath(value)))

    @classmethod
    def under(cls, var: Path | str | None = None) -> OmpPaths:
        root = var_dir() if var is None else Path(var)
        home = root / "omp-home"
        return cls(
            home=home,
            agent=home / "agent",
            natives=home / "natives",
            work=root / "omp-work",
            sessions=root / "omp-sessions",
        )

    @property
    def local_app_data(self) -> Path:
        return self.home / "AppData" / "Local"

    @property
    def app_data(self) -> Path:
        return self.home / "AppData" / "Roaming"

    @property
    def temp(self) -> Path:
        return self.home / "tmp"

    def prepare(self) -> None:
        for path in (self.home, self.local_app_data, self.app_data, self.temp, self.agent, self.natives, self.work):
            path.mkdir(parents=True, exist_ok=True)
        if self.sessions is not None:
            self.sessions.mkdir(parents=True, exist_ok=True)
        if any(self.work.iterdir()):
            raise OmpConfigError(f"omp working directory {self.work} must be empty to prevent upward config discovery")


def _removed(name: str) -> bool:
    upper = name.upper()
    return (
        upper in REMOVED_ENV_VARS
        or upper.endswith(REMOVED_ENV_SUFFIXES)
        or any(part in upper for part in REMOVED_ENV_SUBSTRINGS)
    )


def _merge_no_proxy(values: Sequence[str]) -> str:
    hosts: dict[str, str] = {}
    for value in values:
        for entry in value.split(","):
            if host := entry.strip():
                hosts.setdefault(host.lower(), host)
    for host in NO_PROXY_HOSTS:
        hosts.setdefault(host.lower(), host)
    return ",".join(hosts.values())


def _isolation_overrides(paths: OmpPaths) -> dict[str, str]:
    home = str(paths.home)
    return {
        "USERPROFILE": home,
        "HOME": home,
        "LOCALAPPDATA": str(paths.local_app_data),
        "APPDATA": str(paths.app_data),
        "TEMP": str(paths.temp),
        "TMP": str(paths.temp),
        "PI_CODING_AGENT_DIR": str(paths.agent),
        "PI_NATIVES_DIR": str(paths.natives),
        "PI_BUILD_URL": UNREACHABLE_BUILD_URL,
        "OMP_SKIP_SETUP": "1",
        "PI_NO_TITLE": "1",
    }


def _check_provider_env(key: object, value: object) -> str:
    name = key.upper() if isinstance(key, str) else None
    if name not in PROVIDER_KEY_ENV_VARS:
        raise OmpConfigError(f"provider_env only allows whitelisted extension keys {sorted(PROVIDER_KEY_ENV_VARS)}: {key}")
    if not isinstance(value, str) or not value or any(ch.isspace() or not ch.isprintable() for ch in value):
        raise OmpConfigError(f"provider_env {key} must be a non-empty printable string without whitespace")
    return name


def isolated_env(
    paths: OmpPaths,
    *,
    base: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
    provider_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    overrides = _isolation_overrides(paths)
    reserved = {key.upper() for key in overrides}
    source = os.environ if base is None else base
    entries: dict[str, tuple[str, str]] = {}
    no_proxy: list[str] = []
    for key, value in source.items():
        upper = key.upper()
        if upper == _NO_PROXY:
            no_proxy.append(value)
        elif not _removed(key) and upper not in reserved:
            entries[upper] = (key, value)
    for key, value in (extra or {}).items():
        upper = key.upper()
        if upper == _NO_PROXY:
            no_proxy.append(str(value))
        elif _removed(key) or upper in reserved:
            raise OmpConfigError(f"extra_env cannot override isolated vars or pass secrets/tokens: {key}")
        else:
            entries[upper] = (key, str(value))
    env = dict(entries.values())
    env.update(overrides)
    env[_NO_PROXY] = _merge_no_proxy(no_proxy)
    for key, value in (provider_env or {}).items():
        env[_check_provider_env(key, value)] = value
    return env


@dataclass(frozen=True, slots=True)
class OmpLaunch:
    paths: OmpPaths = field(default_factory=OmpPaths.under)
    command: tuple[str, ...] = (str(DEFAULT_OMP_EXE),)
    model: str = DEFAULT_MODEL
    system_prompt: Path | None = None
    config: Path | None = None
    resume_session: Path | None = None
    extra_args: tuple[str, ...] = ()
    extra_env: Mapping[str, str] = field(default_factory=dict)
    extensions: tuple[Path, ...] = ()
    provider_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    thinking: str | None = None

    def validate(self) -> None:
        if not self.command or not all(isinstance(part, str) and part for part in self.command):
            raise OmpConfigError("command must be a non-empty sequence of strings")
        if not self.model:
            raise OmpConfigError("model cannot be empty")
        if self.thinking is not None and self.thinking not in THINKING_LEVELS:
            raise OmpConfigError(f"thinking must be one of {THINKING_LEVELS}, got {self.thinking!r}")
        for label, path in (("system_prompt", self.system_prompt), ("config", self.config), ("resume_session", self.resume_session)):
            if path is not None and not Path(path).is_file():
                raise OmpConfigError(f"{label} file does not exist: {path}")
        for extension in self.extensions:
            if not Path(extension).is_dir():
                raise OmpConfigError(f"extension directory does not exist: {extension}")

    def argv(self) -> list[str]:
        args = [*self.command, *OMP_RPC_ARGS]
        for extension in self.extensions:
            args += ["-e", os.path.abspath(extension)]
        args += ["--model", self.model]
        if self.thinking is not None:
            args += ["--thinking", self.thinking]
        if self.resume_session is not None:
            args += ["--resume", os.path.abspath(self.resume_session)]
        elif self.paths.sessions is None:
            args.append("--no-session")
        else:
            args += ["--session-dir", str(self.paths.sessions)]
        if self.system_prompt is not None:
            args += ["--system-prompt", os.path.abspath(self.system_prompt)]
        if self.config is not None:
            args += ["--config", os.path.abspath(self.config)]
        args += self.extra_args
        return args

    def env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        return isolated_env(self.paths, base=base, extra=self.extra_env, provider_env=self.provider_env)


@dataclass(frozen=True, slots=True)
class RunLimits:
    max_tool_calls: int = 8
    timeout_s: float = 90.0
    startup_timeout_s: float = 60.0
    command_timeout_s: float = 30.0
    kill_grace_s: float = 5.0
    slot_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if self.max_tool_calls < 0:
            raise OmpConfigError("max_tool_calls must be >= 0")
        for name in ("timeout_s", "startup_timeout_s", "command_timeout_s", "kill_grace_s"):
            if not getattr(self, name) > 0:
                raise OmpConfigError(f"{name} must be > 0")
        if self.slot_timeout_s is not None and self.slot_timeout_s < 0:
            raise OmpConfigError("slot_timeout_s must be >= 0")


class ConcurrencyLimiter:
    def __init__(self, limit: int = MAX_CONCURRENT_RUNS) -> None:
        if limit < 1:
            raise OmpConfigError("Concurrency limit must be >= 1")
        self.limit = limit
        self._cond = threading.Condition()
        self._in_use = 0
        self._waiting = 0
        self._peak = 0

    def acquire(self, timeout: float | None = None) -> bool:
        with self._cond:
            self._waiting += 1
            try:
                if not self._cond.wait_for(lambda: self._in_use < self.limit, timeout):
                    return False
                self._in_use += 1
                self._peak = max(self._peak, self._in_use)
                return True
            finally:
                self._waiting -= 1

    def release(self) -> None:
        with self._cond:
            if self._in_use <= 0:
                raise RuntimeError("release called more times than acquire")
            self._in_use -= 1
            self._cond.notify()

    @property
    def in_use(self) -> int:
        return self._in_use

    @property
    def waiting(self) -> int:
        return self._waiting

    @property
    def peak(self) -> int:
        return self._peak


DEFAULT_LIMITER: Final = ConcurrencyLimiter(MAX_CONCURRENT_RUNS)
_ACTIVE: dict[int, subprocess.Popen[bytes]] = {}
_ACTIVE_LOCK = threading.Lock()


def active_pids() -> tuple[int, ...]:
    with _ACTIVE_LOCK:
        return tuple(pid for pid, proc in _ACTIVE.items() if proc.poll() is None)


class _StderrTail:
    def __init__(self, stream: IO[bytes], name: str, limit: int = 64 * 1024) -> None:
        self._data = bytearray()
        self._lock = threading.Lock()
        self._limit = limit
        self._thread = threading.Thread(target=self._run, args=(stream,), name=name, daemon=True)
        self._thread.start()

    def _run(self, stream: IO[bytes]) -> None:
        read = getattr(stream, "read1", stream.read)
        try:
            while chunk := read(4096):
                with self._lock:
                    self._data += chunk
                    if len(self._data) > self._limit:
                        del self._data[: len(self._data) - self._limit]
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        with self._lock:
            return self._data.decode("utf-8", errors="replace")

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)


@dataclass(frozen=True, slots=True)
class OmpRunResult:
    status: RunStatus
    reason: str | None
    detail: str
    outcome: PromptOutcome | None
    assistant_text: str | None
    session_file: str | None
    session_id: str | None
    tool_names: tuple[str, ...]
    tool_calls: tuple[HostToolExchange, ...]
    events: tuple[RpcEvent, ...] = field(repr=False)
    exit_code: int | None
    elapsed_s: float
    stderr: str = field(repr=False)

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class OmpSession:
    """Manages a single sandboxed omp child process in RPC mode."""

    def __init__(
        self,
        launch: OmpLaunch | None = None,
        *,
        limits: RunLimits | None = None,
        handler: HostToolHandler | None = None,
        on_event: Callable[[RpcEvent], object] | None = None,
        limiter: ConcurrencyLimiter | None = None,
    ) -> None:
        self.launch = OmpLaunch() if launch is None else launch
        self.limits = RunLimits() if limits is None else limits
        self._handler = handler
        self._on_event = on_event
        self._limiter = DEFAULT_LIMITER if limiter is None else limiter
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._client: RpcClient | None = None
        self._stderr: _StderrTail | None = None
        self._watchdog: threading.Timer | None = None
        self._has_slot = False
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._deadline: float | None = None
        self._exit_code: int | None = None
        self._watchdog_fired = False
        self._closed = False
        self._aborted: OmpAborted | None = None

    def __enter__(self) -> OmpSession:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._shutdown(send_abort=exc_type is not None)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def client(self) -> RpcClient | None:
        return self._client

    @property
    def ready(self) -> ReadyInfo | None:
        return self._client.ready if self._client is not None else None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def aborted(self) -> OmpAborted | None:
        return self._aborted

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stderr(self) -> str:
        return self._stderr.text() if self._stderr is not None else ""

    @property
    def elapsed_s(self) -> float:
        if self._started_at is None:
            return 0.0
        return (self._ended_at if self._ended_at is not None else time.monotonic()) - self._started_at

    def start(self) -> ReadyInfo:
        with self._lock:
            if self._proc is not None or self._closed:
                raise RuntimeError("OmpSession can only be started once")
            launch = self.launch
            launch.validate()
            argv = launch.argv()
            env = launch.env()
            launch.paths.prepare()
            wait = self.limits.timeout_s if self.limits.slot_timeout_s is None else self.limits.slot_timeout_s
            if not self._limiter.acquire(timeout=wait):
                self._closed = True
                raise self._record(
                    OmpAborted("busy", f"Max concurrent omp sessions ({self._limiter.limit}) busy after {wait:g}s")
                )
            self._has_slot = True
            self._started_at = time.monotonic()
            self._deadline = self._started_at + self.limits.timeout_s
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(launch.paths.work),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=_CREATIONFLAGS,
                )
            except OSError as exc:
                self._closed = True
                self._ended_at = time.monotonic()
                self._release()
                raise self._record(
                    OmpAborted("launch_failed", f"Failed to start {argv[0]} ({exc}); run scripts/get-omp.ps1 first")
                ) from exc
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            self._proc = proc
            with _ACTIVE_LOCK:
                _ACTIVE[proc.pid] = proc
            self._stderr = _StderrTail(proc.stderr, f"omp-{proc.pid}-stderr")
            self._watchdog = threading.Timer(self.limits.timeout_s + self.limits.kill_grace_s, self._on_watchdog)
            self._watchdog.daemon = True
            self._watchdog.start()
            self._client = RpcClient(
                proc.stdin,
                proc.stdout,
                handler=self._handler,
                on_event=self._on_event,
                max_tool_calls=self.limits.max_tool_calls,
                name=f"omp-{proc.pid}",
            )
            self._client.deadline = self._deadline
        return self._call(lambda client: client.connect(self.limits.startup_timeout_s))

    def set_host_tools(self, tools: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        return self._call(lambda client: client.set_host_tools(tools, timeout=self.limits.command_timeout_s))

    def get_state(self) -> dict[str, Any]:
        return self._call(lambda client: client.get_state(timeout=self.limits.command_timeout_s))

    def last_assistant_text(self) -> str | None:
        return self._call(lambda client: client.last_assistant_text(timeout=self.limits.command_timeout_s))

    def request(self, command: str, **fields: Any) -> Any:
        return self._call(lambda client: client.request(command, timeout=self.limits.command_timeout_s, **fields))

    def prompt(self, message: str) -> PromptOutcome:
        return self._call(lambda client: client.prompt(message))

    def cancel(self, reason: str = "cancelled", detail: str = "Cancelled by caller") -> None:
        client = self._client
        if client is not None and not self._closed:
            client.cancel(reason, detail)

    def close(self) -> int | None:
        return self._shutdown(send_abort=False)

    def run(self, message: str, *, tools: Sequence[Mapping[str, Any]] = ()) -> OmpRunResult:
        names: tuple[str, ...] = ()
        state: Mapping[str, Any] = {}
        outcome: PromptOutcome | None = None
        text: str | None = None
        aborted: OmpAborted | None = None
        try:
            self.start()
            if tools:
                names = self.set_host_tools(tools)
            state = self.get_state()
            outcome = self.prompt(message)
            if outcome.agent_invoked:
                text = self.last_assistant_text()
        except OmpAborted as exc:
            aborted = exc
        finally:
            self.close()

        status: RunStatus
        reason: str | None = None
        if aborted is not None:
            status, reason, detail = "aborted", aborted.reason, aborted.detail
        elif outcome is not None and outcome.status == "completed":
            status, detail = "completed", ""
        elif outcome is not None and outcome.status == "aborted":
            status, reason, detail = "aborted", "prompt_aborted", "omp reported prompt aborted"
        else:
            status = "error"
            msg = outcome.error.get("message") if outcome is not None and outcome.error is not None else None
            detail = f"omp prompt error: {msg}" if msg else f"omp prompt status: {outcome.status if outcome else None}"
        if self._exit_code not in (0, None) and status != "aborted":
            detail = (f"{detail}; " if detail else "") + f"omp exit code {self._exit_code}"
        session_file = state.get("sessionFile")
        session_id = state.get("sessionId")
        client = self._client
        return OmpRunResult(
            status=status,
            reason=reason,
            detail=detail,
            outcome=outcome,
            assistant_text=text,
            session_file=session_file if isinstance(session_file, str) else None,
            session_id=session_id if isinstance(session_id, str) else None,
            tool_names=names,
            tool_calls=client.tool_calls if client is not None else (),
            events=client.events if client is not None else (),
            exit_code=self._exit_code,
            elapsed_s=self.elapsed_s,
            stderr=self.stderr,
        )

    def _call(self, operation: Callable[[RpcClient], _T]) -> _T:
        if self._aborted is not None:
            raise self._aborted
        client = self._client
        if client is None or self._closed:
            raise RuntimeError("OmpSession is not started or already closed")
        try:
            return operation(client)
        except RpcError as exc:
            raise self._abort(exc.reason, exc.detail) from exc
        except BaseException:
            self._shutdown(send_abort=True)
            raise

    def _abort(self, reason: str, detail: str) -> OmpAborted:
        if self._watchdog_fired:
            reason, detail = "timeout", f"Exceeded {self.limits.timeout_s:g}s limit; watchdog killed omp"
        elif reason == "timeout" and self._deadline is not None and time.monotonic() >= self._deadline:
            detail = f"Exceeded {self.limits.timeout_s:g}s limit; {detail}"
        self._shutdown(send_abort=True)
        return self._record(OmpAborted(reason, detail, exit_code=self._exit_code, stderr=self.stderr))

    def _record(self, aborted: OmpAborted) -> OmpAborted:
        if self._aborted is None:
            self._aborted = aborted
        return self._aborted

    def _on_watchdog(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._watchdog_fired = True
            try:
                proc.kill()
            except OSError:
                pass

    def _shutdown(self, *, send_abort: bool) -> int | None:
        with self._lock:
            if self._closed:
                return self._exit_code
            self._closed = True
            proc, client = self._proc, self._client
            grace = self.limits.kill_grace_s
            try:
                if client is not None:
                    if send_abort and proc is not None and proc.poll() is None:
                        client.send_abort()
                    try:
                        client.close(timeout=grace)
                    except Exception:
                        client.close_stdin()
                if proc is not None:
                    try:
                        self._exit_code = proc.wait(timeout=grace)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            self._exit_code = proc.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            self._exit_code = proc.poll()
                if client is not None:
                    client.wait_reader(timeout=1.0)
                if self._stderr is not None:
                    self._stderr.join(timeout=1.0)
            finally:
                if self._watchdog is not None:
                    self._watchdog.cancel()
                if proc is not None:
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
                    with _ACTIVE_LOCK:
                        _ACTIVE.pop(proc.pid, None)
                self._ended_at = time.monotonic()
                self._release()
            return self._exit_code

    def _release(self) -> None:
        if self._has_slot:
            self._has_slot = False
            self._limiter.release()
