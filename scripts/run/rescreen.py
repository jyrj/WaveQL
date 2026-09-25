#!/usr/bin/env python3
"""Re-screen specific mutants, by id, with the current screen code.

For the mutants a fixed classifier defect affected: an assertion that fired
before Spike's banner was recorded as "invalid" (oracle missing) although the
assertion oracle had spoken. The sites are re-derived by enumerating the same
targets and matching mutant ids, so nothing is reconstructed by hand. Output rows
supersede the earlier rows for the same mutant (consumers keep the last row per
mutant_id).

  rescreen.py --chipyard var/workers/w17 --seed SEED --ids ID [ID ...] --out corpus/draw2-rescreen.jsonl
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from waveql.corpus.campaign import DEFAULT_STIMULI, _mutant_id                # noqa: E402
from waveql.corpus.screen import screen_site                                  # noqa: E402
from waveql.corpus.targets import ARCH_TARGETS, filter_sites                  # noqa: E402
from waveql.harness.chipyard import ChipyardEnv                                # noqa: E402
from waveql.mutator.engine import collect_sites                               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chipyard", required=True)
    ap.add_argument("--ids", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, required=True, help="the draw's seed, recorded on the row")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--run-workers", type=int, default=6)
    a = ap.parse_args()
    cy = ChipyardEnv.load(a.chipyard)
    sites = filter_sites(collect_sites(cy.root, [t for t, _ in ARCH_TARGETS]), record=[])
    by_id = {_mutant_id(s): s for s in sites}
    missing = [i for i in a.ids if i not in by_id]
    if missing:
        raise SystemExit(f"no site for {missing}")
    base = cy.root / "toolchains" / "riscv-tools" / "riscv-tests" / "build"
    elfs = [base / n for n in DEFAULT_STIMULI]
    out = ROOT / a.out
    for i in a.ids:
        res = screen_site(cy, by_id[i], elfs, jobs=a.jobs, seed=a.seed,
                          work_dir=ROOT / "corpus" / "work", run_workers=a.run_workers)
        row = json.loads(res.to_json())
        row["rescreen"] = "invalid -> re-screened after the pre-banner-assertion fix"
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"{i} -> {res.verdict} {res.kill_kind} {res.killing_stimulus}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
