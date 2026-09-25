"""Run a screening campaign over many mutants, resumably.

A campaign is a long unattended job -- roughly three minutes per mutant, so a
few hours for a corpus -- and the things that make it trustworthy are all about
what happens when it is *not* going well:

* **The clean design is screened first, on every stimulus.** If the unmutated
  BOOM fails a stimulus, then every mutant also "fails" it and the whole corpus
  reads as 100% detectable. That is a silent, total corruption of the result, and
  it is cheap to rule out. A campaign refuses to start if the baseline is not
  clean.

* **Results are written one line at a time.** A crash three hours in must not
  cost three hours of compute, and a partially complete corpus is still a usable
  one.

* **Resume is by mutant id, not by position.** Re-running with a different
  ``--per-class`` must not re-screen what is already done, and must not silently
  renumber anything.

* **The stimulus ladder is ordered cheapest-first** and stops at the first kill.
  Most mutants die on the first stimulus; paying for eight when one suffices is
  the difference between a two-hour campaign and an eight-hour one.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from waveql.corpus.screen import ScreenResult, screen_site
from waveql.harness.chipyard import ChipyardEnv, assert_cosim_present, run_simulator
from waveql.corpus.targets import ARCH_TARGETS, Exclusion, filter_sites
from waveql.mutator.engine import collect_sites, sample_class_balanced
from waveql.mutator.operators import CLASSES, MutationSite

# Cheapest first, and broad: each entry is meant to reach a different unit, so a
# mutant that only breaks (say) the FPU or the atomics path still gets killed.
DEFAULT_STIMULI = (
    # Cheap ISA tests first: broad unit coverage, ~1-5 s each with +loadmem.
    "isa/rv64ui-p-add",        # integer ALU + commit path
    "isa/rv64ui-p-lw",         # loads
    "isa/rv64ui-p-sw",         # stores
    "isa/rv64um-p-mul",        # multiplier
    "isa/rv64ua-p-amoadd_d",   # atomics: LSU / MSHR
    "isa/rv64mi-p-csr",        # CSR contract, privilege
    "isa/rv64uf-p-fadd",       # FP pipeline
    "isa/rv64ui-p-fence_i",    # fences, instruction-cache coherence
    # Then real programs. THIS is where the kill power is. An ISA test retires
    # ~850 instructions and never fills a queue; dhrystone retires 207,741 and
    # actually creates the backpressure, dependency chains and cache pressure
    # that a handshake or ordering defect needs in order to become visible. The
    # first campaign screened six mutants against ISA tests alone and killed
    # NONE of them.
    "benchmarks/dhrystone.riscv",   # 207k commits: pointer chasing, string ops
    "benchmarks/mm.riscv",          # dense matmul: sustained load/store pressure
    "benchmarks/qsort.riscv",       # branchy, data-dependent control flow
    "benchmarks/spmv.riscv",        # sparse indirection: cache and MSHR pressure
)



class BaselineError(RuntimeError):
    pass


def verify_baseline(cy: ChipyardEnv, config: str, stimuli: Sequence[Path],
                    out_dir: Path, timeout: int = 1800) -> dict:
    """Every stimulus must pass on the UNMUTATED design, with cosim really running.

    Two distinct failures are caught here and they are not the same thing:
    a stimulus that legitimately fails on clean BOOM (excluded from the ladder),
    and a simulator with no Spike in it at all (every verdict meaningless).
    """
    assert_cosim_present(cy, config)
    sim = cy.simulator_path(config, debug=True)
    if not sim.is_file():
        raise BaselineError(f"no simulator at {sim}; build {config} first")

    report = {}
    for elf in stimuli:
        out = run_simulator(cy, sim, elf, out_dir / "baseline",
                            stem=f"base-{elf.name}", cosim=True, verbose=True,
                            vcd=False, timeout=timeout)
        report[elf.name] = {
            "passed": out.passed, "cosim_active": out.cosim_active,
            "diverged": out.diverged, "cycles": out.sim_cycles,
            "commits": out.commits, "seconds": round(out.seconds, 1),
        }
        if not out.cosim_active:
            raise BaselineError(
                f"{elf.name}: cosim did not run. Every screen verdict from this "
                f"binary would be meaningless. Notes: {out.notes}")
        if not out.passed:
            raise BaselineError(
                f"{elf.name} FAILS on the unmutated design "
                f"(diverged={out.diverged}). Every mutant would appear killed by "
                f"it. Remove it from the ladder or fix the build.")
    return report


def run_campaign(
    cy: ChipyardEnv,
    out_path: Path,
    *,
    targets: Sequence[str] | None = None,
    per_class: int = 5,
    seed: int = 20260904,
    config: str = "WaveQLMediumBoomV3Config",
    stimuli: Sequence[str] = DEFAULT_STIMULI,
    jobs: int = 20,
    classes: Sequence[str] = CLASSES,
    skip_baseline: bool = False,
    shard: tuple[int, int] | None = None,
    run_workers: int = 8,
    exclude_ids: frozenset[str] = frozenset(),
) -> list[ScreenResult]:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent / "work"

    base = cy.root / "toolchains" / "riscv-tools" / "riscv-tests" / "build"
    elfs = []
    for n in stimuli:
        p = base / n
        if not p.is_file():
            raise FileNotFoundError(f"stimulus {n} not built at {p}")
        elfs.append(p)

    if not skip_baseline:
        print(f"[campaign] verifying baseline on {len(elfs)} stimuli...", flush=True)
        rep = verify_baseline(cy, config, elfs, work)
        for name, r in rep.items():
            print(f"[campaign]   {name:22s} pass={r['passed']} cosim={r['cosim_active']} "
                  f"cycles={r['cycles']} commits={r['commits']} {r['seconds']}s", flush=True)
        (out_path.parent / "baseline.json").write_text(json.dumps(rep, indent=2))

    tgts = list(targets) if targets else [t for t, _ in ARCH_TARGETS]
    sites = collect_sites(cy.root, tgts)
    excluded: list[Exclusion] = []
    sites = filter_sites(sites, record=excluded)
    if exclude_ids:
        # A second draw must be disjoint from the first, or the corpus would
        # count one mutant twice. Filtering BEFORE sampling keeps each class's
        # quota filled from the sites not yet screened.
        n0 = len(sites)
        sites = [s for s in sites if _mutant_id(s) not in exclude_ids]
        print(f"[campaign] {n0 - len(sites)} site(s) already screened by an "
              f"earlier draw, excluded", flush=True)
    chosen = sample_class_balanced(sites, per_class=per_class, seed=seed, classes=list(classes))
    print(f"[campaign] targets: {len(tgts)} path(s)", flush=True)
    print(f"[campaign] {len(sites):,} architecturally-reachable sites "
          f"({len(excluded)} perf-only excluded) -> {len(chosen)} selected "
          f"({per_class}/class, seed {seed})", flush=True)
    (out_path.parent / "exclusions.json").write_text(json.dumps(
        [asdict(e) for e in excluded], indent=2))

    if shard is not None:
        idx, total = shard
        # Round-robin, not contiguous blocks: the sample is ordered by source
        # path, so contiguous shards would give one worker all of lsu.scala and
        # another all of rob.scala -- different build costs and, worse, a
        # per-worker class skew if a shard dies.
        chosen = [s for i, s in enumerate(chosen) if i % total == idx]
        print(f"[campaign] shard {idx + 1}/{total}: {len(chosen)} mutants", flush=True)

    done = _already_done(out_path)
    if done:
        print(f"[campaign] resuming: {len(done)} mutants already screened", flush=True)

    results: list[ScreenResult] = []
    t0 = time.monotonic()
    for i, site in enumerate(chosen, 1):
        mid = _mutant_id(site)
        if mid in done:
            continue
        print(f"[campaign] {i}/{len(chosen)} {site.path}:{site.line} "
              f"[{site.operator}] {site.mutation_class}", flush=True)
        try:
            res = screen_site(cy, site, elfs, config=config, jobs=jobs, seed=seed,
                              work_dir=work, run_workers=run_workers)
        except Exception as exc:                                   # noqa: BLE001
            # One bad mutant must not end a multi-hour campaign; record and go on.
            print(f"[campaign]   ERROR {type(exc).__name__}: {exc}", flush=True)
            with out_path.open("a") as fh:
                fh.write(json.dumps({"mutant_id": mid, "verdict": "error",
                                     "error": f"{type(exc).__name__}: {exc}",
                                     "site": asdict(site)}, default=str) + "\n")
            continue
        with out_path.open("a") as fh:
            fh.write(res.to_json() + "\n")
        results.append(res)
        el = time.monotonic() - t0
        print(f"[campaign]   -> {res.verdict.upper():12s} "
              f"build={res.build_seconds:.0f}s run={res.run_seconds:.0f}s "
              f"killer={res.killing_stimulus} | elapsed {el/60:.1f}m", flush=True)
    return results


def _mutant_id(site: MutationSite) -> str:
    import hashlib

    return hashlib.sha256(site.site_id.encode()).hexdigest()[:12]


def _already_done(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        try:
            out.add(json.loads(line)["mutant_id"])
        except Exception:                                          # noqa: BLE001
            continue
    return out
