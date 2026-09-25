#!/usr/bin/env bash
# Build the Chipyard environment this project simulates in. Resumable.
#
# Steps run (build-setup.sh numbering):
#   1  conda environment      -- the pinned toolchain (verilator, gcc, sbt, java)
#   2  chipyard submodules    -- rocket-chip, riscv-boom, testchipip, ...
#   3  toolchain collateral   -- SPIKE (the golden ISS), pk, riscv-tests, libgloss
#   5  chipyard scala precompile
#  10  CIRCT / firtool        -- Chisel -> SystemVerilog
#  11  repo clean-up
#
# Steps skipped and why:
#   4  ctags        -- code-navigation index; nothing in this project reads it.
#   6/7 FireSim     -- FPGA-accelerated simulation. This project only needs the
#                      software Verilator targets (VERILATOR, VERILATOR_DEBUG);
#                      the FireSim path also drags in the AWS FPGA SDK, the most
#                      failure-prone step in CHIA's own image build.
#   8/9 FireMarshal -- Linux workload builder. Our stimulus is bare-metal
#                      riscv-tests / riscv-dv ELFs, so no Linux image is needed.
# Reinstating FireSim later is additive: re-run without `-s 6 -s 7`.
#
# RESUMABILITY. build-setup.sh step 1 hard-refuses to run when .conda-env or
# .conda-lock-env already exist, so a failure *after* step 1 must not re-enter
# it. This script therefore probes for a populated .conda-env: if one is there
# the conda step is skipped and the environment is activated by hand; if not,
# both env dirs are cleared and step 1 runs from scratch. Network steps (1, 2,
# 3) are retried, because a transient `Connection reset by peer`
# mid-download is common (see scripts/setup/11_conda_tuning.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/env.sh"
CY="$CHIPYARD_DIR"
ATTEMPTS="${WAVEQL_BUILD_ATTEMPTS:-3}"

[ -d "$CY" ] || { echo "[10_chipyard] $CY missing" >&2; exit 1; }
[ -x "$WAVEQL_CONDA/bin/conda" ] || { echo "[10_chipyard] run scripts/setup/10_conda.sh first" >&2; exit 1; }

export NPROC="$WAVEQL_JOBS"              # build-util.sh turns this into MAKEFLAGS="-j N"

# JVM heap for Chisel elaboration. JAVA_HEAP_SIZE is the knob chipyard documents
# (variables.mk:25,255) and it feeds JAVA_TOOL_OPTIONS.
#
# Do NOT set SBT_OPTS or JAVA_OPTS here. chipyard invokes sbt as
#   SBT = java -jar scripts/sbt-launch.jar $(SBT_OPTS)        (variables.mk:266)
# so SBT_OPTS lands in sbt's *argument* list, not the JVM's: setting it to
# `-Xmx16G` makes sbt try to run a command called "-Xmx16G" and abort with
# "Not a valid command". Worse, overriding it silently discards chipyard's own
# -Dsbt.ivy.home / -Dsbt.boot.directory defaults (variables.mk:265), which is how
# a build ends up writing into $HOME instead of the repo. Cost of learning this
# the hard way: one failed 8-minute step.
export JAVA_HEAP_SIZE="${JAVA_HEAP_SIZE:-16G}"

# shellcheck disable=SC1091
source "$WAVEQL_CONDA/etc/profile.d/conda.sh"

# conda's generated activation scripts are not `set -u` clean: chipyard's
# .conda-env/etc/conda/activate.d/activate-riscv-tools.sh dereferences RISCV
# before defining it, which aborts the whole script under `set -u`. Activate with
# nounset relaxed, then restore it.
activate() {
    set +u
    conda activate "$1"
    local rc=$?
    set -u
    return $rc
}

echo "[10_chipyard] conda      : $(conda --version) at $WAVEQL_CONDA"
echo "[10_chipyard] chipyard   : $(git -C "$CY" rev-parse HEAD) ($(git -C "$CY" rev-parse --abbrev-ref HEAD))"
echo "[10_chipyard] NPROC      : $NPROC"
echo "[10_chipyard] free disk  : $(df -h "$ROOT" | awk 'NR==2{print $4}')"

cd "$CY"
for attempt in $(seq 1 "$ATTEMPTS"); do
    if [ -x "$CY/.conda-env/bin/python" ]; then
        echo "[10_chipyard] attempt $attempt: .conda-env is populated -> skipping step 1"
        activate "$CY/.conda-env"
        SKIP=(-s 1 -s 4 -s 6 -s 7 -s 8 -s 9)
    else
        echo "[10_chipyard] attempt $attempt: no usable .conda-env -> clearing and running step 1"
        rm -rf "$CY/.conda-env" "$CY/.conda-lock-env"
        activate base
        SKIP=(-s 4 -s 6 -s 7 -s 8 -s 9)
    fi
    echo "[10_chipyard] attempt $attempt started $(date -Is): build-setup.sh ${SKIP[*]}"
    if time ./build-setup.sh "${SKIP[@]}"; then
        echo "[10_chipyard] SUCCESS on attempt $attempt at $(date -Is)"
        exit 0
    fi
    echo "[10_chipyard] attempt $attempt FAILED at $(date -Is); retrying" >&2
    sleep 20
done
echo "[10_chipyard] exhausted $ATTEMPTS attempts" >&2
exit 1
