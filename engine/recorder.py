"""
ChainRecorder: structured event log for oracle execution.

Records a fixed vocabulary of events to results/<run_id>/events.jsonl.
The fixed vocabulary is the key design decision: an open-ended "log anything"
recorder couples the recorder to oracle internals. A closed vocabulary forces
the engine to expose a clean seam between execution state and observation.

The recorder is passed into the engine at chain start and threaded via
StreamContext metadata (ctx.metadata["recorder"] = recorder). This keeps it
out of the Oracle Protocol signature — oracles that want to emit events access
the recorder via ctx.metadata rather than receiving it as a parameter. Test
code passes NullRecorder or ListRecorder without touching engine internals.

Event format: one JSON object per line (JSONL), written as-is. Readers can
filter by event_type without parsing every field of every line.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Union


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

@dataclass
class OracleStarted:
    oracle_type: str
    stream_name: str | None  # primary stream this oracle reads, if any
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            self.timestamp = time.monotonic()


@dataclass
class OracleVerdict:
    oracle_type: str
    verdict_type: str   # "Matched", "TimeoutVerdict", "Error"
    verdict_label: str | None  # Matched.label or Error.reason; None for TimeoutVerdict
    elapsed: float      # seconds since OracleStarted for this oracle


@dataclass
class StreamBytesRead:
    stream_name: str
    byte_count: int
    offset: int         # byte offset in the stream at time of read


@dataclass
class CleanupHookRan:
    stream_name: str | None   # stream this hook was associated with
    hook_name: str            # descriptive name registered with the hook


Event = Union[OracleStarted, OracleVerdict, StreamBytesRead, CleanupHookRan]


# ---------------------------------------------------------------------------
# Recorder implementations
# ---------------------------------------------------------------------------

class ChainRecorder:
    """
    Writes events to results/<run_id>/events.jsonl.

    Thread-safe for the asyncio single-event-loop model: all writes happen
    from the same event loop, so no locking is needed. The file is kept open
    for the duration of the chain run and flushed after each event so a crash
    does not lose the partial log.
    """

    def __init__(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self._path = run_dir / "events.jsonl"
        self._file = self._path.open("w", encoding="utf-8")

    def emit(self, event: Event) -> None:
        record = {"event_type": type(event).__name__} | asdict(event)
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class NullRecorder:
    """Discards all events. Used in production when recording is disabled."""

    def emit(self, event: Event) -> None:
        pass

    def close(self) -> None:
        pass


class ListRecorder:
    """
    Accumulates events in memory. Used in tests to assert on event sequences.

    Example:
        recorder = ListRecorder()
        ...run oracle...
        assert any(
            isinstance(e, OracleVerdict) and e.verdict_type == "Matched"
            for e in recorder.events
        )
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass

    def verdicts(self) -> list[OracleVerdict]:
        return [e for e in self.events if isinstance(e, OracleVerdict)]

    def of_type(self, event_type: type) -> list[Event]:
        return [e for e in self.events if isinstance(e, event_type)]
