#!/usr/bin/env bash
# Install Miniforge into tools/conda. Nothing is installed system-wide and
# nothing outside this repository is written. Idempotent: re-running with an
# existing prefix is a no-op.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/configs/pins.env"
PREFIX="$ROOT/tools/conda"

if [ -x "$PREFIX/bin/conda" ]; then
    echo "[10_conda] conda already at $PREFIX ($("$PREFIX/bin/conda" --version))"
    exit 0
fi

mkdir -p "$ROOT/var/log" "$ROOT/tools"
INSTALLER="$ROOT/var/Miniforge3-${MINIFORGE_VERSION}-Linux-x86_64.sh"
echo "[10_conda] downloading $MINIFORGE_URL"
curl -fsSL "$MINIFORGE_URL" -o "$INSTALLER"

# Verify against the checksum the release publishes, so the toolchain this
# project builds on is attributable to a byte sequence, not just a URL.
echo "[10_conda] verifying sha256"
curl -fsSL "${MINIFORGE_URL}.sha256" -o "${INSTALLER}.sha256"
( cd "$(dirname "$INSTALLER")" && sha256sum -c "$(basename "$INSTALLER").sha256" )

echo "[10_conda] installing to $PREFIX (batch, no shell init)"
bash "$INSTALLER" -b -p "$PREFIX"
"$PREFIX/bin/conda" --version
echo "[10_conda] done"
