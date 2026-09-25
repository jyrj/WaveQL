#!/usr/bin/env bash
# Build one Verilator simulator from the Chipyard tree.
#
#   scripts/build/20_sim.sh [CONFIG] [TARGET]
#     CONFIG  default: chipyard.harness.WithSelectiveWaveform_MediumBoomV3CosimConfig
#     TARGET  default: debug   (debug = VCD-capable; use `default` for the fast sim)
#
# CONFIG COMPOSITION. chipyard passes `--legacy-configs <pkg>:<CONFIG>` to the
# generator (common.mk:155). `<CONFIG>` is split on "_" and each part becomes a
# Config class, left-to-right decreasing precedence
# (tools/stage-chisel7/src/main/scala/ChipyardAnnotations.scala:13-27). A part
# containing a "." is taken as a fully-qualified class name; otherwise the
# package is prefixed. Each part is instantiated by
# `Class.forName(name).newInstance` (StageUtils.scala:12), which is why
# WithSelectiveWaveform carries a no-arg constructor.
#
# So the default CONFIG below composes:
#   chipyard.harness.WithSelectiveWaveform   -- PC-triggered VCD windows (<=64)
#   chipyard.MediumBoomV3CosimConfig         -- BOOM v3 + WithCospike + WithTraceIO
#
# RANDOM=0 IS NOT OPTIONAL FOR COSIM. chipyard's default preprocessor defines
# include RANDOMIZE_REG_INIT / RANDOMIZE_MEM_INIT (sims/common-sim-flags.mk:32-42),
# so uninitialised state comes up random. Spike initialises to zero, so without
# this the DUT and the golden model disagree on reset state and the lockstep
# check reports divergences that are not bugs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/env.sh"

CONFIG="${1:-chipyard.harness.WithSelectiveWaveform_MediumBoomV3CosimConfig}"
TARGET="${2:-debug}"

export PATH="$WAVEQL_CONDA/bin:$PATH"
set +u                                  # conda activate.d scripts are not -u clean
# shellcheck disable=SC1091
source "$CHIPYARD_DIR/env.sh"
set -u

export JAVA_HEAP_SIZE="${JAVA_HEAP_SIZE:-16G}"

echo "[20_sim] config   : $CONFIG"
echo "[20_sim] target   : $TARGET"
echo "[20_sim] verilator: $(verilator --version)"
echo "[20_sim] firtool  : $(firtool --version | head -1)"
echo "[20_sim] RISCV    : $RISCV"
echo "[20_sim] jobs     : $WAVEQL_JOBS"
echo "[20_sim] started  : $(date -Is)"

cd "$CHIPYARD_DIR/sims/verilator"
time make "$TARGET" -j"$WAVEQL_JOBS" \
    CONFIG="$CONFIG" \
    EXTRA_SIM_PREPROC_DEFINES="+define+RANDOM=0"

BIN="$CHIPYARD_DIR/sims/verilator/simulator-chipyard.harness-${CONFIG}$([ "$TARGET" = debug ] && echo -debug)"
echo "[20_sim] finished : $(date -Is)"
ls -la "$BIN" && echo "[20_sim] OK: $BIN"
