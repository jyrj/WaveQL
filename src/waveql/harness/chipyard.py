"""Driving the Chipyard build and the Verilator simulator from Python.

This module is the seam between the corpus generator and real hardware. Three
things here are not obvious and were each learned by running the tools:

1. **The environment cannot be inherited.** chipyard's ``env.sh`` sets ``RISCV``,
   activates a conda environment and puts *its own* Verilator (5.022) ahead of
   the host's (5.046) on PATH. ``common.mk`` hard-fails without ``RISCV``, and
   the debug simulator's custom main pokes Verilator-internal symbols
   (``__Vm_dumping``), so building against the host Verilator is not merely
   different, it is wrong. We therefore source ``env.sh`` in a real shell once
   and capture the resulting environment, rather than guessing at variables.

2. **Cosim exits the process on divergence.** ``cospike.cc:67`` is
   ``if (rval) exit(rval);``, called from inside a DPI callback. The VCD is
   truncated at exactly the cycle of interest, which is the one cycle we needed.
   So detection and capture are two passes over the same binary
   (:func:`run_cosim` then :func:`run_windows`), with ``+cospike-enable=0``
   on the capture pass so it runs to completion.

3. **A passing run proves nothing on its own.** A simulator built without
   ``SpikeCosim`` accepts ``+cospike-enable=1``, ignores it, and exits 0. That is
   how a whole corpus can silently screen as "no mutant is detectable".
   :func:`assert_cosim_present` checks the generated collateral, and
   :func:`run_cosim` refuses to report a pass it cannot substantiate.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import children


class ChipyardError(RuntimeError):
    pass


@dataclass(frozen=True)
class WaveWindow:
    """One PC-triggered capture window.

    Mirrors chipyard's ``WaveWindow`` and the plusargs the merged harness binder
    reads. The constraints are enforced rather than documented, because each one
    fails *silently* in hardware: a PC of 0 or a cycle count of 0 disables the
    slot (``enabled = pcs(i) =/= 0.U && cyc(i) =/= 0.U``), and a 0x-prefixed PC
    parses as 0 under the ``%h`` plusarg reader.
    """

    pc: int
    cycles: int
    n: int = 1

    def __post_init__(self) -> None:
        if self.pc == 0:
            raise ValueError("pc=0 silently disables the window slot")
        if self.cycles <= 0:
            raise ValueError("cycles must be > 0; 0 silently disables the window slot")
        if self.n < 1:
            raise ValueError("n is the Nth retired commit and must be >= 1")

    def plusargs(self, slot: int) -> list[str]:
        # Bare hex, no 0x: the slot uses a raw plusarg_reader with FORMAT="%h".
        return [f"+wf_pc_{slot}={self.pc:x}", f"+wf_n_{slot}={self.n}", f"+wf_cyc_{slot}={self.cycles}"]


MAX_WINDOWS = 64


def _find_conda(start: Path) -> Path:
    """Locate the repo's conda prefix by walking up from a checkout.

    Deriving it by fixed depth (root.parents[1]/tools/conda) only works for the
    primary checkout at thirdparty/chipyard. Worker clones live at
    var/workers/wN, a different depth, and the fixed form silently resolved to
    var/tools/conda -- which does not exist, so env.sh ran without conda, never
    set RISCV, and the failure surfaced as "the conda env is probably not built".
    """
    env = os.environ.get("WAVEQL_CONDA")
    if env and Path(env).is_dir():
        return Path(env)
    for base in [start, *start.parents]:
        cand = base / "tools" / "conda"
        if (cand / "bin" / "conda").is_file():
            return cand
    return start.parents[1] / "tools" / "conda"


@dataclass
class ChipyardEnv:
    """The environment a chipyard build or run needs, captured from a real shell."""

    root: Path
    env: dict[str, str]

    @classmethod
    def load(cls, root: str | os.PathLike[str], conda_prefix: str | os.PathLike[str] | None = None) -> "ChipyardEnv":
        root = Path(root).resolve()
        env_sh = root / "env.sh"
        if not env_sh.is_file():
            raise ChipyardError(f"{env_sh} missing -- has scripts/build/10_chipyard.sh run?")
        conda = Path(conda_prefix) if conda_prefix else _find_conda(root)

        # `set +u` because chipyard's generated conda activation scripts
        # dereference RISCV before defining it and abort under nounset.
        script = (
            f'export PATH="{conda}/bin:$PATH"; set +u; '
            f'source "{env_sh}" >/dev/null 2>&1; env -0'
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, timeout=300)
        if proc.returncode != 0:
            raise ChipyardError(f"sourcing {env_sh} failed: {proc.stderr.decode(errors='replace')[:2000]}")
        env = {}
        for entry in proc.stdout.decode(errors="replace").split("\0"):
            if "=" in entry:
                k, v = entry.split("=", 1)
                env[k] = v
        if not env.get("RISCV"):
            raise ChipyardError("env.sh did not set RISCV; the conda env is probably not built")
        return cls(root=root, env=env)

    @property
    def verilator(self) -> str | None:
        return shutil.which("verilator", path=self.env.get("PATH"))

    def generated_src(self, config: str, model_package: str = "chipyard.harness", model: str = "TestHarness") -> Path:
        return self.root / "sims" / "verilator" / "generated-src" / f"{model_package}.{model}.{config}"

    def simulator_path(self, config: str, debug: bool = True) -> Path:
        name = f"simulator-chipyard.harness-{config}" + ("-debug" if debug else "")
        return self.root / "sims" / "verilator" / name


# --- build -------------------------------------------------------------------

@dataclass
class BuildResult:
    config: str
    binary: Path
    seconds: float
    returncode: int
    log_path: Path

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.binary.is_file()


def build_simulator(
    cy: ChipyardEnv,
    config: str,
    *,
    jobs: int = 20,
    debug: bool = True,
    log_path: str | os.PathLike[str] | None = None,
    java_heap: str = "16G",
    timeout: int = 7200,
) -> BuildResult:
    """Run ``make`` for one config and return where the binary landed.

    ``RANDOM=0`` is not optional: chipyard's default preprocessor defines include
    ``RANDOMIZE_REG_INIT``/``RANDOMIZE_MEM_INIT``, so uninitialised state comes up
    random while Spike initialises to zero. Without it the lockstep check reports
    divergences that are not bugs, and a detectability screen built on that would
    label every mutant "killed" for the wrong reason.
    """
    env = dict(cy.env)
    env["JAVA_HEAP_SIZE"] = java_heap
    target = "debug" if debug else "default"
    cmd = [
        "make", target, f"-j{jobs}",
        f"CONFIG={config}",
        "EXTRA_SIM_PREPROC_DEFINES=+define+RANDOM=0",
    ]
    log_path = Path(log_path) if log_path else cy.root.parents[1] / "var" / "log" / f"build-{config}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with log_path.open("wb") as fh:
        # Its own process group, so stopping the harness stops the compilers too.
        proc = children.run(
            cmd, cwd=cy.root / "sims" / "verilator", env=env,
            stdout=fh, stderr=subprocess.STDOUT, timeout=timeout,
        )
    return BuildResult(
        config=config,
        binary=cy.simulator_path(config, debug),
        seconds=time.monotonic() - t0,
        returncode=proc.returncode,
        log_path=log_path,
    )


def assert_cosim_present(cy: ChipyardEnv, config: str) -> None:
    """Fail loudly if the built design has no Spike cosimulation in it.

    Chipyard dispatches harness binders through ``fn orElse up(HarnessBinders)``,
    so two binders matching the same port type silently drop one. Composing
    ``WithCospike`` with ``WithSelectiveWaveform`` produces a simulator with the
    waveform machinery and *no* ``SpikeCosim`` -- which still accepts
    ``+cospike-enable=1``, still exits 0, and still prints ``*** PASSED ***``.
    Every mutant screened against such a binary is recorded as undetectable.

    This check is cheap and runs before any screening campaign, because the
    failure it catches is invisible in every downstream signal.
    """
    gen = cy.generated_src(config) / "gen-collateral"
    if not gen.is_dir():
        raise ChipyardError(f"{gen} does not exist -- build {config} first")
    hits = [p.name for p in gen.iterdir() if "cospike" in p.name.lower()]
    if not hits:
        raise ChipyardError(
            f"config {config!r} generated NO cospike collateral in {gen}. "
            "The Spike lockstep check is absent, so every run will trivially "
            "'pass'. If this config composes WithCospike with another TracePort "
            "binder (e.g. WithSelectiveWaveform), one of them was silently "
            "dropped -- use WithCospikeAndSelectiveWaveform instead."
        )


# --- run ---------------------------------------------------------------------

_MISMATCH = re.compile(r"^.*\b(?:PC|wdata) mismatch.*$", re.M)
_PC_MM = re.compile(r"Cosim: ([0-9a-fA-F]+) PC mismatch spike ([0-9a-fA-F]+) != DUT ([0-9a-fA-F]+)")
_WDATA_MM = re.compile(r"Cosim: ([0-9a-fA-F]+) wdata mismatch reg (\d+) ([0-9a-fA-F]+) != ([0-9a-fA-F]+)")
_CYCLES = re.compile(r"Completed after\s+(\d+)\s+simulation cycles")
_TIMEOUT = re.compile(r"\(timeout\)")
_COSIM_ACTIVE = re.compile(r"^Cosim: (?:Configuring spike cosim|isa string:)", re.M)
# A Chisel `assert` that fires aborts the simulator through Verilog $stop. This
# is a SECOND mechanical oracle alongside Spike, and it catches a class Spike
# cannot: a design that never commits a wrong instruction because it stops
# committing at all. BOOM ships a liveness assert -- core.scala:2059,
# `assert(!(idle_cycles.value(13)), "Pipeline has hung.")` -- and a mutant that
# deadlocks the pipeline trips it.
_ASSERTION = re.compile(
    r"^\[?\d*\]?\s*%Error:.*?Assertion failed(?: in [^:]*)?: (?P<msg>.*)$"
    r"(?:\n\s*at (?P<src>\S+) (?P<expr>.*))?", re.M)

# The DUT commit log. Two shapes, because the two BOOM mixins emit different
# things and only one of them is usable as a join key:
#
#   WithBoomCommitLogPrintf        "%d 0x%x (0x%x)[ x%d 0x%x]"
#                                   priv pc    insn   rd  wdata      <- NO cycle
#   WithBoomHumanReadableCommitLog "C%d: " prefix, where the number is
#                                   debug_tsc_reg, the core's cycle counter
#
# Both are parsed; `cycle` is None for the first, and a caller that needs to join
# to a waveform must check for that rather than silently indexing by privilege
# level. (core.scala:2095-2098)
_COMMIT = re.compile(
    # Chisel's printf("C%d: ", debug_tsc_reg) right-pads the %d to the width of
    # the signal, so a real line looks like "C                  27: 3 0x...".
    # An unpadded `C(\d+):` matches nothing at all -- and, being an optional
    # group, it fails SILENTLY by reporting every commit as cycle-less.
    r"^(?:C\s*(?P<cycle>\d+):\s*)?(?P<priv>\d)\s+0x(?P<pc>[0-9a-f]+)\s+\(0x(?P<insn>[0-9a-f]+)\)"
    # The write-back group is anchored to end-of-line so it cannot match a
    # register mentioned inside the disassembly that spike-dasm splices in
    # ("auipc   a0, 0x0" sits between the opcode and the real "x10 0x...").
    # ...and the tail is permissive, because spike-dasm splices the disassembled
    # mnemonic in after the opcode. Anchoring the line end tightly here silently
    # DROPPED every instruction with no register write-back -- stores, branches,
    # fences -- which is 40% of the log and exactly the traffic an LSU bug lives in.
    r"(?:[^\n]*?\s(?P<rf>[xf])\s*(?P<rd>\d+)\s+0x(?P<wdata>[0-9a-f]+))?[^\n]*$",
    re.M,
)


@dataclass
class Divergence:
    """What cospike saw, and where -- with an explicit caveat about the cycle.

    ``cycle_token`` is kept verbatim because cospike prints the *same* ``cycle``
    variable in different radices depending on which message fires:

        cospike_impl.cc:678  PC mismatch     "%" PRIx64   -> HEX
        cospike_impl.cc:811  wdata mismatch  "%" PRIx64   -> HEX
        cospike_impl.cc:828  wdata mismatch  "%" PRIx64   -> HEX
        cospike_impl.cc:786  wdata mismatch  "%lld"       -> DECIMAL
        cospike_impl.cc:626  interrupt       "%" PRIu64   -> DECIMAL
        cospike_impl.cc:651  exception       "%" PRIu64   -> DECIMAL
        cospike_impl.cc:657  commit          "%" PRIu64   -> DECIMAL

    Line 786 and line 828 emit textually identical "wdata mismatch reg N X != Y"
    messages in different bases, so the radix cannot be recovered from the text.
    We therefore do not trust this field as *the* cycle: the authoritative cycle
    is BOOM's own ``debug_tsc_reg``, carried on every commit-log line by
    WithBoomHumanReadableCommitLog. ``cycle_hex``/``cycle_dec`` expose both
    readings so a caller can cross-check against the commit log rather than
    guess.
    """

    kind: str                  # "pc" | "wdata" | "unknown"
    line: str
    spike: str | None = None
    dut: str | None = None
    reg: int | None = None
    cycle_token: str | None = None

    @property
    def cycle_hex(self) -> int | None:
        try:
            return int(self.cycle_token, 16) if self.cycle_token else None
        except ValueError:
            return None

    @property
    def cycle_dec(self) -> int | None:
        return int(self.cycle_token) if self.cycle_token and self.cycle_token.isdigit() else None


@dataclass
class CosimOutcome:
    elf: str
    returncode: int
    passed: bool
    diverged: bool
    cosim_active: bool         # did the binary actually run Spike?
    divergence: Divergence | None
    sim_cycles: int | None
    commits: int
    seconds: float
    log_path: Path
    out_path: Path
    vcd_path: Path | None = None
    notes: list[str] = field(default_factory=list)
    # A fired Chisel assertion. Recorded separately from `diverged` because it is
    # a different oracle answering a different question: Spike says "you
    # committed the wrong thing"; an assertion says "you violated an invariant
    # the designers wrote down". A deadlocked pipeline trips the second and never
    # reaches the first, so without this field a hung mutant looks like a survivor.
    assertion: str | None = None
    assertion_src: str | None = None
    timed_out: bool = False


def _parse_divergence(text: str) -> Divergence | None:
    m = _MISMATCH.search(text)
    if not m:
        return None
    line = m.group(0).strip()
    if pc := _PC_MM.search(line):
        return Divergence("pc", line, spike=f"0x{pc.group(2)}", dut=f"0x{pc.group(3)}",
                          cycle_token=pc.group(1))
    if wd := _WDATA_MM.search(line):
        return Divergence("wdata", line, spike=f"0x{wd.group(3)}", dut=f"0x{wd.group(4)}",
                          reg=int(wd.group(2)), cycle_token=wd.group(1))
    return Divergence("unknown", line)


def run_simulator(
    cy: ChipyardEnv,
    binary: str | os.PathLike[str],
    elf: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    stem: str = "run",
    cosim: bool = True,
    verbose: bool = True,
    windows: list[WaveWindow] | None = None,
    vcd: bool = False,
    timeout: int = 3600,
    extra_plusargs: list[str] | None = None,
    loadmem: bool = True,
    max_cycles: int = 2_000_000,
) -> CosimOutcome:
    """Run one ELF and parse the verdict.

    stderr goes through ``spike-dasm`` exactly as chipyard's own run recipes do
    (``common.mk:419``), because the DUT commit log and the cospike messages are
    emitted there and the opcodes need disassembling to be readable.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path, out_path = out_dir / f"{stem}.log", out_dir / f"{stem}.out"
    vcd_path = out_dir / f"{stem}.vcd" if vcd else None

    argv = [str(binary), "+permissive"]
    argv += [f"+cospike-enable={1 if cosim else 0}", "+cospike-printf=0"]
    # +loadmem preloads the ELF into the memory model instead of streaming it in
    # over the serial-TL link, exactly as chipyard's own LOADMEM=1 flow does
    # (common.mk:354-360), where the binary is BOTH the loadmem elf and the
    # positional argument.
    #
    # It is not an optimisation, it is a requirement for any stimulus bigger than
    # a small ISA test. Streaming the ELF in leaves the core idle in the bootrom,
    # and BOOM asserts "Pipeline has hung" once idle_cycles reaches 2^13
    # (core.scala:2059). Every riscv-tests BENCHMARK aborted at 3 committed
    # instructions on the UNMUTATED design because of it.
    #
    # Measured on a clean build:
    #   rv64ui-p-add   16.6s / 99,186 cy  ->  1.2s /   5,976 cy   (14x)
    #   dhrystone     217.6s / 1.35M cy   -> 44.2s / 265,046 cy   (5x)
    # The removed cycles are pure serial-link load overhead.
    if loadmem:
        argv.append(f"+loadmem={elf}")
    # Bound the simulation in CYCLES, not just in wall-clock. A mutant can
    # livelock without ever tripping BOOM's hang assert -- it keeps making
    # progress, just never finishes -- and with no cycle limit such a run
    # occupied a screening worker for 27 MINUTES before the wall-clock timeout
    # fired. The TestDriver turns this into `reason = " (timeout)"; failure = 1`
    # (TestDriver.v:158), which is a verdict rather than a hang.
    #
    # 2,000,000 is ~3.3x the slowest clean stimulus (qsort, 603,556 cycles), so a
    # timeout means the mutant genuinely failed to make progress, not that the
    # budget was tight.
    if max_cycles:
        argv.append(f"+max-cycles={max_cycles}")
    for slot, w in enumerate(windows or []):
        if slot >= MAX_WINDOWS:
            raise ValueError(f"the harness provides {MAX_WINDOWS} window slots; got {len(windows or [])}")
        argv += w.plusargs(slot)
    if vcd_path is not None:
        argv.append(f"+vcdfile={vcd_path}")
    if verbose:
        argv.append("+verbose")
    argv += extra_plusargs or []
    argv += ["+permissive-off", str(elf)]

    t0 = time.monotonic()
    # `2> >(spike-dasm > out)` in shell form; done with a pipe here so the
    # dasm process is ours to wait on rather than a detached job.
    # A simulation that overruns its budget is a FAILED STIMULUS, not a crashed
    # harness. Letting TimeoutExpired propagate took down a whole shard of the
    # headline run mid-matrix: one dhrystone verification exceeded 900 s and the
    # runner died with a traceback, losing every task it had not reached. The
    # partial log is still worth parsing -- it says how far the design got.
    wall_timeout = False
    returncode = -1
    with log_path.open("wb") as logf, out_path.open("wb") as outf:
        dasm = subprocess.Popen(
            ["spike-dasm"], stdin=subprocess.PIPE, stdout=outf, env=cy.env
        )
        try:
            proc = children.run(
                argv, stdout=logf, stderr=dasm.stdin, stdin=subprocess.DEVNULL,
                env=cy.env, timeout=timeout,
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            wall_timeout = True
        finally:
            with contextlib.suppress(Exception):
                dasm.stdin.close()
            try:
                dasm.wait(timeout=600)
            except subprocess.TimeoutExpired:
                dasm.kill()
    seconds = time.monotonic() - t0

    text = log_path.read_text(errors="replace") + "\n" + out_path.read_text(errors="replace")
    divergence = _parse_divergence(text)
    assertion = _ASSERTION.search(text)
    timed_out = bool(_TIMEOUT.search(text)) or wall_timeout
    cosim_active = bool(_COSIM_ACTIVE.search(text))
    commits = len(_COMMIT.findall(text))
    cycles = int(m.group(1)) if (m := _CYCLES.search(text)) else None

    notes: list[str] = []
    if cosim and not cosim_active:
        notes.append(
            "COSIM REQUESTED BUT NEVER RAN: no 'Cosim:' banner in the output. "
            "This verdict cannot distinguish a correct design from an unchecked "
            "one; treat it as invalid, not as a pass."
        )
    passed = (returncode == 0 and divergence is None and assertion is None
              and not timed_out and (cosim_active or not cosim))

    return CosimOutcome(
        elf=str(elf), returncode=returncode, passed=passed,
        diverged=divergence is not None, cosim_active=cosim_active,
        assertion=(assertion.group("msg") or "").strip() if assertion else None,
        assertion_src=(assertion.group("src") or None) if assertion else None,
        timed_out=timed_out,
        divergence=divergence, sim_cycles=cycles, commits=commits,
        seconds=seconds, log_path=log_path, out_path=out_path,
        vcd_path=vcd_path if vcd_path and vcd_path.exists() else None,
        notes=notes,
    )


@dataclass(frozen=True)
class Commit:
    """One retired instruction as the DUT reported it."""

    cycle: int | None      # debug_tsc_reg; None unless the human-readable mixin is on
    priv: int              # privilege level at commit (0=U, 1=S, 3=M)
    pc: int
    insn: int
    rd: int | None = None
    wdata: int | None = None
    regfile: str | None = None   # "x" integer, "f" floating point


def parse_commit_log(path: str | os.PathLike[str]) -> list[Commit]:
    """Parse the DUT commit log.

    The cycle is what makes this file the join key of the whole project: it maps
    an *architectural* event (this instruction retired) onto a
    *microarchitectural* coordinate (at this cycle) that a waveform can be
    indexed by. It is present only when the design was built with
    WithBoomHumanReadableCommitLog; with the plain mixin every ``cycle`` is None
    and no join is possible. Callers that need the join must check.
    """
    rows: list[Commit] = []
    for m in _COMMIT.finditer(Path(path).read_text(errors="replace")):
        g = m.groupdict()
        rows.append(Commit(
            cycle=int(g["cycle"]) if g["cycle"] else None,
            priv=int(g["priv"]),
            pc=int(g["pc"], 16),
            insn=int(g["insn"], 16),
            rd=int(g["rd"]) if g["rd"] else None,
            wdata=int(g["wdata"], 16) if g["wdata"] else None,
            regfile=g["rf"],
        ))
    return rows
