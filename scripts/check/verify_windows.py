#!/usr/bin/env python3
"""Does each capture window actually contain the failure it was aimed at?

Every stage of capture can succeed at what it checks while the window still ends
before the failure (a hang asserts 8,192 cycles after the last commit). A window
is only correct against the cycle the failure happens at, so this compares the two.

A hang is correct when the dump reaches `last_commit + 8192`, BOOM's liveness
threshold. Any other assertion is correct when the dump reaches the abort.
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HANG_IDLE_CYCLES = 8192


def last_commit(out: Path) -> int | None:
    if not out.is_file():
        return None
    for line in reversed(out.read_text(errors="replace").splitlines()):
        if line.startswith("C"):
            try:
                return int(line.split(":")[0][1:].strip())
            except ValueError:
                continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recapture", default=str(ROOT / "measurements" / "recapture.jsonl"))
    ap.add_argument("--stem", default="capture2")
    ap.add_argument("--screen", default=None, metavar="GLOB",
                    help="check the windows the SCREEN captured (repo-relative manifest "
                         "glob, e.g. the second draw) instead of a recapture file")
    a = ap.parse_args()
    if a.screen:
        rec = []
        for f in sorted(ROOT.glob(a.screen)):
            for l in f.read_text().splitlines():
                r = json.loads(l) if l.strip() else {}
                w = r.get("window") or {}
                if (r.get("verdict") == "killed" and r.get("vcd_path")
                        and r.get("kill_kind") in ("divergence", "assertion")):
                    rec.append({"mutant_id": r["mutant_id"], "vcd_path": r["vcd_path"],
                                "cycle_lo": w.get("cycle_lo"), "cycle_hi": w.get("cycle_hi"),
                                "vcd_bytes": r.get("vcd_bytes", 0), "kind": r["kill_kind"],
                                "covers": bool(r.get("window_covers_divergence")),
                                "div_cycle": r.get("divergence_cycle")})
    else:
        rec = [json.loads(l) for l in Path(a.recapture).read_text().splitlines() if l.strip()]
    scr = {json.loads(l)["mutant_id"]: json.loads(l)
           for f in sorted((ROOT / "corpus").glob("*.jsonl"))
           for l in f.read_text().splitlines() if l.strip()}
    print(f"{'mutant':10s} {'bug':28s} {'window':16s} {'last commit':>11s} "
          f"{'needs':>7s} {'MB':>6s}  verdict")
    print("-" * 96)
    ok = 0
    for r in rec:
        s = scr[r["mutant_id"]]
        work = Path(r["vcd_path"]).parent
        lc = last_commit(work / f"{a.stem}.out")
        log = work / f"{a.stem}.log"
        txt = log.read_text(errors="replace") if log.is_file() else ""
        hang = "Pipeline has hung" in txt
        need = (lc or 0) + HANG_IDLE_CYCLES
        if r.get("kind") == "divergence":
            # A wrong value or PC is correct when the dump spans the divergence
            # cycle, measured on the DUT's own counter when the screen captured it.
            good = r["covers"]
            ok += good
            print(f"{r['mutant_id'][:8]:10s} {Path(s['record']['path']).name[:26]:28s} "
                  f"{str(r['cycle_lo'])+'..'+str(r['cycle_hi']):16s} {'div@'+str(r['div_cycle']):>11s} "
                  f"{'':>7s} {r['vcd_bytes']/1e6:6.1f}  {'DIVERGENCE covered' if good else 'divergence MISSED'}")
            continue
        good = abs(r["cycle_hi"] - need) <= 3 if hang else ("Assertion failed" in txt)
        ok += good
        print(f"{r['mutant_id'][:8]:10s} {Path(s['record']['path']).name[:26]:28s} "
              f"{str(r['cycle_lo'])+'..'+str(r['cycle_hi']):16s} {str(lc):>11s} "
              f"{need:>7d} {r['vcd_bytes']/1e6:6.1f}  "
              f"{'HANG covered' if hang and good else 'hang MISSED' if hang else 'assert (not a hang)'}")
    print(f"\n{ok}/{len(rec)} dumps contain their failure")
    return 0 if ok == len(rec) else 1


if __name__ == "__main__":
    raise SystemExit(main())
