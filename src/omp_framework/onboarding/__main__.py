"""CLI entry point for the Step-by-Step Onboarding Wizard (`python -m omp_framework.onboarding`)."""

from __future__ import annotations

import argparse
import sys

from omp_framework.api.app import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omp_framework.onboarding",
        description="Gated 7-Step Onboarding & Customization Wizard for OmpAgentFramework",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="Show current step, progress, and instructions")
    v_parser = sub.add_parser("verify", help="Verify current step (or specified step_id) and unlock the next step")
    v_parser.add_argument("step_id", nargs="?", default=None, help="Optional step_id to verify")
    sub.add_parser("verify-all", help="Sequentially verify all 7 steps from Step 1 to Step 7")
    sub.add_parser("reset", help="Reset onboarding progress back to Step 1")

    args = parser.parse_args(argv)
    app = create_app()
    wf = app.state.onboarding

    if args.cmd == "reset":
        state = wf.reset()
        print(f"[onboarding] Progress reset. Current step: Step {state.current_step_number} ({state.current_step_id})")
        return 0

    if args.cmd == "verify-all":
        for step in wf.get_state().steps:
            res = wf.verify_step(step.step_id)
            mark = "PASS" if res.passed else "FAIL"
            print(f"[{mark}] Step {res.step_number} ({res.step_id}): {res.summary}")
            if not res.passed:
                if res.remediation_hint:
                    print(f"       Hint: {res.remediation_hint}")
                return 1
        print("\nAll 7 onboarding steps verified and completed!")
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
