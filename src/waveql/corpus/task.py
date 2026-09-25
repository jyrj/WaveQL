"""A BuggyBOOM task: one screened mutant, its evidence, and its ground truth.

A task is assembled from what the screen already produced, so building the corpus
costs no additional simulation. The same artifacts feed both arms -- the control
arm reads them as text, the WaveQL arm reads them as a joined store -- which is
what makes the comparison a comparison of *interfaces* rather than of evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from waveql.agent.arms import TextEvidence
from waveql.harness.chipyard import parse_commit_log
from waveql.ingest.wave import WaveReader
from waveql.store.store import WaveQLStore
from waveql.tool.api import BOOM_CORE, BOOM_TILE, TILE_SCOPES, WaveQLTool


@dataclass
class Task:
    task_id: str
    work_dir: Path
    killing_stimulus: str
    kill_kind: str
    # ground truth -- never shown to the agent
    true_module: str | None
    true_context: str
    true_before: str
    true_after: str
    true_path: str
    true_line: int
    mutation_class: str
    operator: str
    true_cycle: int | None
    vcd_path: Path | None
    divergence: str | None
    assertion: str | None
    assertion_src: str | None = None

    @staticmethod
    def from_screen_row(row: dict, work_root: Path) -> "Task":
        rec = row["record"]
        d = row.get("first_divergence") or {}
        wd = work_root / row["mutant_id"]
        vcd = row.get("vcd_path")
        return Task(
            task_id=row["mutant_id"], work_dir=wd,
            killing_stimulus=row.get("killing_stimulus") or "",
            kill_kind=row.get("kill_kind") or "",
            true_module=rec.get("module"), true_context=rec.get("context", ""),
            true_before=rec.get("before", ""), true_after=rec.get("after", ""),
            true_path=rec.get("path", ""), true_line=rec.get("line", 0),
            mutation_class=rec.get("mutation_class", ""), operator=rec.get("operator", ""),
            true_cycle=row.get("divergence_cycle"),
            vcd_path=Path(vcd) if vcd else None,
            divergence=(d.get("line") if d else None),
            assertion=row.get("assertion"),
            assertion_src=row.get("assertion_src"),
        )

    # --- the two arms' views of the same evidence ---------------------------

    def text_evidence(self) -> TextEvidence:
        stem = f"cosim-{self.killing_stimulus}"
        return TextEvidence(
            sim_log=self.work_dir / f"{stem}.log",
            commit_out=self.work_dir / f"{stem}.out",
            divergence=self.divergence, assertion=self.assertion,
        )

    # Scopes ingested per task. Scoped on purpose: a full-hierarchy MediumBoom
    # window carries ~72k signals and materialising all of them per task is
    # neither necessary nor affordable. These cover the commit point, the issue
    # and rename logic, the execution units and the memory system -- where this
    # corpus's defects actually are.
    #
    # NOTE the two prefixes. lsu/dcache are TILE-level, siblings of the core;
    # writing them as core-relative matched nothing and silently produced a
    # ROB-only store.
    DEFAULT_SCOPES = (
        # `core.*` is the core's OWN signals, not the whole core: dispatch and
        # decode stalls live there, and a hang is usually visible in them.
        "core.*",
        "core.rob", "core.rename_stage", "core.int_issue_unit",
        "core.mem_issue_unit", "core.iregister_read", "core.alu_exe_unit",
        "core.memExeUnit", "core.csr", "core.dispatcher",
        # The fetch and redirect logic, without the branch predictor. A
        # control-flow divergence shows up as the DUT executing from a PC Spike
        # never reaches, and this is where that is decided. `frontend.bpd` is
        # 5,827 signals and holds no defect in this corpus, so it stays out.
        "frontend.*",
        "lsu", "dcache",
    )

    def waveql_store(self, scopes: tuple[str, ...] | None = None,
                     strict: bool = True, gen_src: Path | None = None) -> WaveQLTool:
        """Ingest this task's collateral into a queryable store.

        `strict` makes a scope that matches NO signal an error rather than a
        silent omission. That is the whole lesson of this function's history.
        """
        st = WaveQLStore()
        if self.vcd_path and self.vcd_path.is_file():
            r = WaveReader(self.vcd_path)
            idx = r.cycle_index(BOOM_CORE + ".clock", BOOM_CORE + ".debug_tsc_reg")
            names: list[str] = []
            empty: list[str] = []
            for s in (scopes or self.DEFAULT_SCOPES):
                root = BOOM_TILE if s.split(".", 1)[0] in TILE_SCOPES else BOOM_TILE
                got = r.signals_under([f"{root}.{s}"])
                if not got:
                    empty.append(s)
                names += got
            if empty and strict:
                raise ValueError(
                    f"scopes matched no signals in {self.vcd_path}: {empty}. "
                    "An unmatched scope is silently empty in pywellen, so this is "
                    "raised rather than producing a store that looks healthy.")
            if names:
                st.put_signals(r.metadata(names))
                st.put_wave(r.changes(names, idx))
                if gen_src is not None:
                    self._attach_source(st, gen_src, names)
            st.set_meta(cycle_source=idx.source, window_lo=idx.first_cycle,
                        window_hi=idx.last_cycle)
        out = self.work_dir / f"cosim-{self.killing_stimulus}.out"
        if out.is_file():
            st.put_commits(parse_commit_log(out))
        if self.divergence:
            from waveql.harness.chipyard import _parse_divergence
            d = _parse_divergence(self.divergence)
            if d:
                st.put_divergence(d, cycle=self.true_cycle)
        if self.assertion:
            st.put_assertion(self.assertion, self.assertion_src, cycle=self.true_cycle)
        nl = None
        if gen_src is not None:
            from waveql.ingest.netlist import HierNetlist
            from waveql.ingest.srcmap import SourceMap
            nl = HierNetlist(gen_src, SourceMap(gen_src).inst2mod)
        return WaveQLTool(store=st, netlist=nl)

    @staticmethod
    def _attach_source(st, gen_src: Path, names: list[str]) -> None:
        """Fill signal_src from firtool's locators.

        Roughly 3% of signals do not resolve, and they are the honest 3%: SRAM
        macros and other blackboxes, which have no Chisel line to point at.
        """
        from waveql.ingest.srcmap import SourceMap
        sm = SourceMap(gen_src)
        have = {r[0] for r in st.db.execute("SELECT full_path FROM signal").fetchall()}
        rows = []
        for n in names:
            if n not in have:
                continue
            mod = sm.module_of(n)
            for ref in sm.resolve(n):
                rows.append((n, ref.chisel_file, ref.line, mod))
        st.put_signal_src(rows)

    def prompt(self) -> str:
        """Identical for every arm. Names no file, no module, no cycle."""
        what = ("the design disagreed with the golden ISA model"
                if self.kill_kind == "divergence"
                else "an assertion fired and the run aborted")
        return (
            f"A defect was introduced into one Chisel source file of this BOOM v3 "
            f"core. Running `{self.killing_stimulus}` on the mutated design, "
            f"{what}. The unmutated design passes this stimulus.\n\n"
            "Localize the defect using the tools, then answer in the required "
            "three-line format."
        )


def load_tasks(manifests: list[Path], work_root: Path,
               kinds: tuple[str, ...] = ("divergence", "assertion")) -> list[Task]:
    tasks = []
    for m in manifests:
        if not m.is_file():
            continue
        for line in m.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("verdict") != "killed":
                continue
            if row.get("kill_kind") not in kinds:
                continue
            tasks.append(Task.from_screen_row(row, work_root))
    return tasks
