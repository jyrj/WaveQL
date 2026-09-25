#!/bin/bash
# Wait until nothing matches WAIT_REGEX (a running screen or verifier), then
# verify the queues in TAGS on workers PREFIX1..N, claiming cells through GCS so
# verifiers on every host share one queue and no cell is verified twice.
#   verify_after.sh WAIT_REGEX PREFIX N TAG [TAG ...]
# NOSYNC=1 on the host whose agents write the queue (it must not mirror the queue
# down onto itself). Needs WAVEQL_SYNC (gs://bucket/prefix) for the mirror and
# WAVEQL_CLAIMS (gs://bucket/prefix) for claims.
cd "${WAVEQL_ROOT:?}"
WAIT="$1"; PREFIX="$2"; N="$3"; shift 3; TAGS=("$@")
while ps -eo args= | awk -v re="$WAIT" '$1 == ".venv/bin/python" && $0 ~ re' | grep -q .; do sleep 30; done
echo "$(date -u +%H:%M) '$WAIT' finished; verifying ${TAGS[*]} on ${PREFIX}1..$N"
B=generators/boom/src/main; REF=${REF:-var/workers/w0}
for i in $(seq 1 "$N"); do                       # a worker is trusted only after a source check
  w=var/workers/$PREFIX$i
  diff -rq "$REF/$B" "$w/$B" >/dev/null || { echo "worker $w differs from $REF; refusing to verify"; exit 1; }
done
if [ -z "${NOSYNC:-}" ]; then
  UP=(); DOWN=()
  for t in "${TAGS[@]}"; do
    UP+=("measurements/episodes-$t-V-$PREFIX*.jsonl")
    DOWN+=("measurements/proposals-$t.jsonl" "measurements/agents-$t.done" "corpus/$t-*.jsonl")
  done
  setsid nohup .venv/bin/python -u scripts/run/gcs_sync.py --prefix "${WAVEQL_SYNC:?}" --every 45 \
    --up "${UP[@]}" --down "${DOWN[@]}" > var/log/gcs_sync_verify.log 2>&1 < /dev/null &
  sleep 20
fi
for i in $(seq 1 "$N"); do
  t=${TAGS[$(( (i-1) % ${#TAGS[@]} ))]}
  setsid nohup .venv/bin/python -u scripts/run/scale.py verify --worker "var/workers/$PREFIX$i" \
    --jobs 6 --tag "$t" --gcs-claims "${WAVEQL_CLAIMS:?}/$t" \
    > "var/log/verify-$t-$PREFIX$i.log" 2>&1 < /dev/null &
done
