#!/usr/bin/env python3
"""Verified fix rate: can the agent actually repair the processor?

The headline outcome, and the one localization cannot stand in for. Each episode gets the same evidence tools as the localization ablation, plus
read access to the defective Chisel source and one structured edit. The patch is
then applied, the processor rebuilt, and every stimulus re-run under Spike
lockstep. The agent's own claim counts for nothing.

The mutation is held for the whole episode AND its verification: the agent must
read the DEFECTIVE source, and the rebuild must be of the source the agent
actually patched.
"""
import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from waveql.agent.arms import ARMS, REPAIR_SYSTEM, ControlTool, arm_surface   # noqa: E402
from waveql.agent.seat import VertexSeat, cost_usd                            # noqa: E402
from waveql.agent.source import SourceView                                    # noqa: E402
from waveql.analysis.metrics import score_blame                               # noqa: E402
from waveql.corpus.campaign import DEFAULT_STIMULI                            # noqa: E402
from waveql.corpus.task import load_tasks                                     # noqa: E402
from waveql.corpus.verify import FixResult, verify_applied, _tampering        # noqa: E402
from waveql.harness.chipyard import ChipyardEnv                               # noqa: E402
from waveql.mutator.engine import default_resolver, mutated                   # noqa: E402
from waveql.mutator.operators import enumerate_sites                          # noqa: E402

MID_NUDGE = ("You are halfway through your budget. If you have ANY plausible "
             "candidate, call propose_fix now -- you can replace it later, and "
             "an episode that ends with no proposal scores zero.")


def site_for(cy: ChipyardEnv, task) -> object:
    """Recover the exact MutationSite a task was generated from."""
    src = (cy.root / task.true_path).read_text()
    for s in enumerate_sites(task.true_path, src):
        if (s.line == task.true_line and s.operator == task.operator
                and s.before == task.true_before):
            return s
    raise LookupError(f"cannot re-derive the mutation site for {task.task_id}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chipyard", default=str(ROOT / "var" / "workers" / "w1"))
    ap.add_argument("--manifest", action="append", default=None)
    ap.add_argument("--work", default=str(ROOT / "corpus" / "work"))
    ap.add_argument("--arms", nargs="+", default=list(ARMS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--seeds", type=int, nargs="+", default=[1])
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--deadline", type=float, default=900.0)
    ap.add_argument("--jobs", type=int, default=11)
    ap.add_argument("--shard", default=None, metavar="I/N")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "measurements" / "episodes-run.jsonl"))
    ap.add_argument("--gen-src", default=str(ROOT / "var" / "netlist-snapshot"),
                    help="generated sources, for signal-to-Chisel and the netlist tools")
    a = ap.parse_args()

    model = a.model or os.environ.get("WAVEQL_MODEL_PRIMARY")
    if not model:
        print("no model: pass --model or source configs/gcp.env", file=sys.stderr)
        return 2

    cy = ChipyardEnv.load(a.chipyard)
    manifests = [Path(m) for m in (a.manifest or
                 sorted(str(p) for p in (ROOT / "corpus").glob("*.jsonl")))]
    tasks = load_tasks(manifests, Path(a.work))
    # Prefer a re-captured dump where one exists. For a hang the original window
    # ends about 500 cycles after the last commit and the liveness assert fires
    # 8,192 later, so the original dump records healthy execution only.
    rp = ROOT / "measurements" / "recapture.jsonl"
    if rp.is_file():
        recap = {json.loads(l)["mutant_id"]: json.loads(l)
                 for l in rp.read_text().splitlines() if l.strip()}
        swapped = 0
        for t in tasks:
            r = recap.get(t.task_id)
            if r and r.get("vcd_path") and Path(r["vcd_path"]).is_file():
                t.vcd_path = Path(r["vcd_path"])
                swapped += 1
        print(f"using re-captured windows for {swapped} task(s)", flush=True)
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        tasks = [t for k, t in enumerate(tasks) if k % n == i]
    if a.limit:
        tasks = tasks[: a.limit]

    base = cy.root / "toolchains" / "riscv-tools" / "riscv-tests" / "build"
    stimuli = [base / s for s in DEFAULT_STIMULI]
    missing = [str(s) for s in stimuli if not s.is_file()]
    if missing:
        print(f"missing stimuli: {missing}", file=sys.stderr)
        return 2

    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out_path.is_file():
        for l in out_path.read_text().splitlines():
            if l.strip():
                r = json.loads(l)
                # A cell killed by a 504 or a quota refusal was never scored, so
                # it is not done -- it is missing. Recording it as done leaves a
                # permanent hole in the matrix, and at the observed 35% transport
                # loss rate that is a third of the design.
                if r.get("verdict") == "lost" or r.get("stop") == "error":
                    continue
                done.add((r["task_id"], r["arm"], r["seed"]))

    seat = VertexSeat(model=model)
    print(f"{len(tasks)} task(s) x {len(a.arms)} arm(s) x {len(a.seeds)} seed(s) "
          f"on {Path(a.chipyard).name}, model {model}\n", flush=True)

    for t in tasks:
        try:
            site = site_for(cy, t)
        except LookupError as e:
            print(f"  SKIP {t.task_id[:8]}: {e}", flush=True)
            continue
        wq = None
        for arm in a.arms:
            for seed in a.seeds:
                if (t.task_id, arm, seed) in done:
                    continue
                if arm == "waveql" and wq is None:
                    wq = t.waveql_store(gen_src=Path(a.gen_src) if a.gen_src else None)
                t0 = time.monotonic()
                # Hold the mutation for the episode AND its verification.
                with mutated(cy.root, site, seed=seed):
                    sv = SourceView(root=cy.root)
                    ctl = ControlTool(t.text_evidence()) if arm == "control" else None
                    decls, dispatch = arm_surface(arm, waveql=wq, control=ctl, source=sv)
                    ep = seat.run(task_id=t.task_id, arm=arm, system=REPAIR_SYSTEM,
                                  prompt=t.prompt(), declarations=decls,
                                  dispatch=dispatch, max_turns=a.max_turns,
                                  deadline_s=a.deadline, seed=seed,
                                  final_nudge=(
                                      "You are out of budget. Call propose_fix NOW "
                                      "with your best candidate edit -- an unproposed "
                                      "fix scores zero -- and then give the three "
                                      "required lines."),
                                  mid_nudge=MID_NUDGE,
                                  commit_tools=["propose_fix", "read_source",
                                                "search_source", "list_files"])
                    proposal = sv.proposals[-1] if sv.proposals else None
                    fr = FixResult(task_id=t.task_id, arm=arm, seed=seed,
                                   verdict="no-proposal", stimuli_total=len(stimuli),
                                   proposal=proposal)
                    if proposal:
                        why = _tampering(proposal["old"], proposal["new"])
                        if why:
                            fr.verdict, fr.reason = "rejected", why
                        else:
                            fr = verify_applied(
                                cy, site, proposal, stimuli, res=fr,
                                work=Path(a.work).parent / "fix" /
                                     f"{t.task_id}-{arm}-s{seed}",
                                jobs=a.jobs)
                    elif ep.error:
                        # The episode did not end, it was KILLED -- a 504 or a
                        # quota refusal after the retries were exhausted. Calling
                        # that "no-proposal" blames the agent for a transport
                        # failure and puts a zero in a cell that was never
                        # scored. It is `lost`, and the analysis excludes it.
                        fr.verdict = "lost"
                        fr.reason = (f"episode lost to a transport error after "
                                     f"{ep.queries} queries: {ep.error[:120]}")
                    else:
                        fr.reason = (
                            f"propose_fix was called but every anchor was rejected "
                            f"({len(sv.rejected)} attempt(s): "
                            f"{', '.join(sorted({r['why'] for r in sv.rejected}))})"
                            if sv.rejected else "the agent never called propose_fix")

                sc = score_blame(ep.answer, true_module=t.true_module,
                                 true_context=t.true_context,
                                 true_before=t.true_before, true_cycle=t.true_cycle)
                row = {**asdict(fr), "model": model, "wall_seconds": round(time.monotonic() - t0, 1),
                       "episode_seconds": round(ep.wall_seconds, 1),
                       "queries": ep.queries, "turns": ep.turns,
                       "tokens": ep.usage.total, "cost_usd": cost_usd(model, ep.usage),
                       # The split is what a price is applied to. Recording only
                       # the total made the preview seat's 67 episodes impossible
                       # to price exactly after the fact.
                       "tokens_prompt": ep.usage.prompt, "tokens_cached": ep.usage.cached,
                       "tokens_output": ep.usage.output, "tokens_thought": ep.usage.thought,
                       "stop": ep.stop_reason, "error": ep.error,
                       "module": sc.module, "signal": sc.signal, "cycle": sc.cycle,
                       "kill_kind": t.kill_kind, "mutation_class": t.mutation_class,
                       "operator": t.operator, "true_module": t.true_module,
                       "true_path": t.true_path, "true_line": t.true_line,
                       "answer": (ep.answer or "")[:800],
                       "tool_calls": [c.name for c in ep.tool_calls],
                       "rejected_proposals": len(sv.rejected)}
                # Keep the transcript. A run that ends "no-proposal" after 42
                # queries is a question about what the agent was doing, and the
                # ledger row cannot answer it -- the first such episode cost a
                # blind guess about budget because there was nothing to read.
                tdir = out_path.parent / "transcripts"
                tdir.mkdir(parents=True, exist_ok=True)
                tpath = tdir / f"{t.task_id[:8]}-{arm}-s{seed}.json"
                tpath.write_text(json.dumps({
                    "task_id": t.task_id, "arm": arm, "seed": seed,
                    "verdict": fr.verdict, "stop": ep.stop_reason,
                    "turns": ep.turns, "queries": ep.queries,
                    "true_path": t.true_path, "true_line": t.true_line,
                    "tool_calls": [{"turn": c.turn, "name": c.name, "args": c.args,
                                    "chars": c.result_chars, "truncated": c.truncated}
                                   for c in ep.tool_calls],
                    "transcript": ep.transcript}, indent=1, default=str))
                row["transcript"] = str(tpath)
                with out_path.open("a") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
                print(f"  {t.task_id[:8]} {arm:8s} s{seed} {fr.verdict:12s} "
                      f"{fr.stimuli_passed}/{fr.stimuli_total} pass "
                      f"revert={int(fr.exact_revert)} q={ep.queries:2d} "
                      f"tok={ep.usage.total:7,} {row['wall_seconds']:6.0f}s"
                      + (f"  [{fr.reason[:52]}]" if fr.reason else ""), flush=True)
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
