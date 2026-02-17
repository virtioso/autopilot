#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    code_root = Path(__file__).resolve().parent.parent
    py = sys.executable
    sys.path.insert(0, str(code_root))

    checks = [
        ("py_compile", [py, "-m", "py_compile", str(code_root / "chain_runtime.py"), str(code_root / "orin_kernel_autopilot.py"), str(code_root / "sel4_client.py"), str(code_root / "sel4_mcp_server.py")]),
        ("prepare_lifecycle_lint", [py, str(code_root / "scripts" / "lint_prepare_lifecycle.py")]),
    ]

    for name, cmd in checks:
        proc = subprocess.run(cmd, cwd=str(code_root), capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"[FAIL] {name}")
            if proc.stdout.strip():
                print(proc.stdout.strip())
            if proc.stderr.strip():
                print(proc.stderr.strip())
            return 1
        print(f"[OK] {name}")

    from chain_runtime import validate_chain

    chains_dir = code_root / "chains"
    for chain_path in sorted(chains_dir.glob("*.json")):
        try:
            chain = json.loads(chain_path.read_text())
            validate_chain(chain)
        except Exception as exc:
            print(f"[FAIL] validate_chain {chain_path.name}: {exc}")
            return 1
    print(f"[OK] validate_chain_all ({len(list(chains_dir.glob('*.json')))} chains)")
    print("[OK] preflight_autopilot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
