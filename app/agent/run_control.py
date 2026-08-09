"""Who is running, how far they have got, and whether they were told to stop.

One agent turn can take minutes, and until this existed the browser's only
window on it was a spinner - and the only way out was to close the tab, which
did not actually stop anything. This module is the shared ledger the runner
writes and the HTTP layer reads: progress out, cancellation in.

In-memory on purpose. A run dies with the process, so its status should too;
persisting "running" through a restart would report a turn that no longer
exists. One process, one registry - the same assumption the rest of the app
already makes about its SQLite file.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


class RunInProgress(Exception):
    """A second turn was started for a trip that already has one running."""

    def __init__(self, trip_id: str) -> None:
        super().__init__(f"a turn is already running for {trip_id}")
        self.trip_id = trip_id


@dataclass
class RunControl:
    trip_id: str
    started_at: float = field(default_factory=time.monotonic)
    iteration: int = 0
    tools_done: list[dict[str, Any]] = field(default_factory=list)
    current_tool: str | None = None
    current_tool_started: float | None = None
    # The trip revision after the run's most recent commit. Telemetry only -
    # it changes nothing about the run - but it is what lets the screen notice
    # that committed work exists *during* the turn and paint it, instead of
    # sitting on a spinner for two minutes and revealing everything at the end.
    revision: int | None = None
    _cancel: asyncio.Event = field(default_factory=asyncio.Event)

    # -- what the runner tells us -------------------------------------------

    def begin_iteration(self, number: int) -> None:
        self.iteration = number

    def begin_tool(self, name: str) -> None:
        self.current_tool = name
        self.current_tool_started = time.monotonic()

    def end_tool(self, name: str, milliseconds: int, ok: bool) -> None:
        self.tools_done.append({"name": name, "milliseconds": milliseconds, "ok": ok})
        self.current_tool = None
        self.current_tool_started = None

    # -- cancellation --------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    async def wait_cancelled(self) -> None:
        await self._cancel.wait()

    # -- what the browser sees ----------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "running": True,
            "elapsed_ms": int((now - self.started_at) * 1000),
            "iteration": self.iteration,
            "cancelled": self.cancelled,
            "revision": self.revision,
            "current_tool": (
                {
                    "name": self.current_tool,
                    "elapsed_ms": int((now - self.current_tool_started) * 1000),
                }
                if self.current_tool is not None and self.current_tool_started is not None
                else None
            ),
            "tools_done": list(self.tools_done),
        }


_RUNS: dict[str, RunControl] = {}


def begin(trip_id: str) -> RunControl:
    """Register a run, refusing a second for the same trip.

    Two turns interleaving on one trip would race each other's revisions and
    each apply against a state the other is changing - the browser already
    guards this with a busy flag, but a second tab does not share that flag.
    """
    if trip_id in _RUNS:
        raise RunInProgress(trip_id)
    control = _RUNS[trip_id] = RunControl(trip_id)
    return control


def finish(trip_id: str) -> None:
    _RUNS.pop(trip_id, None)


def get(trip_id: str) -> RunControl | None:
    return _RUNS.get(trip_id)
