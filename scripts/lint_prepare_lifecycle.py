#!/usr/bin/env python3
import json
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_ROOT))

from chain_runtime import FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS


def main() -> int:
    chains_dir = CODE_ROOT / "chains"
    failures = []

    for chain_path in sorted(chains_dir.glob("*.json")):
        try:
            chain = json.loads(chain_path.read_text())
        except Exception as exc:
            failures.append(f"{chain_path}: json_parse_error: {exc}")
            continue
        for step_name, step in (chain.get("steps") or {}).items():
            step_type = step.get("type")
            if step_type == "task_spawn":
                if str(step.get("task", "")).strip() == "prepare_next_run":
                    failures.append(
                        f"{chain_path}:{step_name}: {FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS[0]}"
                    )
                if str(step.get("chain", "")).strip() == "prepare_next_run_task":
                    failures.append(
                        f"{chain_path}:{step_name}: {FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS[1]}"
                    )
            if step_type == "signal_set":
                if str(step.get("signal", "")).strip() == "prepare_next_run_go":
                    failures.append(
                        f"{chain_path}:{step_name}: {FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS[2]}"
                    )
            if step_type == "task_join":
                for task_name in step.get("tasks", []) or []:
                    if str(task_name).strip() == "prepare_next_run":
                        failures.append(
                            f"{chain_path}:{step_name}: {FORBIDDEN_PREPARE_LIFECYCLE_PATTERNS[3]}"
                        )
                        break

    if failures:
        print("prepare lifecycle lint failed:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("prepare lifecycle lint passed: no forbidden signatures found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
