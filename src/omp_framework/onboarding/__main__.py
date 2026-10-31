"""CLI entry point for the Step-by-Step Onboarding Wizard (`python -m omp_framework.onboarding`)."""

from __future__ import annotations

import argparse
import sys

from omp_framework.api.app import create_app
from omp_framework.onboarding.engine import REQUIREMENT_QUESTIONS, DomainRequirementSpec


def _split_csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omp_framework.onboarding",
        description="Gated 8-Step Onboarding & Requirement Discovery Wizard for OmpAgentFramework",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="Show current step, progress, and instructions")
    int_parser = sub.add_parser("interview", help="Interactive Step 1 Requirement Discovery Questionnaire")
    int_parser.add_argument("--defaults", action="store_true", help="Accept default starter requirements non-interactively")
    v_parser = sub.add_parser("verify", help="Verify current step (or specified step_id) and unlock the next step")
    v_parser.add_argument("step_id", nargs="?", default=None, help="Optional step_id to verify")
    sub.add_parser("verify-all", help="Capture default requirements (if not set) and sequentially verify all 8 steps")
    sub.add_parser("reset", help="Reset onboarding progress back to Step 1")

    args = parser.parse_args(argv)
    app = create_app()
    wf = app.state.onboarding

    if args.cmd == "reset":
        state = wf.reset()
        print(f"[onboarding] Progress reset. Current step: Step {state.current_step_number} ({state.current_step_id})")
        return 0

    if args.cmd == "interview":
        print("=== Step 1: Requirement Discovery & Domain Blueprint Interview ===")
        answers: dict[str, object] = {}
        list_fields = {
            "sample_scope_ids",
            "key_metrics",
            "selected_archetypes",
            "read_only_tools",
            "mutating_proposals",
            "human_approver_roles",
            "event_triggers",
        }
        for q in REQUIREMENT_QUESTIONS:
            field = q["field"]
            default_val = q["default"]
            if args.defaults:
                raw_val = default_val
                print(f"{q['question']}\n  -> {raw_val}")
            else:
                try:
                    user_in = input(f"{q['question']} [{default_val}]: ").strip()
                except EOFError:
                    user_in = ""
                raw_val = user_in if user_in else default_val
            answers[field] = _split_csv(raw_val) if field in list_fields else raw_val

        spec = DomainRequirementSpec.model_validate(answers)
        res = wf.save_requirements(spec)
        print(f"\n[PASS] {res.summary}")
        print("Generated: var/domain_requirements.json and var/DOMAIN_BLUEPRINT_SPEC.md")
        if res.unlocked_next_step:
            print(f"[UNLOCKED] Next Step: {res.unlocked_next_step}")
        return 0

    if args.cmd == "verify-all":
        if wf.get_state().requirements is None:
            default_spec = DomainRequirementSpec(
                project_name="ServiceSentinel",
                domain_purpose="Automated reliability monitoring, root-cause triage, and human-governed remediation",
                scope_entity_name="cluster",
                sample_scope_ids=["prod-cluster-01", "edge-site-alpha"],
                key_metrics=["cpu_load_ratio", "p99_latency_ms", "availability_sla"],
                selected_archetypes=["query", "report", "rulecard", "ops"],
                read_only_tools=["get_system_metrics", "list_recent_anomalies"],
                mutating_proposals=["submit_report_draft", "simulate_rule_change", "propose_rule_draft", "create_ticket", "propose_intent"],
                human_approver_roles=["ops_lead", "domain_expert", "safety_officer"],
                event_triggers=["link_packet_loss", "latency_spike"],
                llm_mode_preference="offline",
            )
            wf.save_requirements(default_spec)
        for step in wf.get_state().steps:
            res = wf.verify_step(step.step_id)
            mark = "PASS" if res.passed else "FAIL"
            print(f"[{mark}] Step {res.step_number} ({res.step_id}): {res.summary}")
            if not res.passed:
                if res.remediation_hint:
                    print(f"       Hint: {res.remediation_hint}")
                return 1
        print("\nAll 8 onboarding steps verified and completed!")
        return 0

    if args.cmd == "verify":
        res = wf.verify_step(args.step_id) if args.step_id else wf.verify_current_step()
        mark = "PASS" if res.passed else "FAIL"
        print(f"[{mark}] Step {res.step_number} ({res.step_id}): {res.summary}")
        for c in res.checks:
            c_mark = "[OK]" if c.get("ok") else "[X]"
            print(f"  {c_mark} {c.get('check')}: {c.get('detail')}")
        if not res.passed and res.remediation_hint:
            print(f"\nRemediation Hint: {res.remediation_hint}")
            return 1
        if res.unlocked_next_step:
            print(f"\n[UNLOCKED] Next Step: {res.unlocked_next_step}")
        else:
            print("\nAll 7 steps completed!")
        return 0

    # Default: status
    state = wf.get_state()
    print(f"=== OmpAgentFramework Onboarding Wizard ({state.completed_count}/{state.total_steps} completed, {state.progress_pct}%) ===")
    for s in state.steps:
        icon = "[DONE]" if s.status == "completed" else ("[NEXT]" if s.status == "current" else "[LOCK]")
        print(f"{icon} Step {s.step_number}: {s.title} [{s.status.upper()}] ({s.step_id})")
        if s.status == "current":
            print(f"   Goal: {s.goal}")
            print(f"   Files: {', '.join(s.files_to_edit)}")
            for idx, inst in enumerate(s.instructions, 1):
                print(f"     {idx}. {inst}")
            print(f"   Verify Command: {s.cli_command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
