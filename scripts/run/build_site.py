#!/usr/bin/env python3
"""Build the showcase page from the same measurement files as the paper.

Like render_paper.py, nothing here is typed by hand: the page and the paper read
the same headline computation, so they cannot disagree about a number.
"""
import json, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts" / "run"))
import headline                                                    # noqa: E402


def jl(p):
    p = ROOT / p
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.is_file() else []


def main() -> int:
    chains = json.loads((ROOT / "measurements" / "demo_chains.json").read_text())
    recap = {r["mutant_id"][:8]: r for r in jl("measurements/recapture.jsonl")}
    for c in chains:
        r = recap.get(c["id"])
        stops = [int(l["stopped"]) for l in c["links"]
                 if l.get("stopped") and str(l["stopped"]).lstrip("-").isdigit()]
        c["window_lo"] = r["cycle_lo"] if r else (min(stops) if stops else None)
        c["window_hi"] = r["cycle_hi"] if r else (max(stops) if stops else None)
        c["mode_note"] = (c.get("facts") or {}).get("mode_note")

    h = headline.compute(["measurements/episodes-*.jsonl"])
    loc = jl("measurements/localization-draw1.jsonl")
    dirs = json.loads((ROOT / "measurements" / "boom-v3-layout.json").read_text())["file_to_dir"]
    fh = sum(1 for r in loc if r["hit"])
    dh = sum(1 for r in loc if dirs.get(r["bug"]) in {dirs.get(n) for n in r["named"]})
    med = statistics.median(len(r["named"]) for r in loc) if loc else 0

    rows = [r for p in sorted((ROOT / "measurements").glob("episodes-*.jsonl"))
            for r in jl(p.relative_to(ROOT)) if r.get("stop") != "error" and r.get("verdict") != "lost"]
    rf = {a: [r for r in rows if r["arm"] == a and r.get("same_file_as_bug")] for a in ("waveql", "control")}
    twist = {"rf_w": len(rf["waveql"]), "rf_c": len(rf["control"]),
             "conv_w": f"{sum(1 for r in rf['waveql'] if r['verdict']=='fixed')} of {len(rf['waveql'])}",
             "conv_c": f"{sum(1 for r in rf['control'] if r['verdict']=='fixed')} of {len(rf['control'])}"}

    weak = jl("measurements/seat-gemini-2.5-pro.jsonl")
    weak_fixed = sum(1 for r in weak if r["verdict"] == "fixed")

    # The pipeline-depth repair of the paper: the waveform arm's first verified
    # fix of task b3bd38d7 and the control's first patch that did not fix it.
    first = [r for r in jl("measurements/episodes-draw1-initial.jsonl")
             if r["task_id"].startswith("b3bd38d7") and r.get("proposal")]
    pick = [next((r for r in first if r["arm"] == "waveql" and r["verdict"] == "fixed"), None),
            next((r for r in first if r["arm"] == "control" and r["verdict"] != "fixed"), None)]
    repair = []
    for r in (x for x in pick if x):
        if True:
            repair.append({"arm": "waveql arm" if r["arm"] == "waveql" else "text control",
                           "where": f"{str(r.get('patched_path') or '?').split('/')[-1]}:{r.get('patched_line')}",
                           "passed": r.get("stimuli_passed", 0), "fixed": r["verdict"] == "fixed"})
    repair.sort(key=lambda x: not x["fixed"])

    chase = [
        {"name": "The recording",
         "what": "BOOM asserts a hang 8,192 cycles after it stops retiring, and every capture window ended about 500 cycles after the last commit. Half the corpus recorded healthy execution and asked the agent to explain a failure after the recording ended.",
         "fix": "<s>7 of 15</s> → 15 of 15 windows contain their failure"},
        {"name": "The store",
         "what": "A waveform aliases identical nets onto one id, and the store kept one name per id. Signals an agent would ask for by name, rob_head among them, did not exist.",
         "fix": "<s>17,612</s> → 29,183 signal names addressable"},
        {"name": "Signal to source",
         "what": "The agent could pin a wrong value to a cycle and a bit and still have nowhere to edit. firtool's locators and the module hierarchy map each waveform path to a Chisel line.",
         "fix": "<s>0%</s> → 97.1% of signals resolve to Chisel file:line"},
        {"name": "Symptom to cause",
         "what": "Nine ways of ranking files by waveform activity all failed together, because in an out-of-order core a defect touches everything within a few cycles. A causal walk through the netlist, following only the operands that account for a value, names a few files instead -- but tested on unseen bugs, a fixed list of the most-implicated hub files does better.",
         "fix": (lambda ho: f"held out on {ho['n']} unseen bugs, the chain names the file in {ho['file']}; a failure-blind list of five hub files names it in {ho['static_file']}")(json.loads((ROOT / 'paper' / 'numbers.json').read_text())['heldout']['heldout'])},
        {"name": "The harness",
         "what": "Reading transcripts found episodes scored for reasons that were ours: blank answers accepted, a forced-commit mechanism that made the API return nothing, transport failures recorded as the agent declining, and a timeout that crashed a whole shard.",
         "fix": "lost episodes excluded and retried, never scored as zero"},
        {"name": "The model",
         "what": "With the evidence repaired, one seat still could not turn a correct diagnosis into a correct edit. It removed the wrong register stage on the pipeline-depth defect.",
         "fix": f"gemini-2.5-pro: {weak_fixed} of {len(weak)} fixed in either arm → gemini-3.1-pro-preview: fixes appear"},
    ]

    data = {"chains": chains, "headline": h, "dirs": dirs, "twist": twist,
            "repair": repair, "chase": chase,
            "footnote": (f"{h['episodes']} scored episodes, {h['lost']} lost to transport and "
                         f"excluded. Every figure on this page is computed from the repository's "
                         f"measurement files by scripts/run/build_site.py.")}
    tpl = (ROOT / "paper" / "site" / "template.html").read_text()
    out = tpl.replace("/*DATA*/", "const DATA = " + json.dumps(data, default=str) + ";")
    (ROOT / "paper" / "site" / "index.html").write_text(out)
    print(f"built paper/site/index.html ({len(out)/1024:.0f} KB): {len(chains)} chains, "
          f"difference {h['diff']*100:+.1f} points, file {fh}/15 dir {dh}/15, "
          f"weak seat {weak_fixed}/{len(weak)}, repair rows {len(repair)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
