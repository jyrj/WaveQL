#!/usr/bin/env bash
# Patch chipyard's build-setup.sh so its conda step survives a modern host.
#
# WHY (on a Fedora 44 host):
#   build-setup.sh step 1 compares the host glibc against the sysroot pinned in
#   conda-reqs/chipyard-base.yaml and, on a mismatch, rewrites the pin to the
#   host's version and regenerates every conda lockfile. On Fedora 44 (glibc
#   2.43) conda-forge publishes no sysroot_linux-64=2.43, so the
#   regeneration cannot solve and the build dies ~2 minutes in.
#
#   Two separate defects are involved and both are fixed here:
#     1. DEFAULT_GLIBC is parsed with `awk -F= '{print $2}'`, which keeps the
#        trailing YAML comment -- so the value is
#        "2.34 # need to be close to system glibc for VCS compatibility",
#        never equal to any host glibc. The rewrite therefore fires on EVERY
#        host, including a genuine 2.34 one.
#     2. Even parsed correctly, rewriting the pin upward is wrong for us: the
#        conda sysroot only sets the *minimum* glibc the conda toolchain targets.
#        Binaries built against sysroot 2.34 run fine on 2.43 (glibc is
#        backward-compatible); the reverse is not true. Keeping the pin is both
#        the working and the more portable choice.
#
#   CHIA's own ChipyardDockerfile carries the same fix for its Ubuntu 22.04 base
#   (glibc 2.35 -> 2.34); this is the same change generalized to any host.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CY="$ROOT/thirdparty/chipyard"
TARGET="$CY/scripts/build-setup.sh"
[ -f "$TARGET" ] || { echo "[00_patch] $TARGET missing -- clone chipyard first" >&2; exit 1; }

python3 - "$TARGET" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); text = p.read_text()
old = """    SYS_GLIBC=$(ldd --version | awk '/ldd/{print $NF}')
    DEFAULT_GLIBC=$(grep -i "sysroot_linux-64=" conda-reqs/chipyard-base.yaml | awk -F= '{print $2}')
"""
new = """    SYS_GLIBC=$(ldd --version | awk '/ldd/{print $NF}')
    # WaveQL patch: strip the trailing YAML comment (upstream keeps it, so the
    # comparison below never matches on any host) and keep the pinned sysroot
    # rather than rewriting it to the host's glibc. conda-forge publishes no
    # sysroot for a modern host glibc, and a lower sysroot is the portable
    # target anyway.
    DEFAULT_GLIBC=$(grep -i "sysroot_linux-64=" conda-reqs/chipyard-base.yaml | awk -F= '{print $2}' | awk '{print $1}')
    echo "[waveql] host glibc=$SYS_GLIBC pinned sysroot=$DEFAULT_GLIBC (keeping the pin)"
    SYS_GLIBC="$DEFAULT_GLIBC"
"""
if "WaveQL patch: strip the trailing YAML comment" in text:
    print("[00_patch] already patched; nothing to do"); sys.exit(0)
if old not in text:
    print("[00_patch] FAILED: the glibc block does not match the pinned source.", file=sys.stderr)
    print("[00_patch] chipyard moved; re-derive this patch before building.", file=sys.stderr)
    sys.exit(2)
p.write_text(text.replace(old, new, 1))
print("[00_patch] patched the glibc/sysroot block in build-setup.sh")
PY

mkdir -p "$ROOT/patches"
git -C "$CY" diff -- scripts/build-setup.sh > "$ROOT/patches/0001-chipyard-pin-conda-sysroot.patch"
echo "[00_patch] recorded $(wc -l < "$ROOT/patches/0001-chipyard-pin-conda-sysroot.patch") lines to patches/0001-chipyard-pin-conda-sysroot.patch"
