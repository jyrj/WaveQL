#!/usr/bin/env python3
"""Export every killed mutant's causal explanation as data for the showcase page.

Same code path the agent uses -- first_divergence for a wrong value or PC,
stall_report for a hang -- so the page shows what the tool actually says, not a
curated rendering of it.
"""
import json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from waveql.corpus.task import load_tasks                          # noqa: E402

MODULE_OF = [  # signal-path prefix -> the processor block it belongs to
    ("rob.", "ROB"), ("rename_stage", "Rename"), ("dispatcher", "Dispatch"),
    ("dis_", "Dispatch"), ("int_issue_unit", "Issue"), ("mem_issue_unit", "Issue"),
    ("iregister_read", "RegRead"), ("alu_exe_unit", "Execute"),
    ("memExeUnit", "Execute"), ("FpPipeline", "FPU"), ("ll_wbarb", "Writeback"),
    ("lsu.", "LSU"), ("io_lsu", "LSU"), ("dcache", "D-cache"),
    ("frontend", "Frontend"), ("io_ifu", "Frontend"), ("csr", "CSR"),
]


def block(sig: str) -> str:
    for pre, name in MODULE_OF:
        if sig.startswith(pre):
            return name
    return "Core"


def main() -> int:
    gen = ROOT / "var" / "netlist-snapshot"
    recap = {}
    rp = ROOT / "measurements" / "recapture.jsonl"
    if rp.is_file():
        recap = {json.loads(l)["mutant_id"]: json.loads(l)
                 for l in rp.read_text().splitlines() if l.strip()}
    base = {}
    bp = ROOT / "measurements" / "mutant_baselines.jsonl"
    if bp.is_file():
        base = {json.loads(l)["mutant_id"]: json.loads(l)["baseline_passed"]
                for l in bp.read_text().splitlines() if l.strip()}
    out = []
    for t in load_tasks(sorted((ROOT / "corpus").glob("*.jsonl")),
                        ROOT / "corpus" / "work"):
        r = recap.get(t.task_id)
        if r and Path(r["vcd_path"]).is_file():
            t.vcd_path = Path(r["vcd_path"])
        t0 = time.monotonic()
        tool = t.waveql_store(gen_src=gen)
        db = tool.store.db
        fd = tool.first_divergence()
        head = dict(zip(fd.columns, fd.rows[0])) if fd.rows else {}
        links, facts, mode = [], {}, None
        if head.get("kind") == "assertion":
            rep = tool.stall_report(limit=200)
            facts = {x[1]: x[2] for x in rep.rows if x[0] in ("stall", "rob")}
            for x in rep.rows:
                if x[0] != "cause":
                    continue
                if x[1] in ("stuck_entry", "rob_empty"):
                    mode = x[1]; facts["mode_note"] = x[3]; continue
                d, sig = str(x[1]).split(" ", 1)
                note = str(x[3])
                links.append({"depth": int(d[1:]), "signal": sig, "value": x[2],
                              "stopped": note.split(";")[0].replace("last change ", "").strip(),
                              "chisel": note.split(";", 1)[1].strip() if ";" in note else "",
                              "block": block(sig)})
        else:
            for x in fd.rows[1:]:
                item = str(x[1])
                if item.startswith("lane") or item == "frontend.s0_vpc":
                    continue
                if item.startswith("--"):
                    links.append({"crossing": item.strip("- ").strip()}); continue
                d, sig = item.split(" ", 1)
                links.append({"depth": int(d[1:]), "signal": sig, "value": x[2],
                              "chisel": x[3] or "", "block": block(sig)})
        out.append({
            "id": t.task_id[:8], "stimulus": t.killing_stimulus,
            "kind": head.get("kind"), "cycle": head.get("cycle"),
            "dut": head.get("dut"), "golden": head.get("detail"), "reg": head.get("reg"),
            "mode": mode, "facts": facts, "links": links,
            "bug_file": Path(t.true_path).name, "bug_line": t.true_line,
            "bug_dir": Path(t.true_path).parent.name, "operator": t.operator,
            "baseline_passed": base.get(t.task_id),
            "signals": db.execute("SELECT count(*) FROM signal").fetchone()[0],
            "changes": db.execute("SELECT count(*) FROM wave").fetchone()[0],
            "vcd_mb": round(t.vcd_path.stat().st_size / 1e6) if t.vcd_path else 0,
            "seconds": round(time.monotonic() - t0, 1),
        })
        print(f"  {t.task_id[:8]} {head.get('kind'):10s} {len(links):2d} links "
              f"({out[-1]['seconds']:.0f}s)", flush=True)
    (ROOT / "measurements" / "demo_chains.json").write_text(json.dumps(out, indent=1))
    print(f"\nwrote {len(out)} chains")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
