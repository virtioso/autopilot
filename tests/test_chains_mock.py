"""
Mock byte-sequence tests for migrated chain files.

Verifies that each chain parses correctly and, where possible without hardware,
executes against synthetic byte streams that mimic real hardware output.

Hardware-dependent chains (uart_source, relay, ssh_command, uefi_shell_run,
spawn_process) are tested for schema correctness only — execution tests are
covered by hardware parity runs.

Chain coverage:
  Tier 0: post_test_fallback_noop, wait_for_elfloader — see test_chain.py
  Tier 1: startup, monitor_ftrace_noop, monitor_ftrace_storage_full,
           vm_wait_boot_minimal, recovery_boot
  Tier 2: post_test_fallback_relay, boot_stock_linux, deploy_and_boot_test_efi,
           sel4test, boot-interactive, boot-interactive-efi, isengard-docker,
           isengard_deploy_and_boot, isengard-linux-orin*, qemu_*defconfig
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from engine.combinators import Timeout
from engine.oracle import Error, Matched, StreamContext, TimeoutVerdict

CHAINS_DIR = Path(__file__).parent.parent / "chains"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockBiStream:
    """Feed fixed bytes then stall; records writes."""
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._reader: asyncio.StreamReader | None = None
        self.written: list[bytes] = []

    def _get_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            self._reader = asyncio.StreamReader()
            self._reader.feed_data(self._data)
            self._reader.feed_eof()
        return self._reader

    async def read(self, n: int = 4096) -> bytes:
        return await self._get_reader().read(n)

    async def write(self, data: bytes) -> None:
        self.written.append(data)


def make_ctx(**streams) -> StreamContext:
    return StreamContext(streams=dict(streams))


def load_and_parse(name: str):
    from model.chain import OracleFactory
    data = json.loads((CHAINS_DIR / name).read_text())
    return OracleFactory.parse(data)


def load_and_hydrate(name: str):
    from model.chain import OracleFactory
    d = load_and_parse(name)
    return OracleFactory.hydrate(d)


# ---------------------------------------------------------------------------
# Tier 1: startup.json
# ---------------------------------------------------------------------------

def test_startup_schema():
    from model.chain import SequenceDef, UARTSourceDef, VerdictDef
    d = load_and_parse("startup.json")
    assert isinstance(d, SequenceDef)
    assert len(d.steps) == 3
    assert isinstance(d.steps[0], UARTSourceDef)
    assert d.steps[0].stream == "tty0"
    assert isinstance(d.steps[1], UARTSourceDef)
    assert d.steps[1].stream == "tty1"
    assert isinstance(d.steps[2], VerdictDef)
    assert d.steps[2].label == "pass"


# ---------------------------------------------------------------------------
# Tier 1: monitor_ftrace_noop.json
# ---------------------------------------------------------------------------

def test_monitor_ftrace_noop_schema():
    from model.chain import PatternDef, RepeatMonitorDef
    d = load_and_parse("monitor_ftrace_noop.json")
    assert isinstance(d, RepeatMonitorDef)
    assert isinstance(d.step, PatternDef)
    assert d.step.stream == "tty0"
    assert "AUTOPILOT_INTERNAL_NEVER_MATCH" in d.step.pattern


# ---------------------------------------------------------------------------
# Tier 1: monitor_ftrace_storage_full.json
# ---------------------------------------------------------------------------

def test_monitor_ftrace_storage_full_schema():
    from model.chain import PatternDef
    d = load_and_parse("monitor_ftrace_storage_full.json")
    assert isinstance(d, PatternDef)
    assert d.stream == "tty0"
    assert d.pattern == "FTRACE: Storage full"
    assert d.label == "fail"


async def test_monitor_ftrace_storage_full_matches():
    oracle = load_and_hydrate("monitor_ftrace_storage_full.json")
    ctx = make_ctx(tty0=MockBiStream(b"[ftrace] cycle 47\r\nFTRACE: Storage full\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("fail")


async def test_monitor_ftrace_storage_full_no_match_eof():
    oracle = load_and_hydrate("monitor_ftrace_storage_full.json")
    ctx = make_ctx(tty0=MockBiStream(b"[ftrace] all is well\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert isinstance(verdict, Error)


# ---------------------------------------------------------------------------
# Tier 1: vm_wait_boot_minimal.json
# ---------------------------------------------------------------------------

def test_vm_wait_boot_minimal_schema():
    from model.chain import PatternDef, TimeoutDef
    d = load_and_parse("vm_wait_boot_minimal.json")
    assert isinstance(d, TimeoutDef)
    assert d.seconds == 120
    assert isinstance(d.step, PatternDef)
    assert d.step.pattern == "driver-vm login:"
    assert d.step.label == "pass"


async def test_vm_wait_boot_minimal_pass():
    oracle = load_and_hydrate("vm_wait_boot_minimal.json")
    ctx = make_ctx(tty0=MockBiStream(
        b"[  OK  ] Reached target Multi-User System.\r\ndriver-vm login: \r\n"
    ))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("pass")


async def test_vm_wait_boot_minimal_timeout():
    class StallStream:
        async def read(self, n=4096):
            await asyncio.sleep(100)
            return b""
        async def write(self, data): pass

    oracle = load_and_hydrate("vm_wait_boot_minimal.json")
    ctx = make_ctx(tty0=StallStream())
    wrapped = Timeout(oracle, 0.05)
    verdict, _ = await wrapped(ctx, 0.05)
    assert isinstance(verdict, TimeoutVerdict)


# ---------------------------------------------------------------------------
# Tier 1: recovery_boot.json
# ---------------------------------------------------------------------------

def test_recovery_boot_schema():
    from model.chain import ChainRefDef, SequenceDef
    d = load_and_parse("recovery_boot.json")
    assert isinstance(d, SequenceDef)
    assert len(d.steps) == 2
    assert isinstance(d.steps[0], ChainRefDef)
    assert isinstance(d.steps[1], ChainRefDef)
    assert "startup" in d.steps[0].path
    assert "boot_stock_linux" in d.steps[1].path


# ---------------------------------------------------------------------------
# Tier 2: post_test_fallback_relay.json
# ---------------------------------------------------------------------------

def test_post_test_fallback_relay_schema():
    from model.chain import RelayDef
    d = load_and_parse("post_test_fallback_relay.json")
    assert isinstance(d, RelayDef)
    assert d.action == "boot"


# ---------------------------------------------------------------------------
# Tier 2: boot_stock_linux.json
# ---------------------------------------------------------------------------

def test_boot_stock_linux_schema():
    from model.chain import SequenceDef, TimeoutDef, UEFIShellRunDef
    d = load_and_parse("boot_stock_linux.json")
    assert isinstance(d, SequenceDef)
    # First step: timeout wrapping uefi_shell_run
    step0 = d.steps[0]
    assert isinstance(step0, TimeoutDef)
    assert isinstance(step0.step, UEFIShellRunDef)
    assert step0.step.stream == "tty0"
    assert "BOOTAA64" in step0.step.binary or "BOOTAA64" in step0.step.binary.upper()
    # Success pattern must mention L4TLauncher
    assert "L4TLauncher" in (step0.step.success_pattern or "")


# ---------------------------------------------------------------------------
# Tier 2: deploy_and_boot_test_efi.json
# ---------------------------------------------------------------------------

def test_deploy_and_boot_test_efi_schema():
    from model.chain import RelayDef, SSHCommandDef, SSHUploadDef, SequenceDef, TimeoutDef, UEFIShellRunDef
    d = load_and_parse("deploy_and_boot_test_efi.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "SSHCommandDef" in types
    assert "SSHUploadDef" in types
    assert "RelayDef" in types
    assert "TimeoutDef" in types
    assert "VerdictDef" in types
    # The timeout step wraps uefi_shell_run
    for s in d.steps:
        if isinstance(s, TimeoutDef):
            assert isinstance(s.step, UEFIShellRunDef)
            break


# ---------------------------------------------------------------------------
# Tier 2: sel4test.json (complete)
# ---------------------------------------------------------------------------

def test_sel4test_schema_complete():
    from model.chain import ChainRefDef, SequenceDef, TimeoutDef, UARTSourceDef
    d = load_and_parse("sel4test.json")
    assert isinstance(d, SequenceDef)
    steps = d.steps
    # Must start with uart_source
    assert isinstance(steps[0], UARTSourceDef)
    assert steps[0].stream == "tty0"
    # Must contain chain_refs to boot_stock_linux and deploy_and_boot_test_efi
    refs = [s.path for s in steps if isinstance(s, ChainRefDef)]
    assert any("boot_stock_linux" in r for r in refs)
    assert any("deploy_and_boot_test_efi" in r for r in refs)
    # Must have timeout steps for elfloader wait and pass/fail choice
    timeouts = [s for s in steps if isinstance(s, TimeoutDef)]
    assert len(timeouts) >= 2


# ---------------------------------------------------------------------------
# Tier 2: boot-interactive.json
# ---------------------------------------------------------------------------

def test_boot_interactive_schema():
    from model.chain import ChainRefDef, InteractiveDef, RelayDef, SequenceDef, UARTSourceDef, VerdictDef
    d = load_and_parse("boot-interactive.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "UARTSourceDef" in types
    assert "RelayDef" in types
    assert "ChainRefDef" in types
    assert "InteractiveDef" in types
    assert "VerdictDef" in types


# ---------------------------------------------------------------------------
# Tier 2: boot-interactive-efi.json
# ---------------------------------------------------------------------------

def test_boot_interactive_efi_schema():
    from model.chain import ChainRefDef, InteractiveDef, RelayDef, SequenceDef, TimeoutDef, UARTSourceDef
    d = load_and_parse("boot-interactive-efi.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "UARTSourceDef" in types
    assert "RelayDef" in types
    assert "ChainRefDef" in types
    assert "TimeoutDef" in types
    assert "InteractiveDef" in types
    # Two chain_refs: boot_stock_linux + deploy_and_boot_test_efi
    refs = [s.path for s in d.steps if isinstance(s, ChainRefDef)]
    assert len(refs) == 2


# ---------------------------------------------------------------------------
# Tier 2: isengard-docker.json
# ---------------------------------------------------------------------------

def test_isengard_docker_schema():
    from model.chain import SequenceDef, SpawnProcessDef, TimeoutDef, VerdictDef
    d = load_and_parse("isengard-docker.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "SpawnProcessDef" in types
    assert "TimeoutDef" in types
    assert "VerdictDef" in types


# ---------------------------------------------------------------------------
# Tier 2: isengard_deploy_and_boot.json (shared sub-chain)
# ---------------------------------------------------------------------------

def test_isengard_deploy_and_boot_schema():
    from model.chain import RelayDef, SSHCommandDef, SSHUploadDef, SequenceDef, TimeoutDef, UEFIShellRunDef
    d = load_and_parse("isengard_deploy_and_boot.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "SSHCommandDef" in types
    assert "SSHUploadDef" in types
    assert "RelayDef" in types
    assert "TimeoutDef" in types
    # uefi_shell_run inside a timeout
    for s in d.steps:
        if isinstance(s, TimeoutDef):
            assert isinstance(s.step, UEFIShellRunDef)
            break


# ---------------------------------------------------------------------------
# Tier 2: isengard-linux-orin.json
# ---------------------------------------------------------------------------

def test_isengard_linux_orin_schema():
    from model.chain import ChainRefDef, RelayDef, SequenceDef, UARTSourceDef, VerdictDef
    d = load_and_parse("isengard-linux-orin.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "UARTSourceDef" in types
    assert "RelayDef" in types
    refs = [s.path for s in d.steps if isinstance(s, ChainRefDef)]
    assert any("boot_stock_linux" in r for r in refs)
    assert any("isengard_deploy_and_boot" in r for r in refs)
    assert "VerdictDef" in types


# ---------------------------------------------------------------------------
# Tier 2: isengard-linux-orin-demo.json
# ---------------------------------------------------------------------------

def test_isengard_linux_orin_demo_schema():
    from model.chain import RepeatPollDef, SSHCommandDef, SequenceDef, VerdictDef
    d = load_and_parse("isengard-linux-orin-demo.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "RepeatPollDef" in types
    assert "SSHCommandDef" in types
    assert "VerdictDef" in types
    # SSH commands include demo-start and state verification
    ssh_cmds = [s.cmd for s in d.steps if isinstance(s, SSHCommandDef)]
    assert any("isengard-demo-start" in cmd for cmd in ssh_cmds)


# ---------------------------------------------------------------------------
# Tier 2: isengard-linux-orin-interactive.json
# ---------------------------------------------------------------------------

def test_isengard_linux_orin_interactive_schema():
    from model.chain import ChainRefDef, InteractiveDef, SequenceDef, UARTSourceDef, VerdictDef
    d = load_and_parse("isengard-linux-orin-interactive.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "UARTSourceDef" in types
    assert "ChainRefDef" in types
    assert "InteractiveDef" in types
    assert "VerdictDef" in types
    # ssh_wait_ready is inside isengard_deploy_and_boot chain_ref, not at top level
    refs = [s.path for s in d.steps if isinstance(s, ChainRefDef)]
    assert any("isengard_deploy_and_boot" in r for r in refs)


# ---------------------------------------------------------------------------
# Tier 2: isengard-linux-orin-rust-demo.json
# ---------------------------------------------------------------------------

def test_isengard_linux_orin_rust_demo_schema():
    from model.chain import RepeatPollDef, SSHCommandDef, SequenceDef, VerdictDef
    d = load_and_parse("isengard-linux-orin-rust-demo.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "RepeatPollDef" in types
    assert "SSHCommandDef" in types
    assert "VerdictDef" in types
    ssh_cmds = [s.cmd for s in d.steps if isinstance(s, SSHCommandDef)]
    assert any("isengard-demo-start" in cmd for cmd in ssh_cmds)
    # Rust demo: must have isengard-demo-watch-rust command
    assert any("isengard-demo-watch-rust" in cmd for cmd in ssh_cmds)


# ---------------------------------------------------------------------------
# Tier 2: qemu_arm64_defconfig.json
# ---------------------------------------------------------------------------

def test_qemu_arm64_defconfig_schema():
    from model.chain import ChoiceDef, SequenceDef, SpawnProcessDef, TimeoutDef
    d = load_and_parse("qemu_arm64_defconfig.json")
    assert isinstance(d, SequenceDef)
    assert len(d.steps) == 2
    spawn, timeout = d.steps
    assert isinstance(spawn, SpawnProcessDef)
    assert spawn.stream == "tty0"
    assert "qemu_arm64_defconfig" in " ".join(str(a) for a in spawn.cmd)
    assert isinstance(timeout, TimeoutDef)
    assert timeout.seconds == 420
    assert isinstance(timeout.step, ChoiceDef)
    labels = [o.label for o in timeout.step.options]
    assert "pass" in labels
    assert "fail" in labels


async def test_qemu_arm64_defconfig_pass_pattern():
    """Verify the pass pattern matches real seL4test success string."""
    from model.chain import OracleFactory
    data = json.loads((CHAINS_DIR / "qemu_arm64_defconfig.json").read_text())
    # Extract and test only the timeout(choice) part
    timeout_def = OracleFactory.parse(data["steps"][1])
    oracle = OracleFactory.hydrate(timeout_def)
    ctx = make_ctx(tty0=MockBiStream(b"All is well in the universe\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("pass")


async def test_qemu_arm64_defconfig_fail_pattern():
    from model.chain import OracleFactory
    data = json.loads((CHAINS_DIR / "qemu_arm64_defconfig.json").read_text())
    timeout_def = OracleFactory.parse(data["steps"][1])
    oracle = OracleFactory.hydrate(timeout_def)
    ctx = make_ctx(tty0=MockBiStream(b"*** FAILURES DETECTED ***\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("fail")


# ---------------------------------------------------------------------------
# Tier 2: qemu_x86_64_defconfig.json
# ---------------------------------------------------------------------------

def test_qemu_x86_64_defconfig_schema():
    from model.chain import ChoiceDef, RepeatPollDef, SequenceDef, SpawnProcessDef, TimeoutDef
    d = load_and_parse("qemu_x86_64_defconfig.json")
    assert isinstance(d, SequenceDef)
    assert len(d.steps) == 3
    poll, spawn, timeout = d.steps
    assert isinstance(poll, RepeatPollDef)
    assert isinstance(spawn, SpawnProcessDef)
    assert spawn.stream == "tty0"
    assert "qemu_x86_64_defconfig" in " ".join(str(a) for a in spawn.cmd)
    assert isinstance(timeout, TimeoutDef)
    assert isinstance(timeout.step, ChoiceDef)
    labels = [o.label for o in timeout.step.options]
    assert "pass" in labels
    assert "fail" in labels


async def test_qemu_x86_64_defconfig_pass_pattern():
    from model.chain import OracleFactory
    data = json.loads((CHAINS_DIR / "qemu_x86_64_defconfig.json").read_text())
    timeout_def = OracleFactory.parse(data["steps"][2])
    oracle = OracleFactory.hydrate(timeout_def)
    ctx = make_ctx(tty0=MockBiStream(b"driver-vm /dev/ttyS0\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("pass")


async def test_qemu_x86_64_defconfig_fail_pattern():
    from model.chain import OracleFactory
    data = json.loads((CHAINS_DIR / "qemu_x86_64_defconfig.json").read_text())
    timeout_def = OracleFactory.parse(data["steps"][2])
    oracle = OracleFactory.hydrate(timeout_def)
    ctx = make_ctx(tty0=MockBiStream(b"Kernel panic - not syncing\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("fail")


async def test_qemu_x86_64_defconfig_all_is_well_pattern():
    """x86 chain also matches the standard seL4test pass string."""
    from model.chain import OracleFactory
    data = json.loads((CHAINS_DIR / "qemu_x86_64_defconfig.json").read_text())
    timeout_def = OracleFactory.parse(data["steps"][2])
    oracle = OracleFactory.hydrate(timeout_def)
    ctx = make_ctx(tty0=MockBiStream(b"All is well in the universe\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("pass")


# ---------------------------------------------------------------------------
# Tier 3: vm_common_setup.json
# ---------------------------------------------------------------------------

def test_vm_common_setup_schema():
    from model.chain import ChainRefDef, SequenceDef, TimeoutDef, UARTSourceDef
    d = load_and_parse("vm_common_setup.json")
    assert isinstance(d, SequenceDef)
    assert len(d.steps) == 4
    assert isinstance(d.steps[0], UARTSourceDef)
    assert d.steps[0].stream == "tty0"
    assert isinstance(d.steps[1], UARTSourceDef)
    assert d.steps[1].stream == "tty1"
    assert isinstance(d.steps[2], ChainRefDef)
    assert "deploy_and_boot_test_efi" in d.steps[2].path
    assert isinstance(d.steps[3], TimeoutDef)
    assert d.steps[3].seconds == 300


# ---------------------------------------------------------------------------
# Tier 3: vm_common.json
# ---------------------------------------------------------------------------

def test_vm_common_schema():
    from model.chain import ChainRefDef, PatternDef, RunProcessDef, SequenceDef, TimeoutDef, VCMuxSourceDef, VerdictDef
    d = load_and_parse("vm_common.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "ChainRefDef" in types
    assert "VCMuxSourceDef" in types
    assert "TimeoutDef" in types
    assert "RunProcessDef" in types
    assert "VerdictDef" in types
    # VCMux on tty0 with nvidia_tcu=True
    vcmux = next(s for s in d.steps if isinstance(s, VCMuxSourceDef))
    assert vcmux.stream == "tty0"
    assert vcmux.nvidia_tcu is True
    # Wait pattern on vm0_guest_console_sink
    timeout = next(s for s in d.steps if isinstance(s, TimeoutDef))
    assert isinstance(timeout.step, PatternDef)
    assert timeout.step.stream == "vm0_guest_console_sink"


# ---------------------------------------------------------------------------
# Tier 3: vm-minimal.json
# ---------------------------------------------------------------------------

def test_vm_minimal_schema():
    from model.chain import ChainRefDef
    d = load_and_parse("vm-minimal.json")
    assert isinstance(d, ChainRefDef)
    assert "vm_common" in d.path


# ---------------------------------------------------------------------------
# Tier 3: vm_wait_boot_qemu_virtio.json
# ---------------------------------------------------------------------------

def test_vm_wait_boot_qemu_virtio_schema():
    from model.chain import CommandDef, SequenceDef, TimeoutDef, VerdictDef
    d = load_and_parse("vm_wait_boot_qemu_virtio.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "TimeoutDef" in types
    assert "CommandDef" in types
    assert "VerdictDef" in types
    # Commands must use split streams (write_stream != stream)
    cmds = [s for s in d.steps if isinstance(s, CommandDef)]
    split_cmds = [c for c in cmds if c.write_stream is not None]
    assert len(split_cmds) > 0
    # vm0 writes go to vm0, reads from vm0_guest_console_sink
    vm0_cmds = [c for c in cmds if c.write_stream == "vm0"]
    assert len(vm0_cmds) > 0
    assert all(c.stream == "vm0_guest_console_sink" for c in vm0_cmds)
    # vm1 writes go to vm1, reads from vm1_guest_console_sink
    vm1_cmds = [c for c in cmds if c.write_stream == "vm1"]
    assert len(vm1_cmds) > 0
    assert all(c.stream == "vm1_guest_console_sink" for c in vm1_cmds)


# ---------------------------------------------------------------------------
# Tier 3: vm-qemu-virtio.json
# ---------------------------------------------------------------------------

def test_vm_qemu_virtio_schema():
    from model.chain import ChainRefDef, RunProcessDef, SequenceDef, VCMuxSourceDef, VerdictDef
    d = load_and_parse("vm-qemu-virtio.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "ChainRefDef" in types
    assert "VCMuxSourceDef" in types
    assert "RunProcessDef" in types
    assert "VerdictDef" in types
    refs = [s.path for s in d.steps if isinstance(s, ChainRefDef)]
    assert any("vm_common_setup" in r for r in refs)
    assert any("vm_wait_boot_qemu_virtio" in r for r in refs)


# ---------------------------------------------------------------------------
# Tier 3: vm_common_orin_virtioso_mux.json
# ---------------------------------------------------------------------------

def test_vm_common_orin_virtioso_mux_schema():
    from model.chain import ChainRefDef, PatternDef, RunProcessDef, SequenceDef, TimeoutDef, UARTSourceDef, VCMuxSourceDef, VerdictDef
    d = load_and_parse("vm_common_orin_virtioso_mux.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "UARTSourceDef" in types
    assert "VCMuxSourceDef" in types
    assert "TimeoutDef" in types
    assert "RunProcessDef" in types
    assert "VerdictDef" in types
    # VCMux on tty1, raw mode (nvidia_tcu=False)
    vcmux = next(s for s in d.steps if isinstance(s, VCMuxSourceDef))
    assert vcmux.stream == "tty1"
    assert vcmux.nvidia_tcu is False


# ---------------------------------------------------------------------------
# Tier 3: linux-kernel.json and linux-kernel-multi.json
# ---------------------------------------------------------------------------

def test_linux_kernel_schema():
    from model.chain import ChainRefDef, ExtlinuxBootDef, InteractiveDef, RelayDef, SSHCommandDef, SSHUploadDef, SequenceDef, TimeoutDef, UARTSourceDef, VerdictDef
    d = load_and_parse("linux-kernel.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "UARTSourceDef" in types
    assert "RelayDef" in types
    assert "ChainRefDef" in types
    assert "InteractiveDef" in types
    assert "SSHUploadDef" in types
    assert "SSHCommandDef" in types
    assert "TimeoutDef" in types
    assert "VerdictDef" in types
    # extlinux_boot wrapped in timeout
    timeouts = [s for s in d.steps if isinstance(s, TimeoutDef)]
    extlinux_steps = [t for t in timeouts if isinstance(t.step, ExtlinuxBootDef)]
    assert len(extlinux_steps) == 1
    assert extlinux_steps[0].step.entry == 2


def test_linux_kernel_multi_schema():
    from model.chain import ExtlinuxBootDef, SSHUploadDef, SequenceDef, TimeoutDef
    d = load_and_parse("linux-kernel-multi.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "SSHUploadDef" in types
    assert "TimeoutDef" in types


async def test_linux_kernel_pass_pattern():
    """Boot success pattern: Ubuntu login prompt."""
    from model.chain import OracleFactory
    data = json.loads((CHAINS_DIR / "linux-kernel.json").read_text())
    # Last timeout step = wait_kernel choice
    timeout_def = OracleFactory.parse(data["steps"][-2])
    oracle = OracleFactory.hydrate(timeout_def)
    ctx = make_ctx(tty0=MockBiStream(b"ubuntu@tegra-ubuntu:~$ \r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("pass")


async def test_linux_kernel_fail_pattern():
    from model.chain import OracleFactory
    data = json.loads((CHAINS_DIR / "linux-kernel.json").read_text())
    timeout_def = OracleFactory.parse(data["steps"][-2])
    oracle = OracleFactory.hydrate(timeout_def)
    ctx = make_ctx(tty0=MockBiStream(b"Kernel panic - not syncing: Oops\r\n"))
    verdict, _ = await oracle(ctx, 5.0)
    assert verdict == Matched("fail")


# ---------------------------------------------------------------------------
# Tier 3: qemu_x86_64_vm_qemu_virtio_* chains
# ---------------------------------------------------------------------------

def test_qemu_x86_64_vm_qemu_virtio_banner_cr_probe_schema():
    from model.chain import CommandDef, RepeatPollDef, SequenceDef, SpawnProcessDef, TimeoutDef, VCMuxSourceDef, VerdictDef
    d = load_and_parse("qemu_x86_64_vm_qemu_virtio_banner_cr_probe.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "RepeatPollDef" in types
    assert "SpawnProcessDef" in types
    assert "VCMuxSourceDef" in types
    assert "TimeoutDef" in types
    assert "CommandDef" in types
    assert "VerdictDef" in types
    # vcmux on tty0
    vcmux = next(s for s in d.steps if isinstance(s, VCMuxSourceDef))
    assert vcmux.stream == "tty0"


def test_qemu_x86_64_vm_qemu_virtio_login_probe_schema():
    from model.chain import CommandDef, RepeatPollDef, SequenceDef, SpawnProcessDef, VCMuxSourceDef, VerdictDef
    d = load_and_parse("qemu_x86_64_vm_qemu_virtio_login_probe.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "RepeatPollDef" in types
    assert "SpawnProcessDef" in types
    assert "VCMuxSourceDef" in types
    assert "CommandDef" in types
    assert "VerdictDef" in types
    # probe command must include the sentinel string
    cmds = [s for s in d.steps if isinstance(s, CommandDef)]
    assert any("__AUTOPILOT_PROBE_AFTER_ROOT__" in c.cmd for c in cmds)


def test_qemu_x86_64_vm_qemu_virtio_minimal_login_schema():
    from model.chain import CommandDef, RepeatPollDef, SequenceDef, SpawnProcessDef, VCMuxSourceDef, VerdictDef
    d = load_and_parse("qemu_x86_64_vm_qemu_virtio_minimal_login.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "RepeatPollDef" in types
    assert "SpawnProcessDef" in types
    assert "VCMuxSourceDef" in types
    assert "CommandDef" in types
    assert "VerdictDef" in types


def test_qemu_x86_64_vm_qemu_virtio_uservm_schema():
    from model.chain import CommandDef, RepeatPollDef, SequenceDef, SpawnProcessDef, TimeoutDef, VCMuxSourceDef, VerdictDef
    d = load_and_parse("qemu_x86_64_vm_qemu_virtio_uservm.json")
    assert isinstance(d, SequenceDef)
    types = [type(s).__name__ for s in d.steps]
    assert "RepeatPollDef" in types
    assert "SpawnProcessDef" in types
    assert "VCMuxSourceDef" in types
    assert "TimeoutDef" in types
    assert "CommandDef" in types
    assert "VerdictDef" in types
    # uservm chain uses user_vm_console stream
    cmds = [s for s in d.steps if isinstance(s, CommandDef)]
    user_vm_cmds = [c for c in cmds if "user_vm_console" in (c.stream or "")]
    assert len(user_vm_cmds) > 0
    # uname command verifies user VM identity
    assert any("uname" in c.cmd for c in cmds)
