#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: new-project-init.sh <project_root> [autopilot_dir]

Creates an Autopilot working directory (queues/results/runtime) inside the
project and optionally writes a .mcp.json for MCP clients.

Arguments:
  project_root   Path to the project repo root (required)
  autopilot_dir  Path to Autopilot working dir (default: <project_root>/autopilot)

Environment:
  AUTOPILOT_CODE   Path to Autopilot code repo (default: ~/autopilot)
  AUTOPILOT_TTY0   Example: /dev/ttyACM0
  AUTOPILOT_TTY1   Example: /dev/ttyACM1
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 1
fi

project_root="$1"
autopilot_dir="${2:-${project_root%/}/autopilot}"
autopilot_code="${AUTOPILOT_CODE:-$HOME/autopilot}"

mkdir -p "${autopilot_dir}/requests/"{pending,inflight,done,failed}
mkdir -p "${autopilot_dir}/results"

cat > "${project_root%/}/.mcp.json" <<EOF
{
  "mcpServers": {
    "sel4-autopilot": {
      "command": "python3",
      "args": ["${autopilot_code}/sel4_mcp_server.py"],
      "env": {
        "AUTOPILOT_DIR": "\${AUTOPILOT_DIR:-${autopilot_dir}}"
      }
    }
  }
}
EOF

cat <<EOF
Initialized Autopilot working directory:
  AUTOPILOT_DIR=${autopilot_dir}

Next:
  export AUTOPILOT_DIR="${autopilot_dir}"
  export AUTOPILOT_TTY0="${AUTOPILOT_TTY0:-/dev/ttyACM0}"
  export AUTOPILOT_TTY1="${AUTOPILOT_TTY1:-/dev/ttyACM1}"
  python3 "${autopilot_code}/orin_kernel_autopilot.py"
EOF
