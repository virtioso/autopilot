# AI Interactive Console Guide

**Last Updated**: 2026-02-07

This guide explains how AI tools (Codex/Claude Code) can interact with guest
Linux consoles through Autopilot. It is designed to be generic and works for
single or multiple UART consoles (e.g., VM0/VM1).

## MCP Server Availability

The `sel4-autopilot` MCP server is defined in `~/tii-sel4/.mcp.json`.
Some clients auto-load MCP servers from `.mcp.json`; some do not.
If MCP is unavailable, fall back to the request/result queues in `AUTOPILOT_DIR`.

## Concepts

- **Interactive sessions** are created by Autopilot after a boot completes.
- Each session has:
  - A UART port (example: `/dev/ttyACM0`)
  - A **profile** that describes login/prompt regexes
  - A session ID and transcript files
- AI tools interact via MCP tools or the client helper functions.

## Mandatory: Set AUTOPILOT_DIR

Always set `AUTOPILOT_DIR` in your environment before using the client or MCP tools.

Defaults for `AUTOPILOT_DIR`, TTYs, and queue names are defined in `config.py` (SSOT).
This points to the **working directory** that contains the request/result queues,
not the code location.

Example:

```bash
export AUTOPILOT_DIR=/home/hlyytine/tii-sel4/autopilot
```

If you need to reference the client library explicitly, use the code location:
`/home/hlyytine/autopilot/sel4_client.py`

## Profiles

Profiles live in `/home/hlyytine/autopilot/profiles/` and define prompts and login behavior.

Example: `/home/hlyytine/autopilot/profiles/linux-yocto.json`
```json
{
  "name": "linux-yocto",
  "baud": 115200,
  "login": {
    "prompt": "login:",
    "username": "root",
    "password_prompt": "Password:",
    "password": "",
    "post_login_prompt": "root@.*:~#"
  },
  "shell": { "prompt": "root@.*:~#" },
  "ready": { "prompt": "login:" }
}
```

Profiles are **generic**. Create additional profiles for different guest OS
prompts or login flows.

Profiles are **static data**. Edit them only in `/home/hlyytine/autopilot/profiles` (the
code repo) and do not copy them into `AUTOPILOT_DIR`.

## Step 1: Submit a boot_interactive request

Create a `.request` file in `requests/pending/`.

### Example: boot EFI and open two sessions

```json
{
  "type": "boot_interactive",
  "boot_target": "efi",
  "binary_path": "/path/to/capdl-loader-image-arm-orinagx",
  "binary_name": "vm_qemu_virtio.efi",
  "interactive": {
    "enabled": true,
    "phase": "post_boot",
    "sessions": [
      { "name": "vm0", "port": "/dev/ttyACM0", "profile": "linux-yocto" },
      { "name": "vm1", "port": "/dev/ttyACM1", "profile": "linux-yocto" }
    ],
    "idle_timeout_s": 900
  }
}
```

### Example: boot stock Linux and open one session

```json
{
  "type": "boot_interactive",
  "boot_target": "stock_linux",
  "interactive": {
    "enabled": true,
    "phase": "post_boot",
    "sessions": [
      { "name": "linux", "port": "/dev/ttyACM0", "profile": "linux-yocto" }
    ]
  }
}
```

## Step 2: Discover sessions

Use MCP:
- `list_console_sessions`

The response includes session IDs and ports.

## Step 3: Open a session

Use MCP:
- `open_console_session`

This returns:
```json
{
  "session_id": "20260204-123456-vm0",
  "offset": 1024,
  "log_path": "/.../results/<ts>/console/vm0.log"
}
```

## Step 4: Send commands

Use MCP:
- `send_console_command`

Example:
```json
{
  "session_id": "...",
  "command": "uname -a",
  "append_newline": true,
  "wait_for_prompt": true,
  "timeout_s": 10
}
```

If `wait_for_prompt` is true, Autopilot waits for the prompt regex from the
profile (or `prompt_override`) before returning.

## Step 5: Read output manually (optional)

Use MCP:
- `read_console_output`

Example:
```json
{
  "session_id": "...",
  "offset": 1024,
  "max_bytes": 4096
}
```

This returns:
```json
{
  "output": "...",
  "new_offset": 2048
}
```

## Step 6: Close the session

Use MCP:
- `close_console_session`

Autopilot will close the UART and mark the session as closed.

## Transcripts and Artifacts

For each interactive request:

```
results/<timestamp>/console/
  sessions.json        # session manifest
  vm0.log              # raw UART transcript
  vm0.jsonl            # rx/tx event stream
  vm1.log
  vm1.jsonl
```

## Notes and Limitations

- One session per UART port (exclusive lock).
- Auto-login is best-effort based on profile regexes.
- Idle timeout closes all sessions if no activity.
- Output polling uses byte offsets; keep track of `new_offset`.
