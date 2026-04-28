#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: new-project-init.sh <workspace_root> [autopilot_dir]

Creates an Autopilot working directory (queues/results/runtime) inside the
workspace and prints the command-API setup for agents.

Arguments:
  workspace_root Path to the workspace root (required)
  autopilot_dir  Path to Autopilot working dir (default: <workspace_root>/autopilot)

Environment:
  AUTOPILOT_CODE   Path to Autopilot code repo (default: <workspace_root>/tools/autopilot)
  AUTOPILOT_TTY0   Example: /dev/ttyACM0
  AUTOPILOT_TTY1   Example: /dev/ttyACM1
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 1
fi

workspace_root="${1%/}"
autopilot_dir="${2:-${workspace_root}/autopilot}"
autopilot_code="${AUTOPILOT_CODE:-${workspace_root}/tools/autopilot}"

mkdir -p "${autopilot_dir}/requests/"{pending,processing,completed,failed}
mkdir -p "${autopilot_dir}/results"

cat <<EOF
Initialized Autopilot working directory:
  WORKSPACE=${workspace_root}
  AUTOPILOT_DIR=${autopilot_dir}
  AUTOPILOT_CODE=${autopilot_code}

Next:
  export WORKSPACE="${workspace_root}"
  export AUTOPILOT_DIR="${autopilot_dir}"
  export AUTOPILOT_TTY0="${AUTOPILOT_TTY0:-/dev/ttyACM0}"
  export AUTOPILOT_TTY1="${AUTOPILOT_TTY1:-/dev/ttyACM1}"
  ln -s "${autopilot_code}/bin/autopilot" "\$HOME/.local/bin/autopilot"
  autopilot --autopilot-dir "${autopilot_dir}" status --json
EOF
