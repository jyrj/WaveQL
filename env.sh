# WaveQL environment — single source of truth. `source env.sh` before anything.
# Every path is absolute and derived; nothing is assumed to be on PATH.
WAVEQL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export WAVEQL_ROOT
export WAVEQL_TP="$WAVEQL_ROOT/thirdparty"
export WAVEQL_TOOLS="$WAVEQL_ROOT/tools"
export WAVEQL_VAR="$WAVEQL_ROOT/var"
export CHIPYARD_DIR="$WAVEQL_TP/chipyard"

# Build parallelism (default leaves a few threads free on a 24-thread machine).
export WAVEQL_JOBS="${WAVEQL_JOBS:-20}"

# Conda lives inside the repo prefix; nothing is installed system-wide.
export WAVEQL_CONDA="$WAVEQL_TOOLS/conda"
[ -d "$WAVEQL_CONDA" ] && export PATH="$WAVEQL_CONDA/bin:$PATH"

export PATH="$WAVEQL_TOOLS/bin:$WAVEQL_ROOT/.venv/bin:$HOME/.local/bin:$PATH"
export PYTHONPATH="$WAVEQL_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# Chipyard's own env (conda activate + RISCV toolchain + firtool). Only exists
# after scripts/build/chipyard.sh has completed step 1.
waveql_chipyard_env() {
    # shellcheck disable=SC1091
    source "$CHIPYARD_DIR/env.sh"
}
