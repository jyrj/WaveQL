"""The WaveQL tool surface: what an agent is actually allowed to ask.

This is the independent variable of the whole experiment, so its design is the
contribution and not an implementation detail. Four principles, each of which
came out of running the thing rather than from taste:

**Discovery is a first-class operation.** A MediumBoom waveform has 71,937
signals; no agent can be expected to know their names, and pywellen answers a
misspelled path with *zero changes and no error*. Without ``find_signals`` the
predictable failure is an agent confidently querying a name that does not exist,
getting an empty result, and concluding the signal was quiet. So the surface
makes names discoverable and every lookup fails loudly.

**Every answer is capped, and says when it was capped.** An uncapped query here
returns megabytes. An agent that pastes megabytes into its own context has not
been helped, and one that cannot tell a prefix from a complete answer will reason
confidently about the prefix.

**Answers are asof, not range-filtered.** A waveform stores value *changes*, so
"what was signal X at cycle N" is the last change at or before N. A range filter
returns nothing for a signal that is holding its value, which reads as "no data"
when the truth is "it never moved".

**Every call is logged.** "Queries per fix" is a headline metric of the ablation,
and a metric that is not instrumented on day one never gets instrumented.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from waveql.store.store import DEFAULT_ROW_LIMIT, Result, WaveQLStore

# The BOOM tile and core scopes, so an agent can pass short names instead of the
# 100-character absolute path to every signal.
#
# The distinction matters and cost real time: the LSU, D-cache, frontend and PTW
# are siblings of `core` under the TILE, not children of it. Prefixing "lsu" with
# the core scope matched nothing -- and pywellen answers an unmatched prefix with
# silence, so the store came up ROB-only while looking healthy.
BOOM_TILE = ("TOP.TestDriver.testHarness.chiptop0.system.tile_prci_domain"
             ".element_reset_domain_boom_tile")
BOOM_CORE = BOOM_TILE + ".core"

# Scopes that live on the tile, beside the core rather than inside it.
TILE_SCOPES = ("lsu", "dcache", "frontend", "ptw", "hellaCacheArb", "tlMasterXbar")


class ToolError(RuntimeError):
    """Raised so the agent sees a real message instead of an empty table."""


@dataclass
class WaveQLTool:
    """The agent's seat at the store.

    ``scope_alias`` lets an agent write ``rob.rob_state`` instead of the full
    hierarchical path. It is a convenience, not a security boundary: the store is
    read-only regardless.
    """

    store: WaveQLStore
    core: str = BOOM_CORE
    tile: str = BOOM_TILE
    row_limit: int = DEFAULT_ROW_LIMIT
    netlist: Any = None          # HierNetlist, when the generated sources are known

    # --- discovery ----------------------------------------------------------

    def find_signals(self, pattern: str, limit: int = 50) -> Result:
        """Find signal paths matching a substring or regex.

        The entry point for everything else. BOOM's signal names are generated,
        so an agent that guesses will guess wrong; this makes the namespace
        searchable instead.
        """
        return self._short(self.store._run("find_signals", """
            SELECT full_path, bitwidth, var_type FROM signal
            WHERE regexp_matches(full_path, ?) ORDER BY full_path
        """, [pattern], limit, {"pattern": pattern}))

    def scopes(self, under: str = "", limit: int = 100) -> Result:
        """List module scopes, to navigate the hierarchy top-down."""
        pref = self._abs(under) if under else ""
        return self.store._run("scopes", """
            SELECT scope, count(*) AS signals FROM signal
            WHERE scope LIKE ? GROUP BY scope ORDER BY scope
        """, [f"{pref}%"], limit, {"under": under})

    # --- the join -----------------------------------------------------------

    def first_divergence(self, trace: bool = True) -> Result:
        """Cycle and PC where the DUT first disagreed with the golden ISA model.

        The one operation that makes the rest aimable: it converts "the test
        failed" into a cycle, which every other query can then be centred on.
        Requires no golden waveform -- the reference is an ISA simulator.

        For a WDATA mismatch it also traces the wrong value back. The committed
        value came from a writeback port, which came from an execution unit, and
        working that out by hand costs several turns for a result that is
        mechanical. The chain crosses the ROB's SRAM macro by way of the write
        that produced the value; see :meth:`why`.
        """
        r = self.store.first_divergence()
        if not (trace and r.rows and self.netlist is not None):
            return r
        cols = list(r.columns)
        try:
            row = dict(zip(cols, r.rows[0]))
            kind = row.get("kind")
            if kind not in ("wdata", "pc") or row.get("cycle") is None:
                return r
            cycle, reg = int(row["cycle"]), row.get("reg")
            if kind == "pc":
                # A PC mismatch is the DUT fetching from an address the golden
                # model never reaches, so the question is what chose the next
                # fetch address -- not what computed a value. `s0_vpc` is that
                # choice, and the chain from it runs back through the redirect
                # logic into whatever asked for it.
                chain = self.why("frontend.s0_vpc", cycle, depth=22, limit=1200)
                out = [tuple(v for v in r.rows[0])]
                wide = len(cols)
                pad0 = lambda *vals: tuple(list(vals) + [None] * (wide - len(vals)))
                out.append(pad0("trace", "frontend.s0_vpc", None,
                                "what chose the next fetch address"))
                for w in self._chain_digest(chain.rows):
                    out.append(pad0("trace", f"d{w[0]} {w[1]}", str(w[2]),
                                    self._basename(w[5])))
                return Result(cols, out, r.truncated, r.op, r.ms)
            # Which commit lane retired the offending write? Read it off the
            # waveform rather than assuming lane 0.
            lane = 0
            for cand in (0, 1):
                v = self.state_at(cycle, signals=[f"rob.io_commit_arch_valids_{cand}"]).rows
                d = self.state_at(cycle, signals=[f"rob.io_commit_uops_{cand}_ldst"]).rows
                if v and v[0][2] == 1 and d and reg is not None and d[0][2] == reg:
                    lane = cand
                    break
            chain = self.why(f"rob.io_commit_debug_wdata_{lane}", cycle,
                             depth=22, limit=1200)
        except (ToolError, Exception):                             # noqa: BLE001
            return r
        out = [tuple(v for v in r.rows[0])]
        wide = len(cols)
        pad = lambda *vals: tuple(list(vals) + [None] * (wide - len(vals)))
        out.append(pad("trace", f"lane {lane}", None,
                       "where the committed value came from"))
        for w in self._chain_digest(chain.rows):
            if str(w[1]).startswith("--"):
                out.append(pad("trace", w[1], None, ""))
            else:
                out.append(pad("trace", f"d{w[0]} {w[1]}", str(w[2]),
                               self._basename(w[5])))
        return Result(cols, out, r.truncated, r.op, r.ms)

    def commits(self, cycle: int, radius: int = 10, limit: int | None = None) -> Result:
        """Instructions the DUT retired around a cycle: the architectural story."""
        return self.store.commits_near(cycle, radius, limit or self.row_limit)

    def state_at(self, cycle: int, scope: str = "", signals: list[str] | None = None,
                 changed_only: bool = False, limit: int | None = None) -> Result:
        """Value of each signal in effect at a cycle -- a cursor on the waveform.

        ``changed_only=True`` narrows to signals that changed *at* that cycle,
        which is usually what matters at a divergence and is far smaller.
        """
        return self._short(self.store.state_at(
            cycle,
            scopes=[self._abs(scope)] if scope else None,
            signals=[self._abs(s) for s in signals] if signals else None,
            changed_only=changed_only, limit=limit or self.row_limit))

    def window(self, cycle_lo: int, cycle_hi: int, scope: str = "",
               signals: list[str] | None = None, limit: int | None = None) -> Result:
        """Signal changes within a cycle range.

        With :meth:`state_at` at ``cycle_lo`` this fully determines every cycle in
        the range, while staying small.
        """
        if cycle_hi < cycle_lo:
            raise ToolError("cycle_hi must be >= cycle_lo")
        return self._short(self.store.window(
            cycle_lo, cycle_hi,
            scopes=[self._abs(scope)] if scope else None,
            signals=[self._abs(s) for s in signals] if signals else None,
            limit=limit or self.row_limit))

    def trace_signal(self, signal: str, cycle_lo: int, cycle_hi: int,
                     limit: int | None = None) -> Result:
        """One signal across a cycle range, including the value it entered with.

        The leading row is flagged ``entering``: without it a signal that holds
        its value across the whole range looks like it has no data.
        """
        path = self._abs(signal)
        self._must_exist(path)
        return self.store.trace_signal(path, cycle_lo, cycle_hi, limit or self.row_limit)

    def inflight(self, cycle: int, limit: int | None = None) -> Result:
        """The ROB's occupancy at a cycle: which entries are valid, plus head/tail.

        HONEST LIMIT: per-entry *instruction contents* are not available. BOOM
        keeps them in an SRAM macro (``rob_debug_inst_mem``), and Verilator traces
        only that macro's ports, not its contents. What is traced -- and what this
        returns -- is the per-entry valid bits (``rob_val_*``, one signal per
        entry), the head/tail pointers and the ROB state. For instruction
        identity, join to :meth:`commits`.
        """
        return self.store._run("inflight", """
            WITH latest AS (
                SELECT s.name, w.value_u64, w.cycle,
                       row_number() OVER (PARTITION BY s.full_path
                                          ORDER BY w.cycle DESC, w.time_ps DESC) AS rn
                FROM wave w JOIN signal s USING (signal_id)
                WHERE w.cycle <= ? AND s.scope = ?
                  AND (s.name LIKE 'rob_val_%' OR s.name IN
                       ('rob_head','rob_tail','rob_state','rob_head_idx','rob_tail_idx'))
            )
            SELECT name, value_u64, cycle AS last_change FROM latest WHERE rn = 1
              AND (value_u64 = 1 OR name NOT LIKE 'rob_val_%')
            ORDER BY name
        """, [cycle, f"{self.core}.rob"], limit or self.row_limit, {"cycle": cycle})

    # --- hangs ---------------------------------------------------------------

    def stall_report(self, onset: int | None = None, limit: int | None = None) -> Result:
        """Why the pipeline stopped retiring instructions.

        Half this corpus fails by hanging rather than by computing a wrong value,
        and for those there is no divergence to aim at: ``first_divergence`` says
        only that an assertion fired. The agent is left to reconstruct a stall
        from raw signal values, which is a great deal of querying to arrive at
        facts that are entirely mechanical.

        Those facts are: when retirement stopped, what the ROB looked like when it
        did, which ready/valid handshakes were stuck and in which direction, and
        which parts of the design were still moving. The direction is the
        informative part. A BLOCKED handshake (valid high, ready low) means
        something downstream refused the transfer and names it. A STARVED one
        (ready high, valid low) means nothing was offered, and the defect is
        upstream, in whatever should have raised valid.
        """
        db = self.store.db
        if onset is None:
            onset = db.execute("SELECT max(cycle) FROM commit_log").fetchone()[0]
        if onset is None:
            raise ToolError("no commits in this store, so there is no stall to date.")
        hi = db.execute("SELECT max(cycle) FROM wave").fetchone()[0]
        mid = onset + max(1, (hi - onset) // 2)

        at = dict(db.execute("""
            WITH latest AS (
                SELECT s.full_path AS p, w.value_u64 AS v,
                       row_number() OVER (PARTITION BY s.full_path
                                          ORDER BY w.cycle DESC, w.time_ps DESC) AS rn
                FROM wave w JOIN signal s USING (signal_id) WHERE w.cycle <= ?)
            SELECT p, v FROM latest WHERE rn = 1""", [mid]).fetchall())
        moved = {r[0] for r in db.execute(
            "SELECT DISTINCT full_path FROM wave JOIN signal USING (signal_id) "
            "WHERE cycle > ?", [onset]).fetchall()}

        rows: list[tuple] = [
            ("stall", "last_retired_cycle", str(onset), "after this, nothing committed"),
            ("stall", "last_recorded_cycle", str(hi), ""),
            ("stall", "stalled_cycles", str(hi - onset), ""),
        ]
        for nm in ("rob_head", "rob_tail", "rob_state"):
            k = f"{self.core}.rob.{nm}"
            if k in at:
                rows.append(("rob", nm, str(at[k]), "frozen" if k not in moved else "moving"))
        val = {int(k.rsplit("_", 1)[-1]): v for k, v in at.items()
               if k.startswith(f"{self.core}.rob.rob_val_")}
        bsy = {int(k.rsplit("_", 1)[-1]): v for k, v in at.items()
               if k.startswith(f"{self.core}.rob.rob_bsy_")}
        occupied = sorted(i for i, v in val.items() if v)
        rows.append(("rob", "entries_valid", str(len(occupied)), str(occupied[:16])))
        rows.append(("rob", "entries_valid_and_busy",
                     str(sum(1 for i in occupied if bsy.get(i))),
                     "busy = issued but not yet written back"))

        blocked, starved = [], []
        for k in at:
            if not k.endswith("_valid"):
                continue
            rd = k[:-6] + "_ready"
            if rd not in at:
                continue
            if k in moved or rd in moved:
                continue
            if at[k] == 1 and at[rd] == 0:
                blocked.append(self._rel(k[:-6]))
            elif at[k] == 0 and at[rd] == 1:
                starved.append(self._rel(k[:-6]))
        rows.append(("handshake", "blocked", str(len(blocked)),
                     "valid high, ready low: the receiver refused"))
        for b in sorted(blocked)[:20]:
            rows.append(("handshake", "blocked_at", b, "look downstream of this"))
        rows.append(("handshake", "starved", str(len(starved)),
                     "ready high, valid low: nothing was offered"))
        for b in sorted(starved)[:20]:
            rows.append(("handshake", "starved_at", b, "look upstream of this"))

        live: dict[str, int] = {}
        total: dict[str, int] = {}
        for k in at:
            sc = self._rel(k).rsplit(".", 1)[0]
            total[sc] = total.get(sc, 0) + 1
            if k in moved:
                live[sc] = live.get(sc, 0) + 1
        for sc, n in sorted(live.items(), key=lambda kv: -kv[1])[:10]:
            rows.append(("activity", sc, f"{n}/{total[sc]}", "signals still changing"))
        dead = [sc for sc in total if not live.get(sc)]
        rows.append(("activity", "fully_frozen_scopes", str(len(dead)), str(sorted(dead)[:8])))

        # Finish the question rather than handing it back. The ROB head is not
        # retiring; the entry sitting there is either still BUSY (issued, never
        # written back) or not VALID. Which one, and what that depends on, is
        # mechanical -- and asking the agent to work it out costs several turns
        # it does not have.
        head = at.get(f"{self.core}.rob.rob_head")
        if head is not None and self.netlist is not None:
            # Two ways to hang, and they ask opposite questions. Either an
            # instruction is SITTING in the ROB and never completes, or the ROB
            # is EMPTY and nothing is arriving -- in which case the defect is
            # upstream, in fetch, decode, rename or dispatch. Handling only the
            # first left the empty-ROB hangs with no cause at all.
            cands = [(bank, at.get(f"{self.core}.rob.rob_val_{bank}{head}"))
                     for bank in ("", "1_", "2_", "3_")]
            if not any(v == 1 for _, v in cands):
                rows.append(("cause", "rob_empty", "io_enq_valids_0",
                             "the ROB is empty and nothing is retiring: the defect "
                             "is upstream of dispatch, not in a stuck entry"))
                try:
                    for w in self._chain_digest(
                            self.why("rob.io_enq_valids_0", mid, depth=22,
                                     limit=1200).rows):
                        rows.append(("cause", f"d{w[0]} {w[1]}", str(w[2]),
                                     f"last change {w[3]}; {self._basename(w[5])}"))
                except ToolError:
                    pass
            for bank in ("", "1_", "2_", "3_"):
                v = at.get(f"{self.core}.rob.rob_val_{bank}{head}")
                if v != 1:
                    continue
                busy = at.get(f"{self.core}.rob.rob_bsy_{bank}{head}")
                sig = (f"rob.rob_bsy_{bank}{head}" if busy
                       else f"rob.rob_val_{bank}{head}")
                rows.append(("cause", "stuck_entry", sig,
                             "head entry is valid and busy: issued, never written back"
                             if busy else "head entry is valid and not busy"))
                try:
                    # Report the NAMED links. firtool's `_GEN_*` temporaries are
                    # most of any chain and an agent can do nothing with
                    # `_GEN_1507`; the named signals either side of them are what
                    # identify the module that failed to act. The full chain,
                    # temporaries included, is still there via why().
                    for w in self._chain_digest(
                            self.why(sig, mid, depth=22, limit=1200).rows):
                        rows.append(("cause", f"d{w[0]} {w[1]}", str(w[2]),
                                     f"last change {w[3]}; {self._basename(w[5])}"))
                except ToolError:
                    pass
                break
        return self.store._log_rows(
            "stall_report", ["kind", "item", "value", "note"], rows,
            limit or self.row_limit, {"onset": onset})

    # --- signal -> source ---------------------------------------------------

    def source_of(self, signal: str, limit: int | None = None) -> Result:
        """The Chisel line that declared a signal.

        The waveform is generated Verilog; the defect is in Chisel. Without this
        step an agent can localize a wrong value to the cycle and the bit and
        still have nowhere to edit.
        """
        path = self._abs(signal)
        self._must_exist(path)
        return self._short(self.store._run("source_of", """
            SELECT full_path, chisel_file, chisel_line, module
            FROM signal_src WHERE full_path = ? ORDER BY chisel_line
        """, [path], limit or self.row_limit, {"signal": signal}))

    def drivers(self, signal: str, depth: int = 1, limit: int | None = None) -> Result:
        """What this signal depends on, and where that logic lives in Chisel.

        A waveform answers "what was the value"; it cannot answer "what made it
        that value", because the netlist is not in the dump. This walks firtool's
        generated Verilog backwards from the signal, crossing module boundaries
        through ports, and reports each contributor with its Chisel origin.

        ``via`` distinguishes DATA dependence (the right-hand side) from CONTROL
        dependence (the enclosing enable). For a stalled pipeline the control
        edge is usually the answer: the register is not holding a wrong value, it
        is not being written at all.
        """
        if self.netlist is None:
            raise ToolError(
                "drivers() needs the generated sources for this build; this store "
                "was ingested without them.")
        path = self._abs(signal)
        self._must_exist(path)
        scope, leaf = path.rsplit(".", 1)
        steps = self.netlist.slice(scope, leaf, max_depth=max(1, depth),
                                   max_nodes=(limit or self.row_limit) + 1)
        # Report paths core-relative. The absolute prefix is 100 characters of
        # boilerplate on every row, and the agent's context is the scarce
        # resource here -- the same reason every answer is capped.
        rows = [(s.depth, s.via, s.module, self._rel(f"{s.path}.{s.signal}"),
                 self._basename(str(s.refs[0])) if s.refs else None)
                for s in steps if s.depth > 0]
        if not rows:
            raise ToolError(
                f"no driver found for {signal!r}. It is a primary input, a "
                "clock/reset, or lives inside an SRAM macro, none of which have "
                "Chisel logic behind them.")
        return self.store._log_rows(
            "drivers", ["depth", "via", "module", "signal", "chisel"],
            rows, limit or self.row_limit, {"signal": signal, "depth": depth})

    def why(self, signal: str, cycle: int, depth: int = 12,
            limit: int | None = None, cross_memory: bool = True) -> Result:
        """Why this signal held this value at this cycle.

        :meth:`drivers` lists everything a signal depends on. Most of it is
        irrelevant to the value it actually took: an AND is 0 because of its
        false operands, and a mux takes one arm. This follows only the operands
        that ACCOUNT for the value, so the answer is a chain rather than a cone,
        and every link carries the Chisel line behind it.

        Values for firtool's anonymous temporaries are not in the dump -- most of
        a dependency chain is `_GEN_*` -- so they are recomputed from the design's
        own expressions, bottoming out at the traced registers and ports.

        ``last_change`` is the cycle each link last moved, blank for a signal the
        dump does not carry. In a stall nearly everything is frozen, so the link
        that froze EARLIEST is the one the others are waiting on.
        """
        if self.netlist is None:
            raise ToolError(
                "why() needs the generated sources for this build; this store "
                "was ingested without them.")
        from waveql.ingest.explain import Evaluator, Explainer

        path = self._abs(signal)
        self._must_exist(path)
        scope, leaf = path.rsplit(".", 1)
        # One snapshot up front: an asof per signal lookup would be thousands of
        # queries for a single answer.
        snap = dict(self.store.db.execute("""
            WITH latest AS (
                SELECT s.full_path AS p, w.value_u64 AS v,
                       row_number() OVER (PARTITION BY s.full_path
                                          ORDER BY w.cycle DESC, w.time_ps DESC) AS rn
                FROM wave w JOIN signal s USING (signal_id)
                WHERE w.cycle IS NOT NULL AND w.cycle <= ?)
            SELECT p, v FROM latest WHERE rn = 1""", [cycle]).fetchall())
        ev = Evaluator(self.netlist, lambda p, n: snap.get(f"{p}.{n}"))
        chain = Explainer(self.netlist, ev).why(
            scope, leaf, max_depth=max(1, depth),
            max_nodes=(limit or self.row_limit) + 1)
        # When each link last moved. In a stall almost everything is frozen, so
        # the useful question is which link froze FIRST -- the rest stopped
        # because it did.
        last = dict(self.store.db.execute("""
            SELECT s.full_path, max(w.cycle) FROM wave w JOIN signal s USING (signal_id)
            WHERE w.cycle IS NOT NULL AND w.cycle <= ? GROUP BY s.full_path""",
            [cycle]).fetchall())
        rows = [(w.depth, self._rel(w.rel), w.value, last.get(w.rel), w.module,
                 self._basename(str(w.refs[0])) if w.refs else None, w.expr[:70],
                 self._rel(w.parent) if w.parent else None)
                for w in chain]
        # If the chain ended in an SRAM macro, cross it: continue from the write
        # that produced the value, at the cycle it was written.
        if cross_memory:
            for w in chain:
                m = self._READ_PORT.match(w.rel)
                if not m:
                    continue
                hop = self._memory_hop(m.group("mem"), m.group("k"), cycle)
                if not hop:
                    continue
                wc, j = hop
                try:
                    sub = self.why(self._rel(f"{m.group('mem')}.W{j}_data"), wc,
                                   depth=max(1, depth - w.depth), limit=limit,
                                   cross_memory=False)
                except ToolError:
                    break
                # Drop the macro's own port values from the main chain. They were
                # read at the cycle the VALUE WAS READ, and the write happened
                # earlier -- so every one of them is a stale number attached to a
                # real signal name, which is worse than omitting them. The hop
                # supplies the same ports at the cycle that actually wrote.
                pref = self._rel(m.group("mem")) + "."
                rows = [r for r in rows if not r[1].startswith(pref)
                        or r[1] == self._rel(w.rel)]
                rows.append((w.depth, f"-- written at cycle {wc} by port W{j} --",
                             None, wc, "", None, "crossing the SRAM macro",
                             self._rel(w.rel)))
                rows += [(w.depth + 1 + r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                          r[7] if len(r) > 7 else None) for r in sub.rows]
                # Put the crossing where it belongs in the chain. Appended at the
                # end, everything past the memory sorts after every shallow link,
                # and any summary that reads the first N rows never reaches it --
                # which is how the trace kept stopping at the ROB.
                rows.sort(key=lambda r: r[0])
                break
        return self.store._log_rows(
            "why", ["depth", "signal", "value", "last_change", "module", "chisel",
                    "expr", "from"],
            rows, limit or self.row_limit, {"signal": signal, "cycle": cycle})

    # A memory is where a dependency chain stops being a circuit question and
    # becomes a history question: the value read now was written at some earlier
    # cycle, and nothing in the netlist says when.
    _READ_PORT = re.compile(r"^(?P<mem>.+)\.R(?P<k>\d+)_data$")

    def _memory_hop(self, mem: str, k: str, cycle: int) -> tuple[int, str] | None:
        """Find the write that put the value a read port is returning.

        firtool emits SRAM macros as blackboxes -- BOOM keeps per-entry ROB state
        in one -- so a backward walk reaches `R0_data` and stops: the macro has no
        Chisel logic behind it. But the waveform has the write ports, so the
        question "where did this value come from" is still answerable, just from
        history rather than from structure. Returns (cycle, write port).
        """
        addr = self.store.db.execute("""
            SELECT w.value_u64 FROM wave w JOIN signal s USING (signal_id)
            WHERE s.full_path = ? AND w.cycle IS NOT NULL AND w.cycle <= ?
            ORDER BY w.cycle DESC, w.time_ps DESC LIMIT 1""",
            [f"{mem}.R{k}_addr", cycle]).fetchone()
        if addr is None:
            return None
        ports = [r[0] for r in self.store.db.execute(
            "SELECT DISTINCT name FROM signal WHERE scope = ? AND name LIKE 'W%_en'",
            [mem]).fetchall()]
        best: tuple[int, str] | None = None
        for en in sorted(ports):
            j = en[1:-3]
            # ASOF, not an equijoin on cycle. A waveform stores CHANGES, and a
            # write's enable and its address almost never change on the same
            # cycle -- joining the two change lists directly matches nothing,
            # which is exactly what it did.
            row = self.store.db.execute("""
                WITH e AS (SELECT w.cycle FROM wave w JOIN signal s USING (signal_id)
                           WHERE s.full_path = ? AND w.value_u64 = 1
                             AND w.cycle IS NOT NULL AND w.cycle <= ?),
                     a AS (SELECT w.cycle, w.value_u64 FROM wave w JOIN signal s USING (signal_id)
                           WHERE s.full_path = ? AND w.cycle IS NOT NULL AND w.cycle <= ?)
                SELECT max(e.cycle) FROM e ASOF JOIN a ON a.cycle <= e.cycle
                WHERE a.value_u64 = ?""",
                [f"{mem}.W{j}_en", cycle, f"{mem}.W{j}_addr", cycle, addr[0]]).fetchone()
            if row and row[0] is not None and (best is None or row[0] > best[0]):
                best = (int(row[0]), j)
        return best

    def sql(self, query: str, limit: int | None = None) -> Result:
        """Read-only SQL over the joined store.

        Tables: ``signal``, ``wave(signal_id, cycle, value_u64, value_str)``,
        ``commit_log(cycle, priv, pc, insn, rd, wdata)``, ``spike_log``,
        ``divergence``, ``signal_src``. The escape hatch for questions the fixed
        operations do not cover.
        """
        return self.store.sql(query, limit or self.row_limit)

    # --- helpers ------------------------------------------------------------

    def _abs(self, name: str) -> str:
        """Expand a short name to its absolute hierarchical path.

        Names are core-relative by default (`rob.rob_state`), but the memory
        subsystem sits beside the core on the tile, so `lsu.*` and `dcache.*`
        resolve there instead.
        """
        if name.startswith("TOP."):
            return name
        if not name:
            return self.core
        head = name.split(".", 1)[0]
        if head in TILE_SCOPES:
            return f"{self.tile}.{name}"
        return f"{self.core}.{name}"

    def _short(self, r: Result, col: str = "full_path") -> Result:
        """Report signal paths core-relative in a result table.

        Every absolute path carries the same 100-character prefix, and on a
        50-row discovery query that is 5KB of boilerplate for maybe 1KB of
        information. Context is what the agent runs out of -- an episode ended
        after 42 queries having never proposed a fix -- so the prefix comes off.
        `_abs` accepts the short form back, so the names still round-trip.
        """
        if col not in r.columns:
            return r
        i = r.columns.index(col)
        rows = [tuple(self._rel(v) if j == i and isinstance(v, str) else v
                      for j, v in enumerate(row)) for row in r.rows]
        cols = list(r.columns)
        cols[i] = "signal"
        return Result(cols, rows, r.truncated, r.op, r.ms)

    @staticmethod
    def _basename(ref: str | None) -> str:
        """`execution-unit.scala:208:7`, not the 60 characters of path before it.

        The directory is the same for nearly every BOOM file and is pure cost in
        a table cell. Worse, truncating the full path to fit cut the filename in
        half -- `generators/boom/.../execution-units/execution-unit.scala:208` came
        out as `execut`, so the one part that identifies the file was the part
        that got dropped.
        """
        return ref.rsplit("/", 1)[-1] if ref else ""

    @staticmethod
    def _chain_digest(rows, per_depth: int = 1, total: int = 22) -> list:
        """Pick links that span the chain rather than its first few rows.

        A breadth-first walk emits every shallow link before any deep one, so
        taking the first N rows returns N ways of saying "the ROB" and never
        reaches the execution unit that produced the value. Taking one per depth
        keeps the shape of the chain, which is the part that identifies the module
        at fault.

        DEPTH is what the budget should buy, not breadth. With two links per depth
        a 16-row summary reached depth 8; on 1132ff87 the chain runs to depth 18
        and the defect's file, register-read.scala, first appears at depth 13. The
        answer was in the chain and not in the summary.

        firtool's `_GEN_*` temporaries are dropped: an agent can do nothing with
        `_GEN_1507`, and the named signals around them carry the same structure.
        """
        # Within a depth, prefer links CARRYING THE VALUE being explained. A
        # wrong value propagates, so the signals that hold it are the path it
        # travelled; the rest of the fan-in at that depth is logic that happened
        # to be nearby. Without this the walk followed whichever writeback port
        # the netlist happened to list first -- reliably the FP pipeline, whose
        # valid was 0.
        root = next((w[2] for w in rows if w[0] == 0), None)
        # Following "links that carry the value" only means something when the
        # value IDENTIFIES something. A wrong 64-bit result does; a control bit
        # does not, because in a stalled machine almost everything is 0. Rooted at
        # io_enq_valids_0 = 0, the rule dropped 302 named links including
        # io_lsu_stq_full_0 = 1 and lsu.stq_head -- the signals that explain the
        # stall -- and anchored on an arbitrary deep zero, so the chain wandered
        # back into the ROB instead of forward into the LSU.
        # A value only selects a path if few signals coincidentally hold it.
        # Measured on these chains: a 0 appears at 22 of 23 depths, and a 1 at
        # 19 of 19 and 21 of 21 -- so for a 1-BIT root, "carries the value" picks
        # arbitrarily, not informatively. It is kept only for wide data values,
        # where a wrong 64-bit result really does mark the path it travelled.
        distinctive = root not in (None, 0, 1)
        keep = [w for w in rows
                if str(w[1]).startswith("--")
                or not (str(w[1]).rsplit(".", 1)[-1].startswith("_")
                        or "_ext." in str(w[1])
                        # clock and reset reach everything and explain nothing.
                        or str(w[1]).rsplit(".", 1)[-1] in ("reset", "clock")
                        # Paths _rel could not shorten are above the tile: clock
                        # and reset distribution, carrying no value and no
                        # bearing on why the core stopped.
                        or str(w[1]).startswith("TOP."))]
        # Where a depth has any link carrying the value, show ONLY those. The
        # others are logic that happens to sit at the same distance -- in these
        # chains, reliably the FP pipeline, whose valid was 0 and whose data
        # never went near the answer.
        if distinctive:
            carries = {w[0] for w in keep if w[2] == root}
            keep = [w for w in keep
                    if str(w[1]).startswith("--") or w[0] not in carries
                    or w[2] == root]
            # Below the deepest link that still carries the value, keep only its
            # DESCENDANTS. The producer of a wrong value does not itself hold that
            # value -- it holds the inputs that made it -- so value-matching walks
            # the propagation path and then has nothing to follow, and the summary
            # drifts into whatever else sits at those depths. On 1132ff87 that
            # meant the FP pipeline, while the defect's file appeared at depth 13
            # in the part of the chain being dropped.
            parent = {str(w[1]): (w[7] if len(w) > 7 else None) for w in rows}
            deepest = max((w for w in keep if w[2] == root),
                          key=lambda w: w[0], default=None)
            if deepest is not None:
                anc, kept = str(deepest[1]), []
                for w in keep:
                    if w[0] <= deepest[0] or str(w[1]).startswith("--"):
                        kept.append(w)
                        continue
                    node, hops = str(w[1]), 0
                    while node and hops < 64:
                        if node == anc:
                            kept.append(w)
                            break
                        node = parent.get(node)
                        hops += 1
                keep = kept
        # For a 1-bit root there is no value to follow, so use the evidence that
        # a stall actually provides: WHEN each link last moved. Everything is
        # frozen; the one that froze EARLIEST is what the rest are waiting on.
        # Untraced links (no recorded change) sort last -- they are recomputed,
        # not observed.
        # "Froze earliest" was the first version of this rule and it was wrong in
        # a specific way: a signal that NEVER moved has its last change at the
        # first cycle of the recording, so it looks like the earliest freezer of
        # all. In the dcache hang the summary filled with such constants, all
        # stamped at the window's first cycle, pushing out the links that
        # actually stopped with the symptom. The anchor is the root's own last
        # change: causes stop when the symptom stops, so rank by distance from
        # that moment, and constants -- infinitely far from it -- sort last.
        root_lc = next((w[3] for w in rows
                        if w[0] == 0 and isinstance(w[3], (int, float))), None)
        floor = min((w[3] for w in rows if isinstance(w[3], (int, float))),
                    default=None)

        def rank(i):
            w = keep[i]
            if distinctive:
                return (w[0], 0 if w[2] == root else 1, i)
            lc = w[3] if isinstance(w[3], (int, float)) else None
            if lc is None or root_lc is None or lc == floor:
                return (w[0], 1, 0, i)        # unobserved, or never moved
            return (w[0], 0, abs(lc - root_lc), i)

        order = sorted(range(len(keep)), key=rank)
        seen: dict[int, int] = {}
        picked = []
        for i in order:
            w = keep[i]
            if str(w[1]).startswith("--"):
                picked.append((i, w))
                continue
            d = w[0]
            if seen.get(d, 0) >= per_depth:
                continue
            seen[d] = seen.get(d, 0) + 1
            picked.append((i, w))
            if len(picked) >= total:
                break
        return [w for _, w in sorted(picked, key=lambda p: (p[1][0], p[0]))]

    def _rel(self, path: str) -> str:
        """Inverse of :meth:`_abs`: the short name an agent can pass back in."""
        for pref in (self.core + ".", self.tile + "."):
            if path.startswith(pref):
                return path[len(pref):]
        return path

    def _must_exist(self, path: str) -> None:
        """Fail loudly on an unknown signal.

        pywellen answers an unknown path with zero changes and no error, so
        without this an agent's typo becomes "the signal was quiet".
        """
        n = self.store.db.execute(
            "SELECT count(*) FROM signal WHERE full_path = ?", [path]).fetchone()[0]
        if n:
            return
        leaf = path.rsplit(".", 1)[-1]
        near = self.store.db.execute(
            "SELECT full_path FROM signal WHERE name = ? LIMIT 3", [leaf]).fetchall()
        hint = f" Did you mean: {[r[0] for r in near]}" if near else \
               " Use find_signals(pattern) to search the namespace."
        raise ToolError(f"no signal {path!r} in this store.{hint}")


# --- function-calling schema -------------------------------------------------
# Declared once and shared by every model seat, so all arms describe the tool
# identically. The task statement and success contract are byte-identical across
# arms; only the tools may differ, because the tools ARE the independent variable.

def declarations(core_hint: str = "e.g. 'rob.rob_state' or 'lsu.io_core_req_valid'") -> list[dict]:
    S = lambda **kw: {"type": "STRING", **kw}
    I = lambda **kw: {"type": "INTEGER", **kw}
    return [
        {"name": "first_divergence",
         "description": "How and when the run failed. Either the DUT disagreed with the "
                        "golden ISA model at a cycle and PC, or an assertion fired -- the "
                        "`kind` column says which. Start here: it turns 'the test failed' "
                        "into a cycle. If kind is `assertion`, follow with stall_report; "
                        "there is no diverging value to aim at.",
         "parameters": {"type": "OBJECT", "properties": {}}},
        {"name": "find_signals",
         "description": "Search signal paths by regex. BOOM has ~72k generated signal "
                        "names; use this rather than guessing one.",
         "parameters": {"type": "OBJECT", "properties": {
             "pattern": S(description="regex matched against the full signal path"),
             "limit": I(description="max rows (default 50)")},
             "required": ["pattern"]}},
        {"name": "commits",
         "description": "Instructions the DUT retired around a cycle (pc, insn, rd, wdata).",
         "parameters": {"type": "OBJECT", "properties": {
             "cycle": I(), "radius": I(description="cycles either side, default 10")},
             "required": ["cycle"]}},
        {"name": "state_at",
         "description": "Value of each signal in effect at a cycle (last change at or "
                        "before it). Set changed_only to see just what moved at that cycle.",
         "parameters": {"type": "OBJECT", "properties": {
             "cycle": I(),
             "scope": S(description=f"core-relative scope, {core_hint}"),
             "signals": {"type": "ARRAY", "items": S()},
             "changed_only": {"type": "BOOLEAN"}, "limit": I()},
             "required": ["cycle"]}},
        {"name": "window",
         "description": "Signal changes within a cycle range. Combine with state_at at "
                        "the low end to reconstruct any cycle in the range.",
         "parameters": {"type": "OBJECT", "properties": {
             "cycle_lo": I(), "cycle_hi": I(), "scope": S(),
             "signals": {"type": "ARRAY", "items": S()}, "limit": I()},
             "required": ["cycle_lo", "cycle_hi"]}},
        {"name": "trace_signal",
         "description": "One signal across a cycle range, including the value it entered "
                        "the range with.",
         "parameters": {"type": "OBJECT", "properties": {
             "signal": S(description=f"core-relative path, {core_hint}"),
             "cycle_lo": I(), "cycle_hi": I(), "limit": I()},
             "required": ["signal", "cycle_lo", "cycle_hi"]}},
        {"name": "inflight",
         "description": "ROB occupancy at a cycle: which entries are valid, plus head, "
                        "tail and state. Per-entry instruction contents are NOT traced "
                        "(they live in an SRAM macro); join to commits for identity.",
         "parameters": {"type": "OBJECT", "properties": {
             "cycle": I(), "limit": I()}, "required": ["cycle"]}},
        {"name": "stall_report",
         "description": "Why the pipeline stopped retiring: the last retired cycle, the "
                        "ROB at that moment, which ready/valid handshakes are stuck and "
                        "in which direction, and what is still moving. Start here when "
                        "an assertion fired rather than a value diverging. BLOCKED means "
                        "a receiver refused a transfer, so look downstream; STARVED means "
                        "nothing was offered, so look upstream.",
         "parameters": {"type": "OBJECT", "properties": {
             "onset": I(description="cycle the stall began; defaults to the last commit"),
             "limit": I()}}},
        {"name": "source_of",
         "description": "The Chisel file and line that declared a signal. The waveform is "
                        "generated Verilog and the defect is in Chisel, so this is how a "
                        "suspect signal becomes a place to read and edit.",
         "parameters": {"type": "OBJECT", "properties": {
             "signal": S(description=f"core-relative path, {core_hint}"), "limit": I()},
             "required": ["signal"]}},
        {"name": "drivers",
         "description": "What a signal depends on, with the Chisel line behind each "
                        "contributor, following ports across module boundaries. `via` is "
                        "DATA (the right-hand side) or CONTROL (the enclosing enable) -- "
                        "for a stuck register the control edge is usually the answer, "
                        "because it is not being written at all.",
         "parameters": {"type": "OBJECT", "properties": {
             "signal": S(description=f"core-relative path, {core_hint}"),
             "depth": I(description="how many levels back, default 1"), "limit": I()},
             "required": ["signal"]}},
        {"name": "why",
         "description": "Why a signal held the value it did at a cycle. Unlike drivers, "
                        "this follows only the operands that ACCOUNT for the value -- the "
                        "false operand of an AND, the taken arm of a mux -- so the answer "
                        "is a causal chain rather than the whole dependency cone. Each "
                        "row also gives the cycle that signal last changed; in a stall the "
                        "link that froze EARLIEST is the one the rest are waiting on. Use "
                        "this once you have a signal that holds a value it should not.",
         "parameters": {"type": "OBJECT", "properties": {
             "signal": S(description=f"core-relative path, {core_hint}"),
             "cycle": I(), "depth": I(description="max links, default 12"), "limit": I()},
             "required": ["signal", "cycle"]}},
        {"name": "sql",
         "description": "Read-only SQL over signal / wave / commit_log / spike_log / "
                        "divergence / signal_src. Use when the fixed operations do not fit.",
         "parameters": {"type": "OBJECT", "properties": {
             "query": S(), "limit": I()}, "required": ["query"]}},
    ]


def dispatch(tool: WaveQLTool, name: str, args: dict[str, Any]) -> str:
    """Run one tool call and render it for the model.

    Errors are returned as text rather than raised: a tool error is information
    the agent should act on (usually "that signal does not exist"), not a reason
    to end the episode.
    """
    fn: Callable | None = {
        "first_divergence": tool.first_divergence, "find_signals": tool.find_signals,
        "commits": tool.commits, "state_at": tool.state_at, "window": tool.window,
        "trace_signal": tool.trace_signal, "inflight": tool.inflight, "sql": tool.sql,
        "stall_report": tool.stall_report, "source_of": tool.source_of,
        "drivers": tool.drivers, "why": tool.why,
    }.get(name)
    if fn is None:
        return f"ERROR: no such tool {name!r}"
    try:
        return fn(**args).render()
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:                                         # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"
