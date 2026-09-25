"""The WaveQL store: build it, and query it under a cost bound.

The store exists because of one number. A MediumBoom waveform window carries
71,937 signals over thousands of cycles; the commit log for a single riscv-test
is 832 instructions. Neither fits in an agent's context, and neither answers a
debugging question on its own. What answers the question is the *join* -- and a
join needs both sides in one place, indexed by the same key.

Every query is capped and every query is logged. The cap is not politeness: an
unbounded query against this data returns megabytes, and an agent that pastes
megabytes into its own context has not been helped. The log exists because
"queries per fix" is one of the headline metrics, and a metric that is not
instrumented from the first commit does not get instrumented at all.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import duckdb
import pyarrow as pa

_SCHEMA = Path(__file__).with_name("schema.sql")
DEFAULT_ROW_LIMIT = 500


@dataclass
class Result:
    """A capped answer that always admits when it was capped."""

    columns: list[str]
    rows: list[tuple]
    truncated: bool
    op: str
    ms: float

    def __len__(self) -> int:
        return len(self.rows)

    def to_dicts(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def render(self, max_width: int = 120, max_chars: int = 8000) -> str:
        """A compact table plus an explicit truncation notice.

        The notice is not decoration: an agent that cannot tell a complete answer
        from a truncated one will reason confidently about a prefix.

        `max_chars` is a HARD cap on what goes back into the model's context, and
        it is a correctness measure as much as a cost one. Without it a 500-row
        answer of 100-character signal paths is ~50 KB, and thirty such answers
        built a request large enough that Vertex returned 504 DEADLINE_EXCEEDED
        -- twice, killing both arms of one task. A tool that cannot fit its
        answer in the caller's context has not answered.
        """
        head = " | ".join(self.columns)
        lines = [" | ".join("" if c is None else str(c) for c in r)[:max_width]
                 for r in self.rows]
        note = f"\n[TRUNCATED at {len(self.rows)} rows -- narrow the query]" if self.truncated else ""
        out = f"{head}\n" + "\n".join(lines) + note
        if len(out) <= max_chars:
            return out
        kept, size = [], len(head) + 1
        for ln in lines:
            if size + len(ln) + 1 > max_chars:
                break
            kept.append(ln)
            size += len(ln) + 1
        return (f"{head}\n" + "\n".join(kept)
                + f"\n[TRUNCATED: {len(kept)} of {len(self.rows)} rows shown "
                  f"({max_chars} char cap) -- narrow the query]")


class WaveQLStore:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path else ":memory:"
        self.db = duckdb.connect(self.path)
        self.db.execute(_SCHEMA.read_text())
        self._q = 0

    # --- build --------------------------------------------------------------

    def put_signals(self, metas: Sequence) -> None:
        if not metas:
            return
        tbl = pa.table({
            "signal_id": pa.array([m.signal_id for m in metas], pa.int64()),
            "full_path": pa.array([m.full_path for m in metas]),
            "scope": pa.array([m.scope for m in metas]),
            "name": pa.array([m.name for m in metas]),
            "bitwidth": pa.array([m.bitwidth for m in metas], pa.int32()),
            "var_type": pa.array([m.var_type for m in metas]),
        })
        self.db.register("_sig", tbl)
        # A VCD aliases names onto shared ids. Keep one row per NAME: dropping
        # to one row per id loses 44% of the namespace, and a name an agent
        # cannot look up is indistinguishable to it from a signal that was quiet.
        self.db.execute(
            "INSERT INTO signal (signal_id, full_path, scope, name, bitwidth, var_type) "
            "SELECT signal_id, full_path, scope, name, bitwidth, var_type FROM ("
            "  SELECT *, row_number() OVER (PARTITION BY full_path ORDER BY signal_id) AS rn FROM _sig"
            ") WHERE rn = 1 AND full_path NOT IN (SELECT full_path FROM signal)"
        )
        self.db.unregister("_sig")

    def put_wave(self, table: pa.Table) -> None:
        self.db.register("_wave", table)
        self.db.execute(
            "INSERT INTO wave (signal_id, time_ps, cycle, value_u64, value_str) "
            "SELECT signal_id, time_ps, cycle, value_u64, value_str FROM _wave"
        )
        self.db.unregister("_wave")

    def put_commits(self, commits: Sequence) -> None:
        rows = [(i, c.cycle, c.priv, c.pc, c.insn, c.rd, c.wdata, c.regfile)
                for i, c in enumerate(commits)]
        if not rows:
            # A mutant that asserts before its first commit has an empty commit
            # log. That is evidence ("nothing ever retired"), not an error; an
            # empty insert raised, and the waveform arm could not run the task.
            return
        self.db.executemany("INSERT INTO commit_log VALUES (?,?,?,?,?,?,?,?)", rows)

    def put_assertion(self, message: str, src: str | None = None,
                      cycle: int | None = None) -> None:
        """Record a fired Chisel assertion as an oracle finding.

        There are two oracles, and the store must carry both or the arms do not
        see the same evidence. A deadlocked design never commits anything wrong,
        so `divergence` stays empty and a WaveQL agent asking first_divergence()
        would be told nothing at all -- while the control arm, reading the raw
        log, plainly sees "Assertion failed: Pipeline has hung." That asymmetry
        would have made the tool arm look worse for a reason that has nothing to
        do with the tool.
        """
        self.db.execute(
            "INSERT INTO divergence (kind, cycle, cycle_token, pc, spike, dut, reg, "
            "tolerated, raw) VALUES ('assertion', ?, NULL, NULL, ?, NULL, NULL, FALSE, ?)",
            [cycle, src, message],
        )

    def put_divergence(self, d, cycle: int | None = None, pc: int | None = None,
                       tolerated: bool = False) -> None:
        self.db.execute(
            "INSERT INTO divergence (kind, cycle, cycle_token, pc, spike, dut, reg, tolerated, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [d.kind, cycle, d.cycle_token, pc, d.spike, d.dut, d.reg, tolerated, d.line],
        )

    def set_meta(self, **kv: Any) -> None:
        for k, v in kv.items():
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [k, str(v)])

    def get_meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
        return r[0] if r else None

    # --- query --------------------------------------------------------------

    def _run(self, op: str, sql: str, params: Sequence = (), limit: int = DEFAULT_ROW_LIMIT,
             args: dict | None = None) -> Result:
        t0 = time.monotonic()
        # Fetch one more row than asked for: that is how truncation is *detected*
        # rather than assumed, so `truncated` is a fact and not a guess.
        cur = self.db.execute(f"SELECT * FROM ({sql}) LIMIT {int(limit) + 1}", list(params))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        truncated = len(rows) > limit
        rows = rows[:limit]
        ms = (time.monotonic() - t0) * 1000
        self._q += 1
        self.db.execute(
            "INSERT INTO query_log VALUES (?,?,?,?,?,?)",
            [self._q, op, json.dumps(args or {}, default=str), len(rows), truncated, ms],
        )
        return Result(cols, rows, truncated, op, ms)

    def _log_rows(self, op: str, columns: list[str], rows: list[tuple],
                  limit: int = DEFAULT_ROW_LIMIT, args: dict | None = None) -> Result:
        """Log an answer that did not come from SQL.

        Queries-per-fix is a headline metric, so an operation that bypassed the
        database must still appear in the query log or the metric quietly lies.
        """
        truncated = len(rows) > limit
        rows = rows[:limit]
        self._q += 1
        self.db.execute(
            "INSERT INTO query_log VALUES (?,?,?,?,?,?)",
            [self._q, op, json.dumps(args or {}, default=str), len(rows), truncated, 0.0])
        return Result(columns, rows, truncated, op, 0.0)

    def put_signal_src(self, rows: Sequence[tuple]) -> None:
        """signal path -> Chisel file/line/module, from firtool's locators."""
        if not rows:
            return
        self.db.executemany("INSERT INTO signal_src VALUES (?,?,?,?)", list(rows))

    @property
    def query_count(self) -> int:
        return self._q

    def first_divergence(self) -> Result:
        """What the oracles reported, and at which cycle.

        This is the operation the whole store exists for: it turns "the test
        failed" into a cycle, which every other query can then be aimed at. It
        reports BOTH oracles -- a Spike divergence (the DUT committed the wrong
        thing) and a fired Chisel assertion (the DUT broke an invariant its
        designers wrote down). A deadlock only ever trips the second.
        """
        return self._run("first_divergence", """
            SELECT d.kind, d.cycle, d.pc, d.spike AS detail, d.dut, d.reg,
                   d.tolerated, d.raw
            FROM divergence d WHERE NOT d.tolerated ORDER BY d.cycle NULLS LAST
        """, limit=10)

    # A waveform stores value CHANGES, so "what was signal X at cycle N" is an
    # ASOF question -- the last change at or before N -- and not a range filter.
    # This is not a detail. A range query over a stable signal returns ZERO rows,
    # which reads as "no data" when the truth is "it held its value the whole
    # time". Asking an agent to debug from that is worse than giving it nothing:
    # rob_state sitting in s_normal for the entire window is a fact, and the
    # naive query reports it as absence.

    def _scope_filter(self, scopes, signals, params: list) -> str:
        where = []
        if scopes:
            where.append("(" + " OR ".join("s.full_path LIKE ?" for _ in scopes) + ")")
            params += [f"{s.rstrip('.')}.%" for s in scopes]
        if signals:
            where.append("(" + " OR ".join("s.full_path = ?" for _ in signals) + ")")
            params += list(signals)
        return (" AND " + " AND ".join(where)) if where else ""

    def state_at(self, cycle: int, scopes: Sequence[str] | None = None,
                 signals: Sequence[str] | None = None, changed_only: bool = False,
                 limit: int = DEFAULT_ROW_LIMIT) -> Result:
        """The value in effect for each signal at a cycle.

        This is the operation "what was the machine doing at cycle N" -- the one
        a human answers by putting a cursor on a waveform. ``changed_only``
        narrows it to signals that changed *at* that cycle, which is usually what
        matters at a divergence and is far smaller.
        """
        params: list[Any] = [cycle]
        filt = self._scope_filter(scopes, signals, params)
        changed = " AND w.cycle = ?" if changed_only else ""
        if changed_only:
            params.append(cycle)
        return self._run("state_at", f"""
            SELECT s.full_path, w.cycle AS last_change_cycle, w.value_u64, w.value_str
            FROM wave w JOIN signal s USING (signal_id)
            WHERE w.cycle IS NOT NULL AND w.cycle <= ?{filt}{changed}
            QUALIFY row_number() OVER (
                PARTITION BY s.full_path ORDER BY w.cycle DESC, w.time_ps DESC) = 1
            ORDER BY s.full_path
        """, params, limit, {"cycle": cycle, "scopes": scopes, "signals": signals,
                             "changed_only": changed_only})

    def window(self, cycle_lo: int, cycle_hi: int, scopes: Sequence[str] | None = None,
               signals: Sequence[str] | None = None, limit: int = DEFAULT_ROW_LIMIT) -> Result:
        """Signal changes within a cycle range.

        Complements :meth:`state_at`: the state at ``cycle_lo`` plus these changes
        fully determines every cycle in the range. Returning only the changes
        keeps the answer small, which is the point.
        """
        params: list[Any] = [cycle_lo, cycle_hi]
        filt = self._scope_filter(scopes, signals, params)
        return self._run("window", f"""
            SELECT w.cycle, s.full_path, w.value_u64, w.value_str
            FROM wave w JOIN signal s USING (signal_id)
            WHERE w.cycle BETWEEN ? AND ?{filt}
            ORDER BY w.cycle, s.full_path
        """, params, limit, {"cycle_lo": cycle_lo, "cycle_hi": cycle_hi,
                             "scopes": scopes, "signals": signals})

    def trace_signal(self, signal: str, cycle_lo: int, cycle_hi: int,
                     limit: int = DEFAULT_ROW_LIMIT) -> Result:
        """One signal over a cycle range, including the value it entered with.

        The leading row is the last change at or before ``cycle_lo``, flagged
        ``entering``. Without it a signal that is stable across the whole range
        reports as empty, which is the single most misleading answer this store
        could give.
        """
        return self._run("trace_signal", """
            WITH sig AS (SELECT signal_id FROM signal WHERE full_path = ?),
            entering AS (
                SELECT w.cycle, w.time_ps, w.value_u64, w.value_str, TRUE AS entering
                FROM wave w WHERE w.signal_id = (SELECT signal_id FROM sig)
                  AND w.cycle IS NOT NULL AND w.cycle <= ?
                ORDER BY w.cycle DESC, w.time_ps DESC LIMIT 1),
            inside AS (
                SELECT w.cycle, w.time_ps, w.value_u64, w.value_str, FALSE AS entering
                FROM wave w WHERE w.signal_id = (SELECT signal_id FROM sig)
                  AND w.cycle > ? AND w.cycle <= ?)
            SELECT * FROM entering UNION ALL SELECT * FROM inside
            ORDER BY cycle, time_ps
        """, [signal, cycle_lo, cycle_lo, cycle_hi], limit,
            {"signal": signal, "cycle_lo": cycle_lo, "cycle_hi": cycle_hi})

    def commits_near(self, cycle: int, radius: int = 20, limit: int = DEFAULT_ROW_LIMIT) -> Result:
        """The DUT's retired instructions around a cycle -- the architectural story."""
        return self._run("commits_near", """
            SELECT cycle, priv, printf('0x%x', pc) AS pc, printf('0x%x', insn) AS insn,
                   regfile, rd, printf('0x%x', wdata) AS wdata
            FROM commit_log WHERE cycle BETWEEN ? AND ? ORDER BY cycle, seq
        """, [cycle - radius, cycle + radius], limit, {"cycle": cycle, "radius": radius})

    def sql(self, query: str, limit: int = DEFAULT_ROW_LIMIT) -> Result:
        """Read-only SQL over the joined store.

        Read-only is enforced by rejecting anything that is not a single SELECT or
        WITH. The store is evidence about a failing run; an agent that could
        mutate it could manufacture its own ground truth.
        """
        q = query.strip().rstrip(";")
        low = q.lower()
        if not (low.startswith("select") or low.startswith("with")):
            raise ValueError("only SELECT / WITH queries are allowed")
        for kw in ("insert", "update", "delete", "drop", "alter", "create", "attach", "copy", "pragma"):
            if _has_keyword(low, kw):
                raise ValueError(f"{kw!r} is not allowed in a WaveQL query")
        if ";" in q:
            raise ValueError("multiple statements are not allowed")
        return self._run("sql", q, (), limit, {"query": q})

    def close(self) -> None:
        self.db.close()


import re as _re


def _has_keyword(sql_lower: str, kw: str) -> bool:
    """Whole-word match, so a column called `updated_at` is not a rejected UPDATE."""
    return _re.search(rf"\b{kw}\b", sql_lower) is not None
