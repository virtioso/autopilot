# tmux UI Migration Notes

**Date**: 2026-02-08

This release moves operator interaction from the in-process Autopilot TUI to
tmux-native controls.

## What Changed

- Removed in-process `Ctrl-A` key handling from Autopilot runtime.
- `map_window` remains in profile chains but now binds tmux windows at runtime.
- Status is now rendered by tmux via `runtime/ui/state.json`.
- Abort is triggered via tmux key binding (`Ctrl-B` then `r`) using the local
  control socket (`runtime/ui/control.sock`).
- Raw input is forwarded from per-source tmux pane clients to mapped UART
  sources.

## Operator Command Mapping

- Old: `Ctrl-A` + `1..9` (switch windows)
- New: `Ctrl-B` + `0..9` (switch tmux windows)

- Old: `Ctrl-A` + `R` (abort)
- New: `Ctrl-B` + `r` (abort)

- Old: `Ctrl-A` + `I` (toggle in-process input mode)
- New: type directly in the source tmux window (raw forwarding)

## Compatibility

- Existing profiles with `map_window` continue to work.
- No profile schema change is required for this migration.
