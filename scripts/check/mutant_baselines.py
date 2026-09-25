#!/usr/bin/env python3
"""How many stimuli did each mutant already fail, before any repair?

`stimuli_passed` is recorded for every fix attempt and is uninterpretable on its
own: a patch scoring 9/12 has done nothing if the unpatched mutant also scored
9/12. The screen computes each stimulus's outcome and the ScreenResult keeps only
their NAMES, so the baseline was calculated and discarded.

It is recoverable. Every screened mutant left one cosim-<stimulus>.log and .out on
disk, and the harness's own pass rule -- exit 0, no divergence, no assertion, no
timeout, cosim actually running -- reads off those files. The exit code is not
recorded, so this reconstructs the four conditions that are, which is exactly the
set that distinguishes a failing stimulus from a passing one.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from waveql.harness.chipyard import (_ASSERTION, _COSIM_ACTIVE,   # noqa: E402
                                     _TIMEOUT, _parse_divergence)


def outcome(work: Path, stim: str) -> str:
    log, out = work / f"cosim-{stim}.log", work / f"cosim-{stim}.out"
    if not log.is_file():
        return "missing"
    text = log.read_text(errors="replace")
    joint = text + (out.read_text(errors="replace") if out.is_file() else "")
    if not _COSIM_ACTIVE.search(joint):
        return "no-cosim"
    if _parse_divergence(joint):
        return "diverged"
    if _ASSERTION.search(joint):
        return "assertion"
    if _TIMEOUT.search(joint):
        return "timeout"
    return "passed"


def main() -> int:
    rows = [json.loads(l)
            for f in sorted((ROOT / "corpus").glob("*.jsonl"))
            for l in f.read_text().splitlines() if l.strip()]
    killed = [r for r in rows if r["verdict"] == "killed"]
    out = []
    print(f"{'mutant':10s} {'bug':26s} {'baseline pass':>13s}  failing stimuli")
    print("-" * 92)
    for r in killed:
        work = ROOT / "corpus" / "work" / r["mutant_id"]
        res = {s: outcome(work, s) for s in r["stimuli_run"]}
        npass = sum(1 for v in res.values() if v == "passed")
        fails = [f"{s}:{v}" for s, v in res.items() if v not in ("passed",)]
        out.append({"mutant_id": r["mutant_id"], "bug": r["record"]["path"],
                    "baseline_passed": npass, "total": len(res), "outcomes": res})
        print(f"{r['mutant_id'][:8]:10s} {Path(r['record']['path']).name[:24]:26s} "
              f"{npass:>8d}/{len(res):<4d}  {', '.join(f.split(':')[0] for f in fails[:4])}"
              f"{' ...' if len(fails) > 4 else ''}")
    (ROOT / "measurements" / "mutant_baselines.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in out))
    b = [x["baseline_passed"] for x in out]
    print(f"\nbaseline passes: min {min(b)} max {max(b)} of 12 "
          f"-- a repair only counts as progress ABOVE its mutant's own number")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
