#!/bin/bash
# GCE startup script: turn a fresh Ubuntu VM into a WaveQL worker host.
#
# The environment (conda toolchain, a built Chipyard checkout, the Python venv)
# is shipped as one archive and unpacked at the SAME absolute path it was built
# at, because conda environments and Chipyard's generated files are not
# relocatable. All work then runs inside one Fedora container, whose glibc
# matches the machine the toolchain was built on.
#
# Instance metadata (set with --metadata when creating the VM):
#   waveql-archive  gs:// URL of the environment archive (tar.zst, absolute paths)
#   waveql-root     absolute path of the repository inside the archive
#   waveql-user     user that owns the tree (created here if absent)
exec >> /var/log/waveql-init.log 2>&1
set -euxo pipefail
md() { curl -sf -H 'Metadata-Flavor: Google' \
  "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
ARCHIVE=$(md waveql-archive); ROOT=$(md waveql-root); RUNAS=$(md waveql-user)
HOMEDIR=$(getent passwd "$RUNAS" | cut -d: -f6 || true); HOMEDIR=${HOMEDIR:-/home/$RUNAS}

start_container() {
  podman rm -f wq >/dev/null 2>&1 || true
  podman run -d --init --name wq --network host \
    --user "$(id -u "$RUNAS"):$(id -g "$RUNAS")" -e HOME="$HOMEDIR" \
    --ulimit nofile=65536:65536 --pids-limit=-1 --shm-size=16g \
    -v "$HOMEDIR:$HOMEDIR" -w "$ROOT" wq-img sleep infinity
}
if [ -f /var/waveql-ready ]; then start_container; exit 0; fi

apt-get update -y && apt-get install -y podman zstd
id "$RUNAS" || useradd -m -s /bin/bash "$RUNAS"
gcloud storage cp "$ARCHIVE" /mnt/env.tar.zst
zstd -d -T0 -c /mnt/env.tar.zst | tar -P --same-owner -xf -
rm -f /mnt/env.tar.zst
chown -R "$RUNAS:$RUNAS" "$HOMEDIR"
podman build -q -t wq-img -f "$ROOT/scripts/cloud/Containerfile" "$ROOT/scripts/cloud"
start_container
podman exec wq .venv/bin/python -c "import duckdb, pywellen; print('environment ok')"
touch /var/waveql-ready
