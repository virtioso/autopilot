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
