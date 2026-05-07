# Autopilot: Bringing Real Hardware Into the AI Development Loop

## The Situation

Low-level systems development is difficult even before testing begins. In this
case the work involved pKVM/SMMUv2 driver development on NVIDIA Orin AGX and
later AI-assisted porting of the VirtIO/seL4-based Virtioso system to Orin AGX
and qemu_x86_64. The hard part was not only writing code. The hard part was
closing the feedback loop on real hardware.

Before Autopilot, each experiment required a human to build the artifact, copy
it to the target, reboot or reset the board, catch bootloader or UEFI prompts,
type the right command, watch one or more UARTs, save the logs, decide whether
the failure was build, upload, bootloader, kernel, guest, runner, or cleanup
related, and then return the system to a usable state for the next run.

That workflow is slow, repetitive, and easy to get wrong. It is also exactly
the part that blocks AI agents from being useful in low-level systems work. AI
tools can help with architecture, implementation, refactoring, and review, but
without a reliable way to test on the real target they are trapped in source
code and local reasoning.

Autopilot was built to solve that gap: it gives AI agents and humans a
structured command interface for deploying, booting, observing, diagnosing,
interacting with, and recovering real hardware and remote execution targets.

## The AI Approach

Autopilot is itself an AI-generated tool. Its code was written by AI agents,
then used to let AI agents test low-level software on real hardware. That makes
it a concrete example of a self-reinforcing AI development loop: AI builds the
tooling that gives AI better access to real-world engineering feedback.

The main AI tools were Claude Code with Claude Max and Codex CLI with ChatGPT.
I used them as coding agents, not just as chatbots: they inspected the codebase,
proposed architecture, edited Python and JSON chain files, wrote documentation,
ran shell commands, read logs, debugged failures, and iterated from test
results. The human role was to set direction, correct wrong assumptions, decide
what mattered, and review the engineering tradeoffs.

The technique was iterative agentic development. A typical loop was:

1. Ask the AI agent to implement or adjust an Autopilot capability.
2. Let it inspect the relevant code and make a focused change.
3. Use Autopilot to run the real hardware or remote QEMU test.
4. Feed the resulting logs, verdicts, and failure boundaries back into the next
   AI-assisted implementation step.

The core architecture is:

```text
AI agent -> Autopilot command API -> target-specific backend -> structured evidence
```

The AI agent does not manipulate serial devices, queue files, tmux sessions,
reset wiring, SSH scripts, or QEMU launch internals directly. It uses one
agent-facing command API:

```bash
autopilot --autopilot-dir /home/johndoe/virtioso/autopilot submit efi \
  --chain vm-qemu-virtio \
  --binary /path/to/capdl-loader-image \
  --json
```

Autopilot then executes named JSON chains. A chain is a small state machine:
steps upload files, run SSH commands, reboot or reset targets, enter UEFI
shells, launch remote runners, map UART or process streams, send console input,
wait for patterns, classify outcomes, set verdicts, and run recovery steps.

The important abstraction is not "UART automation." It is controlled actions
plus observed text streams. Today Autopilot consumes physical UARTs, SSH-backed
runner output, process-backed sources, and PTY-backed interactive consoles. It
records step timing, source ranges, regex matches, logs, verdicts, and
housekeeping state so both the human and the AI agent can reason from the same
evidence.

Two concrete backends show the approach:

- **Orin AGX physical board:** the board boots stock Linux first; Autopilot
  uploads artifacts over SSH/SCP, reboots, watches UART output, gets into the
  UEFI shell, types the EFI test binary path, and can use a USB relay to toggle
  the reset line when a hard reset is needed.
- **qemu_x86_64 remote execution:** seL4 x86 hypervisor execution needed Intel
  VMX, while the AI-agent workstation had an AMD processor. Virtioso generates
  a remote QEMU execution bundle and test artifacts; Autopilot verifies the
  remote Intel host, launches the Virtioso runner over SSH, and retrieves logs
  and verdict evidence over SSH.

## The Result

The concrete outcome is a working hardware-in-the-loop automation system that
AI agents can use. The repository currently has 297 commits, 125 tracked files,
28 chain JSON files, and about 11.5k lines of Python. Recent commits show the
system maturing into an agent-facing tool: JSON command wrapper, structured CLI
errors, bounded logs/evidence, queue cleanup on startup, SSH readiness gating
for remote QEMU, VM readiness checks, tmux session hardening, and explicit TTY
configuration.

Autopilot now enables:

- AI-driven build/test loops on real embedded hardware.
- Repeatable Orin AGX EFI boot testing with UART, UEFI, SSH/SCP, and relay
  reset handling.
- Remote qemu_x86_64 validation on an Intel VMX-capable host from an AMD
  workstation.
- Structured verdicts and evidence instead of ad hoc serial-log inspection.
- Failure-boundary classification: setup, upload, runner reachability,
  bootloader/UEFI, kernel/hypervisor/runtime, and cleanup/recovery.
- Live tmux operator sessions where humans can watch Autopilot progress and
  interact directly with target consoles.
- Chains that either recover the target to a known ready state for the next run
  or intentionally leave it running for interactive investigation.

The result is not just saved keystrokes. Real hardware becomes part of the AI
feedback loop. An AI agent can implement a change, submit a hardware test, read
the structured result, inspect bounded logs or evidence, and continue from real
runtime behavior.

## The Contrast

Without AI, this project would likely have remained a collection of manual lab
procedures, one-off scripts, and human memory. A human would still have to
perform most deploy/boot/test/recover steps, and AI agents would be limited to
suggesting code without being able to verify it on the target.

Without Autopilot, a failed run is ambiguous. Did the image fail to build? Was
the wrong artifact uploaded? Did SSH fail? Did the board miss the UEFI prompt?
Did the kernel panic? Did the guest VM boot but fail later? Did the remote QEMU
host lack the right runtime? Each confusion sends the developer or AI agent to
the wrong layer.

With Autopilot, those layers are encoded as chains, steps, sources, timeouts,
patterns, verdicts, logs, and recovery behavior. The AI agent does not need to
guess how to operate the lab. The human does not need to manually repeat the
same fragile sequence. Both work from the same evidence.

This is the important before/after: AI assistance moved from "write or review
code" to "write, deploy, boot, observe, diagnose, and iterate against real
hardware."

## Evidence

The strongest evidence is not only that Autopilot exists, but what it made
possible.

First, Autopilot enabled AI agents to develop a pKVM SMMUv2 driver for NVIDIA
Orin AGX with much more autonomy than would otherwise have been practical. The
AI could compare the existing pKVM SMMUv3 driver model with NVIDIA's SMMUv2
driver in vanilla Linux, implement changes, build test artifacts, run them on
the Orin AGX board, inspect the real UART evidence, and iterate from hardware
behavior instead of only reasoning from source code.

Second, Autopilot enabled AI-assisted porting of Virtioso to Orin AGX. That
work required repeated deploy/boot/test cycles across seL4, CAmkES, Linux
guests, QEMU-backed virtio services, UART output, SSH upload paths, and target
recovery. Autopilot made those cycles executable by AI agents rather than
manual lab rituals.

Third, this workflow helped uncover a longstanding bug in MMU shareability
attributes that had prevented seL4 from working correctly on NVIDIA hardware.
That is a concrete example of the value of closing the AI feedback loop on real
hardware: the system did not merely automate known-good tests; it helped expose
a deep platform issue that required repeated hardware-backed investigation.

Supporting artifacts include the Autopilot repository itself
(`https://github.com/virtioso/autopilot`): roughly ten thousand lines of Python
implementing the runtime, chain engine, command API, console sessions, tmux UI,
filters, and analysis helpers. The same AI-assisted workflow is still used to
improve Autopilot itself, so the tool continues to evolve through the
development loop it enables.

Each run also produces concrete evidence: `chain.json`, console logs, matched
source offsets/ranges, verdicts, and captured artifacts under the Autopilot
results directory. In tmux, humans can watch Autopilot status and target TTYs
in real time while AI agents run tests.

## Future Direction

Autopilot can grow beyond UART and QEMU logs without changing the architecture.
Any observable behavior can become test evidence if a small adapter exposes it
as a timestamped text stream:

```text
physical or virtual signal -> small adapter -> timestamped text stream -> Autopilot chain decision
```

Examples include a camera adapter that emits `SPLASH_SCREEN_SEEN elapsed_ms=...`
for visual boot timing, a CAN bus monitor that emits normalized traffic events,
or multiple computers and VMs participating in one distributed test chain.

This direction is likely to matter beyond the original seL4 and Orin AGX work.
My current customer-facing industrial platform work is moving toward systems
where multiple computers, real devices, field buses, displays, and safety- or
control-related software all need to be validated together. Autopilot's model
of small adapters, timestamped evidence streams, repeatable chains, and
human-visible AI-driven testing is a natural fit for that kind of environment,
even though the exact target hardware and protocols may differ from the
original Orin AGX setup.

## Footnote: Virtioso

Virtioso is the seL4 virtualization platform that provided Autopilot's main
low-level systems test workload. It is a multi-VM system built on seL4 and
CAmkES: isolated Linux guests run on seL4, standard Linux virtio drivers are
used in driver VMs, and virtio backends are provided by QEMU running inside
another Linux VM. The same platform can also host native seL4 applications as
CAmkES components, alongside or instead of Linux guest functionality. The
broader Virtioso project lives under `https://github.com/virtioso/`. Testing it
requires booting seL4, starting Linux VMs or native components, launching
QEMU-backed virtio services, observing multiple streams, separating platform
failures from guest/component failures, and recovering the target for the next
run.
