#!/usr/bin/env bash
# Snapshot the UNMUTATED design's generated SystemVerilog for the causal walk.
#
# `why`, `drivers` and `source_of` walk firtool's output of the clean design; a
# worker's own generated-src changes with every mutant it builds, so the walk
# reads a frozen copy instead. Run once after scripts/build/20_sim.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${CONFIG:-WaveQLMediumBoomV3Config}"
GEN="$ROOT/thirdparty/chipyard/sims/verilator/generated-src/chipyard.harness.TestHarness.$CONFIG"
[ -d "$GEN/gen-collateral" ] || { echo "no generated sources at $GEN; build the simulator first" >&2; exit 1; }
DST="$ROOT/var/netlist-snapshot"
rm -rf "$DST"; mkdir -p "$DST"
cp -a "$GEN/gen-collateral" "$DST/"
cp "$GEN/model_module_hierarchy.json" "$DST/"
echo "netlist snapshot: $(ls "$DST/gen-collateral" | wc -l) files -> $DST"
