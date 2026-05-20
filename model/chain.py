"""
Chain schema: Pydantic OracleDef discriminated union + OracleFactory.

A chain is a JSON file containing a single OracleDef tree. The root oracle
is the entire chain — no separate 'entry' / 'steps' graph structure.
Combinators (Sequence, Choice, Race, Timeout, Parallel, Repeat) express
control flow directly; there are no 'next' pointers or backward edges.

Discriminator field: 'oracle' (a Literal string matching the oracle type).

Example chain JSON:
  {
    "oracle": "sequence",
    "steps": [
      { "oracle": "uart_source", "stream": "tty0", "device": "$AUTOPILOT_TTY0" },
      {
        "oracle": "timeout", "seconds": 300,
        "step": {
          "oracle": "choice", "stream": "tty0",
          "options": [
            { "pattern": "ELF-loader started on CPU", "label": "pass" },
            { "pattern": "not recognized as", "label": "fail" }
          ]
        }
      }
    ]
  }

OracleFactory.hydrate(oracle_def) recursively builds live oracle instances.
Adapters register builders via OracleFactory.register(name, builder_fn) at
import time. All built-in oracle types are registered at the bottom of this
module.

Environment variable substitution: string fields that start with '$' are
resolved from os.environ at hydration time (e.g. "$AUTOPILOT_TTY0").
"""

from __future__ import annotations

import os
import re
from typing import Annotated, Callable, Literal, Union

from pydantic import BaseModel, Field

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# OracleDef models
# ---------------------------------------------------------------------------

class VerdictDef(BaseModel):
    oracle: Literal["verdict"]
    label: str = "ok"


class PatternDef(BaseModel):
    oracle: Literal["pattern"]
    stream: str
    pattern: str
    label: str = "matched"
    max_buf: int = 1024 * 1024


class CommandDef(BaseModel):
    oracle: Literal["command"]
    stream: str
    cmd: str
    response_pattern: str
    label: str = "ok"
    suffix: str = "\n"
    max_buf: int = 1024 * 1024


class ChoiceOptionDef(BaseModel):
    pattern: str
    label: str


class ChoiceDef(BaseModel):
    oracle: Literal["choice"]
    stream: str
    options: list[ChoiceOptionDef]
    max_buf: int = 1024 * 1024


class SequenceDef(BaseModel):
    oracle: Literal["sequence"]
    steps: list[OracleDef]


class TimeoutDef(BaseModel):
    oracle: Literal["timeout"]
    seconds: float
    step: OracleDef


class RaceBranchDef(BaseModel):
    stream: str
    step: OracleDef


class RaceDef(BaseModel):
    oracle: Literal["race"]
    branches: list[RaceBranchDef]


class ParallelBranchDef(BaseModel):
    stream: str
    step: OracleDef


class ParallelDef(BaseModel):
    oracle: Literal["parallel"]
    branches: list[ParallelBranchDef]
    reducer: str = "any_matched"


class RepeatMonitorDef(BaseModel):
    oracle: Literal["repeat_monitor"]
    step: OracleDef
    max_iter: int = 0
    backoff: float = 0.0


class RepeatPollDef(BaseModel):
    oracle: Literal["repeat_poll"]
    step: OracleDef
    success_label: str
    max_iter: int = 100
    backoff: float = 1.0


class UARTSourceDef(BaseModel):
    oracle: Literal["uart_source"]
    stream: str
    device: str
    baudrate: int = 115200


class SSHCommandDef(BaseModel):
    oracle: Literal["ssh_command"]
    host: str
    cmd: str
    username: str | None = None
    success_label: str = "ok"
    failure_label: str | None = None
    capture_name: str | None = None


class SSHUploadDef(BaseModel):
    oracle: Literal["ssh_upload"]
    host: str
    src: str
    dst: str
    username: str | None = None


class SpawnProcessDef(BaseModel):
    oracle: Literal["spawn_process"]
    cmd: list[str]
    stream: str
    ready_pattern: str
    ready_label: str = "ready"
    env_extra: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class RunProcessDef(BaseModel):
    oracle: Literal["run_process"]
    cmd: list[str]
    success_label: str = "ok"
    failure_label: str | None = None
    capture_name: str | None = None
    env_extra: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class DockerContainerDef(BaseModel):
    oracle: Literal["docker_container"]
    image: str
    stream: str
    name: str | None = None
    command: str | list[str] | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    network_mode: str = "host"
    ready_pattern: str | None = None
    ready_label: str = "container_ready"


class VCMuxSourceDef(BaseModel):
    oracle: Literal["vcmux_source"]
    stream: str
    nvidia_tcu: bool = False
    registry_streams: list[str] | None = None
    stream_prefix: str = ""
    registry_timeout: float = 30.0


class InteractiveDef(BaseModel):
    oracle: Literal["interactive"]
    stream: str
    session_id: str | None = None
    done_label: str = "interactive_done"


class RFDef(BaseModel):
    oracle: Literal["rf"]
    suite: str
    outputdir: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)
    extra_args: list[str] = Field(default_factory=list)
    verdict_key: str = "rf_verdict"


class ChainRefDef(BaseModel):
    oracle: Literal["chain_ref"]
    path: str  # relative path to another chain JSON file, or $VAR


# ---------------------------------------------------------------------------
# Discriminated union (must be defined AFTER all model classes)
# ---------------------------------------------------------------------------

OracleDef = Annotated[
    Union[
        VerdictDef,
        PatternDef,
        CommandDef,
        ChoiceDef,
        SequenceDef,
        TimeoutDef,
        RaceDef,
        ParallelDef,
        RepeatMonitorDef,
        RepeatPollDef,
        UARTSourceDef,
        SSHCommandDef,
        SSHUploadDef,
        SpawnProcessDef,
        RunProcessDef,
        DockerContainerDef,
        VCMuxSourceDef,
        InteractiveDef,
        RFDef,
        ChainRefDef,
    ],
    Field(discriminator="oracle"),
]

# Rebuild models that reference OracleDef (resolves forward references)
SequenceDef.model_rebuild()
TimeoutDef.model_rebuild()
RaceBranchDef.model_rebuild()
RaceDef.model_rebuild()
ParallelBranchDef.model_rebuild()
ParallelDef.model_rebuild()
RepeatMonitorDef.model_rebuild()
RepeatPollDef.model_rebuild()


# ---------------------------------------------------------------------------
# Root chain model
# ---------------------------------------------------------------------------

class ChainDef(BaseModel):
    """
    Top-level wrapper for a chain JSON file.

    The 'root' field holds the OracleDef tree for the entire chain.
    For simple chains the file can also omit the wrapper and just be
    a bare OracleDef object — load_chain() handles both forms.
    """
    root: OracleDef
    name: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# Environment variable resolution
# ---------------------------------------------------------------------------

def _resolve(value: str) -> str:
    """Substitute $VAR_NAME with its value from os.environ."""
    if value.startswith("$"):
        var = value[1:]
        resolved = os.environ.get(var)
        if resolved is None:
            log.warning("chain.env_var_missing", var=var)
            return value
        return resolved
    return value


def _resolve_list(values: list[str]) -> list[str]:
    return [_resolve(v) for v in values]


# ---------------------------------------------------------------------------
# OracleFactory
# ---------------------------------------------------------------------------

OracleBuilder = Callable[[object, "OracleFactory"], object]  # (def, factory) → oracle


class OracleFactory:
    """
    Two-phase oracle loader: parse JSON → OracleDef, then hydrate → live oracle.

    Register builders via OracleFactory.register("type_name", builder_fn).
    Builder functions receive (oracle_def, factory_instance) and return a
    callable oracle (anything with __call__(ctx, timeout) → (Verdict, ctx)).

    Built-in oracle types are registered below this class definition.
    Adapter-specific types can be registered at adapter import time.
    """

    _registry: dict[str, OracleBuilder] = {}

    @classmethod
    def register(cls, name: str, builder: OracleBuilder) -> None:
        cls._registry[name] = builder

    @classmethod
    def hydrate(cls, oracle_def: OracleDef) -> object:
        name = oracle_def.oracle  # type: ignore[union-attr]
        builder = cls._registry.get(name)
        if builder is None:
            raise ValueError(
                f"No builder registered for oracle type '{name}'. "
                f"Known types: {sorted(cls._registry)}"
            )
        return builder(oracle_def, cls)

    @classmethod
    def parse(cls, data: dict) -> OracleDef:
        """Parse a raw dict (from JSON) into an OracleDef."""
        from pydantic import TypeAdapter
        adapter: TypeAdapter[OracleDef] = TypeAdapter(OracleDef)
        return adapter.validate_python(data)


# ---------------------------------------------------------------------------
# Built-in builders
# ---------------------------------------------------------------------------

def _build_verdict(d: VerdictDef, f: OracleFactory):
    from engine.primitives import VerdictOracle
    return VerdictOracle(label=d.label)


def _build_pattern(d: PatternDef, f: OracleFactory):
    from engine.primitives import PatternOracle
    return PatternOracle(
        stream=d.stream,
        pattern=d.pattern.encode() if isinstance(d.pattern, str) else d.pattern,
        label=d.label,
        max_buf=d.max_buf,
    )


def _build_command(d: CommandDef, f: OracleFactory):
    from engine.primitives import CommandOracle
    return CommandOracle(
        stream=d.stream,
        cmd=d.cmd.encode(),
        response_pattern=d.response_pattern.encode(),
        label=d.label,
        suffix=d.suffix.encode(),
        max_buf=d.max_buf,
    )


def _build_choice(d: ChoiceDef, f: OracleFactory):
    from engine.combinators import Choice, ChoiceOption
    options = [
        ChoiceOption(pattern=re.compile(opt.pattern.encode()), label=opt.label)
        for opt in d.options
    ]
    return Choice(stream=d.stream, options=options, max_buf=d.max_buf)


def _build_sequence(d: SequenceDef, f: OracleFactory):
    from engine.combinators import Sequence
    return Sequence(steps=[f.hydrate(step) for step in d.steps])


def _build_timeout(d: TimeoutDef, f: OracleFactory):
    from engine.combinators import Timeout
    return Timeout(oracle=f.hydrate(d.step), t=d.seconds)


def _build_race(d: RaceDef, f: OracleFactory):
    from engine.combinators import Race
    branches = [(f.hydrate(b.step), b.stream) for b in d.branches]
    return Race(branches=branches)


def _build_parallel(d: ParallelDef, f: OracleFactory):
    from engine.combinators import Parallel
    from engine.oracle import Matched

    reducers = {
        "any_matched": lambda verdicts: next(
            (v for v in verdicts if isinstance(v, Matched)), verdicts[0]
        ),
        "all_matched": lambda verdicts: (
            verdicts[-1]
            if all(isinstance(v, Matched) for v in verdicts)
            else next(v for v in verdicts if not isinstance(v, Matched))
        ),
    }
    reducer = reducers.get(d.reducer)
    if reducer is None:
        raise ValueError(f"Unknown parallel reducer: {d.reducer!r}")
    branches = [(f.hydrate(b.step), b.stream) for b in d.branches]
    return Parallel(branches=branches, reducer=reducer)


def _build_repeat_monitor(d: RepeatMonitorDef, f: OracleFactory):
    from engine.combinators import Repeat
    return Repeat.monitor(
        oracle=f.hydrate(d.step),
        max_iter=d.max_iter,
        backoff=d.backoff,
    )


def _build_repeat_poll(d: RepeatPollDef, f: OracleFactory):
    from engine.combinators import Repeat
    return Repeat.poll(
        oracle=f.hydrate(d.step),
        success_label=d.success_label,
        max_iter=d.max_iter,
        backoff=d.backoff,
    )


def _build_uart_source(d: UARTSourceDef, f: OracleFactory):
    from adapters.uart import UARTSourceOracle
    return UARTSourceOracle(
        stream_name=d.stream,
        device=_resolve(d.device),
        baudrate=d.baudrate,
    )


def _build_ssh_command(d: SSHCommandDef, f: OracleFactory):
    from adapters.ssh import SSHCommandOracle
    kwargs = dict(
        host=_resolve(d.host),
        cmd=d.cmd,
        success_label=d.success_label,
        failure_label=d.failure_label,
        capture_name=d.capture_name,
    )
    if d.username is not None:
        kwargs["username"] = d.username
    return SSHCommandOracle(**kwargs)


def _build_ssh_upload(d: SSHUploadDef, f: OracleFactory):
    from adapters.ssh import SSHUploadOracle
    from pathlib import Path
    kwargs = dict(
        host=_resolve(d.host),
        src=Path(_resolve(d.src)),
        dst=_resolve(d.dst),
    )
    if d.username is not None:
        kwargs["username"] = d.username
    return SSHUploadOracle(**kwargs)


def _build_spawn_process(d: SpawnProcessDef, f: OracleFactory):
    from adapters.process import SpawnProcessOracle
    from pathlib import Path
    return SpawnProcessOracle(
        cmd=_resolve_list(d.cmd),
        stream_name=d.stream,
        ready_pattern=d.ready_pattern,
        ready_label=d.ready_label,
        env_extra={k: _resolve(v) for k, v in d.env_extra.items()},
        cwd=Path(d.cwd) if d.cwd else None,
    )


def _build_run_process(d: RunProcessDef, f: OracleFactory):
    from adapters.process import RunProcessOracle
    from pathlib import Path
    return RunProcessOracle(
        cmd=_resolve_list(d.cmd),
        success_label=d.success_label,
        failure_label=d.failure_label,
        capture_name=d.capture_name,
        env_extra={k: _resolve(v) for k, v in d.env_extra.items()},
        cwd=Path(d.cwd) if d.cwd else None,
    )


def _build_docker_container(d: DockerContainerDef, f: OracleFactory):
    from adapters.docker import DockerContainerOracle
    return DockerContainerOracle(
        image=d.image,
        stream_name=d.stream,
        name=d.name,
        command=d.command,
        environment=d.environment,
        network_mode=d.network_mode,
        ready_pattern=d.ready_pattern,
        ready_label=d.ready_label,
    )


def _build_vcmux_source(d: VCMuxSourceDef, f: OracleFactory):
    from adapters.vcmux import VCMuxSourceOracle
    return VCMuxSourceOracle(
        raw_stream_name=d.stream,
        nvidia_tcu=d.nvidia_tcu,
        registry_streams=d.registry_streams,
        stream_prefix=d.stream_prefix,
        registry_timeout=d.registry_timeout,
    )


def _build_interactive(d: InteractiveDef, f: OracleFactory):
    from adapters.interactive import InteractiveOracle
    return InteractiveOracle(
        stream_name=d.stream,
        session_id=d.session_id,
        done_label=d.done_label,
    )


def _build_rf(d: RFDef, f: OracleFactory):
    from adapters.robot import RobotFrameworkOracle
    from pathlib import Path
    return RobotFrameworkOracle(
        suite=Path(d.suite),
        outputdir=Path(d.outputdir) if d.outputdir else None,
        variables=d.variables,
        extra_args=d.extra_args,
        verdict_key=d.verdict_key,
    )


def _build_chain_ref(d: ChainRefDef, f: OracleFactory):
    """Load and hydrate a referenced chain file at hydration time."""
    from pathlib import Path
    from engine.runtime import load_chain, hydrate_chain

    resolved_path = Path(_resolve(d.path))
    oracle_def = load_chain(resolved_path)
    return hydrate_chain(oracle_def)


# Register all built-in builders
_BUILDERS = {
    "verdict": _build_verdict,
    "pattern": _build_pattern,
    "command": _build_command,
    "choice": _build_choice,
    "sequence": _build_sequence,
    "timeout": _build_timeout,
    "race": _build_race,
    "parallel": _build_parallel,
    "repeat_monitor": _build_repeat_monitor,
    "repeat_poll": _build_repeat_poll,
    "uart_source": _build_uart_source,
    "ssh_command": _build_ssh_command,
    "ssh_upload": _build_ssh_upload,
    "spawn_process": _build_spawn_process,
    "run_process": _build_run_process,
    "docker_container": _build_docker_container,
    "vcmux_source": _build_vcmux_source,
    "interactive": _build_interactive,
    "rf": _build_rf,
    "chain_ref": _build_chain_ref,
}

for _name, _builder in _BUILDERS.items():
    OracleFactory.register(_name, _builder)
