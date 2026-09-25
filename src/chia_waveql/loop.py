"""The WaveQL loop: one linear pipeline, from a Chisel edit to a verified repair.

    ChiselMutateNode        inject a defect into Chisel SOURCE
            |
    DetectabilityScreenNode build it, run every stimulus under two oracles;
            |               capture a PC-triggered waveform window if it dies
    WaveQLIngestNode        join waveform + DUT commit log + golden trace
            |               + divergence into one cycle-indexed store
    WaveQLQueryTool         hand the agent a cost-bounded query surface
            |
         agent seat         localize, read the defective Chisel, propose a patch
            |
    FixVerifyNode           apply it, REBUILD the processor, re-run everything
            |
          ledger            localization + verified repair + what it cost

Everything the agent is scored on is recomputed here from ground truth it never
sees. The arms differ in exactly one thing -- the interface to the evidence --
because that is the experiment.

Run it:  python -m chia_waveql.loop --chipyard var/workers/w1 --per-class 1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from chia_waveql.ingest_node import WaveQLIngestNode
from chia_waveql.mutate_node import ChiselMutateNode
from chia_waveql.query_tool import WaveQLQueryTool
from chia_waveql.screen_node import DetectabilityScreenNode
from chia_waveql.state_def import BlameArtifact, MutantArtifact, RepairArtifact
from chia_waveql.verify_node import FixVerifyNode

from waveql.agent.arms import ARMS, REPAIR_SYSTEM, ControlTool, TextEvidence, arm_surface
from waveql.agent.seat import VertexSeat, cost_usd
from waveql.agent.source import SourceView
from waveql.analysis.metrics import score_blame
from waveql.corpus.campaign import DEFAULT_STIMULI
from waveql.corpus.verify import FixResult, _tampering, verify_applied
from waveql.harness.chipyard import ChipyardEnv
from waveql.mutator.engine import mutated
from waveql.mutator.operators import enumerate_sites

log = logging.getLogger("waveql.loop")

MID_NUDGE = ("You are halfway through your budget. If you have ANY plausible "
             "candidate, call propose_fix now -- you can replace it later, and "
             "an episode that ends with no proposal scores zero.")

REPAIR_NUDGE = ("You are out of budget. Call propose_fix NOW with your best "
                "candidate edit -- an unproposed fix scores zero -- and then give "
                "the three required lines.")


def _site_of(cy: ChipyardEnv, m: MutantArtifact):
    src = (cy.root / m.path).read_text()
    return next(s for s in enumerate_sites(m.path, src)
                if s.line == m.line and s.operator == m.operator and s.before == m.before)


def run_loop(
    chipyard_dir: str,
    *,
    per_class: int = 1,
    seed: int = 0,
    arms: Sequence[str] = ARMS,
    model: str | None = None,
    jobs: int = 11,
    max_turns: int = 40,
    deadline_s: float = 900.0,
    stimuli: Sequence[str] = DEFAULT_STIMULI,
    out_path: Path | None = None,
    repair: bool = True,
    gen_src: str | None = None,
) -> list[dict]:
    """Drive the whole pipeline and return one ledger row per (mutant, arm)."""
    model = model or os.environ.get("WAVEQL_MODEL_PRIMARY")
    if not model:
        raise RuntimeError("no model: pass model= or source configs/gcp.env")
    cy = ChipyardEnv.load(chipyard_dir)
    # firtool's locators and the module hierarchy live beside the build. With
    # them a waveform path resolves to a Chisel file:line and the netlist-backed
    # operations work; without them those four operations refuse politely.
    if gen_src is None:
        cand = sorted((cy.root / "sims" / "verilator" / "generated-src").glob(
            "*.TestHarness.*"))
        gen_src = str(cand[0]) if cand else None
    out_path = Path(out_path or (cy.root.parents[1] / "measurements" / "loop.jsonl"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    mutate, screen = ChiselMutateNode(), DetectabilityScreenNode()
    ingest, verifier = WaveQLIngestNode(), FixVerifyNode()
    seat = VertexSeat(model=model)

    mutants = mutate.select(chipyard_dir, per_class=per_class, seed=seed)
    log.info(f"selected {len(mutants)} mutant(s)")
    rows: list[dict] = []

    for m in mutants:
        t_start = time.monotonic()
        sc = screen.screen(chipyard_dir, m, stimuli=stimuli, jobs=jobs, seed=seed)
        if sc.verdict != "killed":
            # Not a task: no oracle saw it. Recorded anyway -- the survival rate
            # is a property of the operator set and belongs in the results.
            rows.append({"mutant": asdict(m), "screen": asdict(sc), "arm": None,
                         "stage": "screened-only"})
            _append(out_path, rows[-1])
            log.info(f"{m.mutant_id}: {sc.verdict}; not a task")
            continue

        store_art = ingest.ingest(
            m.mutant_id, sc.vcd_path or "",
            str(Path(sc.vcd_path).parent / f"cosim-{sc.killing_stimulus}.out")
            if sc.vcd_path else "",
            divergence=sc.divergence, assertion=sc.assertion,
            assertion_src=sc.assertion_src, divergence_cycle=sc.divergence_cycle,
            gen_src=gen_src)
        tool = WaveQLQueryTool(ingest.store, gen_src=gen_src)

        work = Path(sc.vcd_path).parent if sc.vcd_path else cy.root
        site = _site_of(cy, m)
        for arm in arms:
            # The mutation is held for the episode AND its verification: the agent
            # must read the DEFECTIVE source, and the rebuild must be of the source
            # it actually patched.
            with mutated(cy.root, site, seed=seed):
                sv = SourceView(root=cy.root) if repair else None
                ctl = ControlTool(TextEvidence(
                    sim_log=work / f"cosim-{sc.killing_stimulus}.log",
                    commit_out=work / f"cosim-{sc.killing_stimulus}.out",
                    divergence=(sc.divergence or {}).get("line"),
                    assertion=sc.assertion)) if arm == "control" else None
                decls, dispatch = arm_surface(arm, waveql=tool.tool, control=ctl, source=sv)
                ep = seat.run(task_id=m.mutant_id, arm=arm, system=REPAIR_SYSTEM,
                              prompt=_prompt(m, sc), declarations=decls,
                              dispatch=dispatch, max_turns=max_turns,
                              deadline_s=deadline_s, final_nudge=REPAIR_NUDGE,
                              mid_nudge=MID_NUDGE, seed=seed)
                proposal = sv.proposals[-1] if (sv and sv.proposals) else None
                fr = FixResult(task_id=m.mutant_id, arm=arm, seed=seed,
                               verdict="no-proposal", stimuli_total=len(stimuli),
                               proposal=proposal)
                if proposal:
                    why = _tampering(proposal["old"], proposal["new"])
                    if why:
                        fr.verdict, fr.reason = "rejected", why
                    else:
                        fr = verify_applied(
                            cy, site, proposal,
                            [cy.root / "toolchains/riscv-tools/riscv-tests/build" / s
                             for s in stimuli],
                            res=fr, work=work / f"fix-{arm}", jobs=jobs)

            b = score_blame(ep.answer, true_module=m.module, true_context=m.context,
                            true_before=m.before, true_cycle=sc.divergence_cycle)
            blame = BlameArtifact(
                mutant_id=m.mutant_id, arm=arm, seed=seed, model=model,
                module=None, signal=None, cycle=None,
                module_hit=b.module, signal_hit=b.signal, cycle_hit=b.cycle,
                queries=ep.queries, turns=ep.turns, tokens=ep.usage.total,
                cost_usd=cost_usd(model, ep.usage), seconds=ep.wall_seconds,
                stop_reason=ep.stop_reason, error=ep.error,
                tool_calls=[c.name for c in ep.tool_calls])
            row = {"mutant": asdict(m), "screen": asdict(sc), "store": asdict(store_art),
                   "blame": asdict(blame),
                   "repair": asdict(RepairArtifact(
                       mutant_id=fr.task_id, arm=fr.arm, seed=fr.seed,
                       verdict=fr.verdict, exact_revert=fr.exact_revert,
                       same_file_as_bug=fr.same_file_as_bug,
                       same_line_as_bug=fr.same_line_as_bug,
                       stimuli_passed=fr.stimuli_passed, stimuli_total=fr.stimuli_total,
                       first_failure=fr.first_failure, reason=fr.reason,
                       proposal=fr.proposal, build_seconds=fr.build_seconds,
                       run_seconds=fr.run_seconds)),
                   "arm": arm, "stage": "complete",
                   "wall_seconds": round(time.monotonic() - t_start, 1),
                   "answer": (ep.answer or "")[:800]}
            rows.append(row)
            _append(out_path, row)
            log.info(f"{m.mutant_id} [{arm}] blame(mod={int(b.module)} "
                     f"sig={int(b.signal)} cyc={int(b.cycle)}) repair={fr.verdict}")
    return rows


def _prompt(m: MutantArtifact, sc) -> str:
    what = ("the design disagreed with the golden ISA model"
            if sc.kill_kind == "divergence" else "an assertion fired and the run aborted")
    return (f"A defect was introduced into one Chisel source file of this BOOM v3 "
            f"core. Running `{sc.killing_stimulus}` on the mutated design, {what}. "
            f"The unmutated design passes this stimulus.\n\n"
            "Localize the defect and repair it using the tools.")


def _append(path: Path, row: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the WaveQL CHIA loop end to end.")
    ap.add_argument("--chipyard", required=True)
    ap.add_argument("--per-class", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--arms", nargs="+", default=list(ARMS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--jobs", type=int, default=11)
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--deadline", type=float, default=900.0)
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--gen-src", default=None,
                    help="generated sources for this build; enables signal-to-Chisel, "
                         "drivers() and why(). Defaults to the config's own tree.")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    run_loop(a.chipyard, per_class=a.per_class, seed=a.seed, arms=a.arms,
             model=a.model, jobs=a.jobs, max_turns=a.max_turns,
             deadline_s=a.deadline, repair=not a.no_repair, gen_src=a.gen_src,
             out_path=Path(a.out) if a.out else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
