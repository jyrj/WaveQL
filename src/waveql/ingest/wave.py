"""Waveform ingest: a Verilator VCD becomes cycle-indexed columnar data.

Three design decisions, each forced by something measured rather than assumed.

**Names are resolved before anything is streamed.** ``pywellen.stream_changes``
accepts a list of signal paths and silently yields *nothing* for a path that does
not exist -- no exception, no warning. ``include=[]`` likewise yields nothing. A
store built from one typo is therefore empty and looks perfectly healthy, which
is the single worst failure mode available to this project. Every requested name
is resolved against the waveform's own variable table first, and a miss raises.

**The cycle comes from the DUT, not from arithmetic.** BOOM traces
``core.debug_tsc_reg``, the very register its commit-log printf prints. Reading
it makes the time-to-cycle map exact by construction and removes any need to
calibrate against clock edges. We verified this: sampling it at clock edges
reproduces the commit log 235/235, and 0/235 at either neighbouring cycle. When
no such counter exists (a design that is not BOOM), we fall back to counting
rising edges of a named clock, and say so in the store's metadata rather than
pretending the two are equivalent.

**Ingest is scoped.** A full-hierarchy MediumBoom VCD carries 71,937 variables
across 2,744 scopes; materialising all of them per task is neither necessary nor
affordable. The caller names the scopes it wants and gets a hard count of what
that selected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pyarrow as pa
import pywellen


class IngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class SignalMeta:
    signal_id: int
    full_path: str
    scope: str
    name: str
    bitwidth: int
    var_type: str


@dataclass
class CycleIndex:
    """Maps VCD time (ps) to core cycle.

    ``source`` is recorded because the two constructions are not equally
    trustworthy and a consumer is entitled to know which one produced its data.
    """

    times: list[int]           # rising-edge times, ascending
    cycles: list[int]          # the cycle in effect at each of those times
    period_ps: int | None
    source: str                # "debug_tsc_reg" | "clock_edges"

    def cycle_at(self, time_ps: int) -> int | None:
        """Cycle in effect at a time, by binary search over the edge table."""
        import bisect

        i = bisect.bisect_right(self.times, time_ps) - 1
        return self.cycles[i] if i >= 0 else None

    @property
    def first_cycle(self) -> int | None:
        return self.cycles[0] if self.cycles else None

    @property
    def last_cycle(self) -> int | None:
        return self.cycles[-1] if self.cycles else None


class WaveReader:
    """A resolved view over one VCD."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise IngestError(f"no waveform at {self.path}")
        self.wf = pywellen.Waveform(str(self.path), multi_threaded=True)
        self._by_path: dict[str, pywellen.Var] = {}
        for v in self.wf.all_vars():
            # A VCD can alias several names onto one signal; first wins, and the
            # alias is still reachable by its own full path.
            self._by_path.setdefault(v.full_name, v)

    # --- resolution ---------------------------------------------------------

    def resolve(self, names: Sequence[str]) -> list[pywellen.Var]:
        """Resolve names to Vars, raising on any miss.

        This is the guard against the silent-empty-store failure: pywellen treats
        an unknown path as "no changes", so without this a typo produces a store
        that is empty and indistinguishable from a quiet signal.
        """
        missing = [n for n in names if n not in self._by_path]
        if missing:
            hint = ""
            if missing:
                near = self.suggest(missing[0])
                if near:
                    hint = f" Closest known paths: {near}"
                    
            raise IngestError(
                f"{len(missing)} signal path(s) not present in {self.path.name}: "
                f"{missing[:5]}{'...' if len(missing) > 5 else ''}.{hint}"
            )
        return [self._by_path[n] for n in names]

    def suggest(self, name: str, limit: int = 3) -> list[str]:
        """Nearest known paths by trailing-component match, to make a typo obvious."""
        leaf = name.rsplit(".", 1)[-1]
        hits = [p for p in self._by_path if p.rsplit(".", 1)[-1] == leaf]
        return hits[:limit]

    def match(self, pattern: str) -> list[str]:
        """Every signal path matching a regex. For exploration, not for ingest."""
        rx = re.compile(pattern)
        return [p for p in self._by_path if rx.search(p)]

    def scopes_under(self, prefix: str) -> list[str]:
        return sorted({s.full_name for s in self.wf.all_scopes() if s.full_name.startswith(prefix)})

    def signals_under(self, prefixes: Sequence[str]) -> list[str]:
        """Signals under one or more scope prefixes.

        A prefix ending in ``.*`` selects that scope's OWN signals and not its
        submodules'. The core's leaf signals are where dispatch and decode stalls
        live, and they are worth having without dragging in the whole core.
        """
        direct = tuple(p[:-2] + "." for p in prefixes if p.endswith(".*"))
        pre = tuple(p if p.endswith(".") else p + "."
                    for p in prefixes if not p.endswith(".*"))
        out = [p for p in self._by_path if pre and p.startswith(pre)]
        for d in direct:
            out += [p for p in self._by_path
                    if p.startswith(d) and "." not in p[len(d):]]
        return sorted(set(out))

    # --- cycle index --------------------------------------------------------

    def cycle_index(self, clock: str, counter: str | None = None) -> CycleIndex:
        """Build the time -> cycle map.

        Prefers the DUT's own cycle counter. Falls back to counting rising edges
        of ``clock``, which is only correct if the dump is contiguous -- with
        PC-triggered windows it is *not*, so the fallback numbers cycles from
        zero within the dump and labels itself accordingly.
        """
        edges = self.rising_edges(clock)
        if not edges:
            raise IngestError(f"clock {clock!r} has no rising edge in this waveform")
        periods = {b - a for a, b in zip(edges, edges[1:])}
        period = next(iter(periods)) if len(periods) == 1 else None

        if counter and counter in self._by_path:
            # Index off the counter's OWN change list, not off clock edges.
            # The counter increments once per cycle, so its change times are
            # exactly the cycle boundaries -- and, crucially, Verilator also
            # dumps its value at dump-start, which is mid-cycle and BEFORE the
            # first rising edge. Indexing off edges left every signal in that
            # initial full-state snapshot with a NULL cycle, so any signal that
            # never changed again (a state machine sitting still) was invisible
            # to every query. Reading the counter covers the snapshot too.
            ch: list[tuple[int, int]] = []
            self.wf.stream_changes(lambda tt, _s, v: ch.append((tt, v)), [counter])
            if ch:
                ch.sort()
                return CycleIndex([t for t, _ in ch], [int(c) for _, c in ch],
                                  period, "debug_tsc_reg")
        return CycleIndex(edges, list(range(len(edges))), period, "clock_edges")

    def rising_edges(self, clock: str) -> list[int]:
        self.resolve([clock])
        ch: list[tuple[int, int]] = []
        self.wf.stream_changes(lambda t, _s, v: ch.append((t, v)), [clock])
        return [t for (_pt, pv), (t, v) in zip(ch, ch[1:]) if pv == 0 and v == 1]

    # --- extraction ---------------------------------------------------------

    def metadata(self, names: Sequence[str]) -> list[SignalMeta]:
        out = []
        for v in self.resolve(names):
            scope, _, leaf = v.full_name.rpartition(".")
            out.append(SignalMeta(
                signal_id=_sid(v), full_path=v.full_name, scope=scope, name=leaf,
                bitwidth=v.bitwidth, var_type=str(v.var_type),
            ))
        return out

    def changes(self, names: Sequence[str], index: CycleIndex | None = None,
                max_rows: int | None = None) -> pa.Table:
        """Value changes for the named signals as an Arrow table.

        ``max_rows`` is a hard cap, and hitting it is reported through
        :attr:`last_truncated` rather than by silently returning a prefix -- a
        truncated store that claims completeness would put wrong answers in front
        of an agent.
        """
        vars_ = self.resolve(names)
        sid_to_path = {_sid(v): v.full_name for v in vars_}
        times: list[int] = []
        sids: list[int] = []
        vals: list[int | None] = []
        strs: list[str | None] = []

        def cb(t, s, v):
            times.append(t)
            sids.append(_sid_of(s))
            # BOOM carries signals wider than 64 bits (128-bit bus data, wide
            # uop vectors), and pywellen hands them back as arbitrary-precision
            # Python ints. A u64-only column cannot hold them: pyarrow raises
            # OverflowError, which is the *good* outcome -- a schema that
            # silently wrapped them would corrupt exactly the wide datapath
            # signals a data-corruption bug lives in. Anything that does not fit
            # an unsigned 64-bit column is kept verbatim as hex instead.
            if isinstance(v, int) and 0 <= v < 2**64:
                vals.append(v)
                strs.append(None)
            elif isinstance(v, int):
                vals.append(None)
                strs.append(hex(v))
            else:
                vals.append(None)
                strs.append(str(v))

        self.wf.stream_changes(cb, list(names))
        self.last_truncated = max_rows is not None and len(times) > max_rows
        if self.last_truncated:
            times, sids, vals, strs = times[:max_rows], sids[:max_rows], vals[:max_rows], strs[:max_rows]

        cycles = [index.cycle_at(t) if index else None for t in times]
        return pa.table({
            "signal_id": pa.array(sids, pa.int64()),
            "signal_path": pa.array([sid_to_path.get(s) for s in sids], pa.string()),
            "time_ps": pa.array(times, pa.int64()),
            "cycle": pa.array(cycles, pa.int64()),
            "value_u64": pa.array(vals, pa.uint64()),
            "value_str": pa.array(strs, pa.string()),
        })

    last_truncated: bool = False


def _sid(var: "pywellen.Var") -> int:
    return _sid_of(var.signal_id)


_SID_RX = re.compile(r"(\d+)")


def _sid_of(signal_id) -> int:
    """pywellen exposes SignalId as an opaque object whose repr carries the id."""
    m = _SID_RX.search(str(signal_id))
    if not m:
        raise IngestError(f"cannot read a numeric id out of {signal_id!r}")
    return int(m.group(1))
