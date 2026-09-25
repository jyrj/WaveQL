"""The three tool surfaces the ablation compares.

The experiment holds everything constant except the interface to the evidence:
same task, same stimulus, same model, same budget, same success contract. Only
the tools differ, because the tools are the independent variable.

The control arm is deliberately NOT a strawman. It gets exactly the evidence a
competent engineer has today with an agentic loop: the simulator's stdout, the
disassembled commit trace, and the cospike first-mismatch report -- the same
artifacts, byte for byte, that the WaveQL arm's store was built from. What it
lacks is the join and the ability to ask a bounded question; it must read.

That distinction is the whole experiment. If a text arm with the same bytes does
as well, WaveQL is not worth building, and this harness is designed so that
result would show up honestly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from waveql.agent.source import (
    SourceView,
    source_declarations,
    source_dispatch,
)
from waveql.store.store import Result
from waveql.tool.api import WaveQLTool, declarations as waveql_declarations, dispatch as waveql_dispatch

ARMS = ("control", "waveql")

MAX_LINES = 200          # per read; the same cap the WaveQL arm's row limit expresses
MAX_MATCHES = 100


@dataclass
class TextEvidence:
    """The raw artifacts of one failing run, as files on disk."""

    sim_log: Path            # simulator stdout
    commit_out: Path         # spike-dasm'd stderr: DUT commit log + cospike messages
    divergence: str | None = None
    assertion: str | None = None
    _lines: dict[str, list[str]] = field(default_factory=dict)

    def lines(self, which: str) -> list[str]:
        if which not in self._lines:
            p = {"log": self.sim_log, "trace": self.commit_out}[which]
            self._lines[which] = p.read_text(errors="replace").splitlines() if p.is_file() else []
        return self._lines[which]


class ControlTool:
    """Logs, grep and the first-mismatch report. Today's best practice."""

    def __init__(self, ev: TextEvidence, log_calls: list | None = None):
        self.ev = ev
        self.calls = log_calls if log_calls is not None else []

    def _wrap(self, op: str, cols: list[str], rows: list[tuple], truncated: bool) -> Result:
        r = Result(cols, rows, truncated, op, 0.0)
        self.calls.append(op)
        return r

    def first_mismatch(self) -> Result:
        """The cosimulation's own report of where the DUT and Spike disagreed."""
        rows = []
        if self.ev.divergence:
            rows.append(("divergence", self.ev.divergence))
        if self.ev.assertion:
            rows.append(("assertion", self.ev.assertion))
        if not rows:
            rows.append(("note", "no divergence or assertion was reported"))
        return self._wrap("first_mismatch", ["kind", "detail"], rows, False)

    def sizes(self) -> Result:
        return self._wrap("sizes", ["stream", "lines"],
                          [("log", len(self.ev.lines("log"))),
                           ("trace", len(self.ev.lines("trace")))], False)

    def read(self, stream: str, start: int = 0, count: int = MAX_LINES) -> Result:
        """Read a slice of a stream. Capped, exactly like every WaveQL answer."""
        if stream not in ("log", "trace"):
            raise ValueError("stream must be 'log' or 'trace'")
        lines = self.ev.lines(stream)
        count = min(int(count), MAX_LINES)
        chunk = lines[int(start): int(start) + count]
        return self._wrap("read", ["line", "text"],
                          [(int(start) + i, t) for i, t in enumerate(chunk)],
                          int(start) + count < len(lines))

    def grep(self, stream: str, pattern: str, context: int = 0,
             limit: int = MAX_MATCHES) -> Result:
        """Regex search, with optional context lines. The workhorse of this arm."""
        lines = self.ev.lines(stream)
        rx = re.compile(pattern)
        out: list[tuple] = []
        for i, t in enumerate(lines):
            if rx.search(t):
                lo, hi = max(0, i - int(context)), min(len(lines), i + int(context) + 1)
                for j in range(lo, hi):
                    out.append((j, lines[j]))
                if len(out) >= int(limit):
                    break
        return self._wrap("grep", ["line", "text"], out[: int(limit)], len(out) > int(limit))

    def tail(self, stream: str, count: int = MAX_LINES) -> Result:
        """The end of a stream -- where an aborted run says why it aborted."""
        lines = self.ev.lines(stream)
        count = min(int(count), MAX_LINES)
        chunk = lines[-count:]
        return self._wrap("tail", ["line", "text"],
                          [(len(lines) - len(chunk) + i, t) for i, t in enumerate(chunk)],
                          len(lines) > count)


def control_declarations() -> list[dict]:
    S = lambda **kw: {"type": "STRING", **kw}
    I = lambda **kw: {"type": "INTEGER", **kw}
    stream = S(description="'log' (simulator stdout) or 'trace' (disassembled commit "
                           "log plus cosimulation messages)")
    return [
        {"name": "first_mismatch",
         "description": "The cosimulation's report of where the DUT disagreed with the "
                        "golden ISA model, and any assertion that fired. Start here.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "sizes",
         "description": "How many lines each stream has, so a read can be aimed.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "read",
         "description": f"Read up to {MAX_LINES} lines of a stream from a line offset.",
         "parameters": {"type": "OBJECT", "properties": {
             "stream": stream, "start": I(), "count": I()}, "required": ["stream"]}},
        {"name": "grep",
         "description": "Regex search a stream, optionally with context lines.",
         "parameters": {"type": "OBJECT", "properties": {
             "stream": stream, "pattern": S(), "context": I(), "limit": I()},
             "required": ["stream", "pattern"]}},
        {"name": "tail",
         "description": "The last lines of a stream -- where an aborted run says why.",
         "parameters": {"type": "OBJECT", "properties": {
             "stream": stream, "count": I()}, "required": ["stream"]}},
    ]


def control_dispatch(tool: ControlTool, name: str, args: dict[str, Any]) -> str:
    fn: Callable | None = {
        "first_mismatch": tool.first_mismatch, "sizes": tool.sizes,
        "read": tool.read, "grep": tool.grep, "tail": tool.tail,
    }.get(name)
    if fn is None:
        return f"ERROR: no such tool {name!r}"
    try:
        return fn(**args).render()
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:                                         # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


# The task statement is byte-identical across arms. Only `declarations` and
# `dispatch` differ -- that is the experiment.
SYSTEM = (
    "You are debugging an out-of-order RISC-V processor (BOOM v3, from the "
    "Chipyard project). A defect was introduced into one Chisel source file and "
    "the design now fails. You cannot re-run the simulation or read the design "
    "source; you can only inspect the recorded evidence through the tools "
    "provided.\n\n"
    "Your job is to localize the defect. Answer with exactly these three lines:\n"
    "MODULE: <the Chisel module you believe contains the defect>\n"
    "SIGNAL: <the signal or expression you believe is wrong, or UNKNOWN>\n"
    "CYCLE: <the cycle at which the misbehaviour is first observable, or UNKNOWN>\n"
    "Then, briefly, the evidence that led you there. Be specific; do not guess "
    "without saying that you are guessing."
)


# The repair task. Same evidence tools as localization, plus read access to the
# design's Chisel source and one structured edit. The agent is told the success
# contract exactly as the harness will apply it, because a benchmark that hides
# its bar measures guessing rather than engineering.
REPAIR_SYSTEM = (
    "You are repairing an out-of-order RISC-V processor (BOOM v3, from the "
    "Chipyard project). A defect was introduced into ONE Chisel source file and "
    "the design now fails. You can inspect the recorded evidence of the failing "
    "run and read the design's Chisel source, but you cannot re-run the "
    "simulation.\n\n"
    "Work in this order:\n"
    "1. Use the evidence tools to find WHERE and WHEN the design misbehaves.\n"
    "2. Use search_source / read_source to read the suspect Chisel code.\n"
    "3. Call propose_fix with the exact edit that repairs it. `old_text` must "
    "match the current source exactly and occur only once in that file.\n\n"
    "PROPOSE EARLY. As soon as you have a plausible candidate, call propose_fix. "
    "You may call it again later to replace it -- only the last proposal counts "
    "-- but an episode that ends without one scores ZERO no matter how good the "
    "analysis was. Do not save it for the end.\n\n"
    "Your fix will be applied, the processor rebuilt, and every stimulus re-run "
    "under Spike lockstep co-simulation. It counts ONLY if the rebuilt design "
    "passes all of them. Repairing the symptom will not pass: deleting an "
    "assertion, or editing a printf the harness reads, is rejected outright.\n\n"
    "Finish with these three lines and nothing after them:\n"
    "MODULE: <the Chisel module containing the defect>\n"
    "SIGNAL: <the signal or expression that is wrong, or UNKNOWN>\n"
    "CYCLE: <the cycle the misbehaviour is first observable, or UNKNOWN>"
)


def arm_surface(arm: str, *, waveql: WaveQLTool | None = None,
                control: ControlTool | None = None,
                source: SourceView | None = None):
    """(declarations, dispatch) for one arm.

    Passing `source` turns a localization arm into a REPAIR arm: it keeps its own
    evidence tools unchanged -- so the comparison between arms is still only
    about the evidence interface -- and gains the same source-reading and
    patch-proposing tools as every other arm.
    """
    if arm == "waveql":
        if waveql is None:
            raise ValueError("the waveql arm needs a WaveQLTool")
        decls = list(waveql_declarations())
        base = lambda n, a: waveql_dispatch(waveql, n, a)
    elif arm == "control":
        if control is None:
            raise ValueError("the control arm needs a ControlTool")
        decls = list(control_declarations())
        base = lambda n, a: control_dispatch(control, n, a)
    else:
        raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")

    if source is None:
        return decls, base

    decls = decls + source_declarations()

    def dispatch(name: str, args: dict) -> str:
        out = source_dispatch(source, name, args)
        return base(name, args) if out is None else out

    return decls, dispatch
