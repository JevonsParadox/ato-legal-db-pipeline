#!/bin/bash
# =====================================================================
# Stage 2 looped fetcher driver.
#
# Runs scrapers/02_fetch_ato.py in batches of BATCH_SIZE until the
# fetch_queue is drained, MAX_BATCHES iterations elapse, or a hard
# error occurs. Surfaces failures via macOS notifications (osascript)
# AND writes a greppable status file at /tmp/ato_stage2_status.
#
# Usage:
#   ./scripts/run_stage2_loop.sh
#
# Stop cleanly any time:
#   touch /tmp/STOP_STAGE2
# The loop checks for the file between batches and exits gracefully
# (no in-flight row left stranded because each Python invocation
# completes its current batch first).
#
# Crash recovery: per-row commits + reset_stuck_in_progress on each
# Python startup means restarting after any failure is safe.
# =====================================================================

set -u

cd "$(dirname "$0")/.."

# Env from .env.local — same pattern as direct fetcher invocation.
set -a
source .env.local
set +a

MAX_BATCHES=${MAX_BATCHES:-50}
BATCH_SIZE=${BATCH_SIZE:-500}
SLEEP_BETWEEN=${SLEEP_BETWEEN:-60}
MIN_PROGRESS_PER_BATCH=${MIN_PROGRESS_PER_BATCH:-5}

LOG=/tmp/ato_stage2_loop.log
STATUS=/tmp/ato_stage2_status
STOP=/tmp/STOP_STAGE2

# ---- helpers ----
notify() {
    # macOS desktop notification + sound. Falls through silently if osascript
    # isn't available (e.g., running headless).
    local title="ATO Stage 2 fetcher"
    local msg="$1"
    local sound="${2:-Glass}"
    osascript -e "display notification \"${msg//\"/\\\"}\" with title \"${title}\" sound name \"${sound}\"" 2>/dev/null || true
}

count_status() {
    # $1 = one of pending|done|failed|in_progress
    curl -sS \
        -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
        -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
        "$SUPABASE_URL/rest/v1/fetch_queue?select=count&status=eq.$1" \
      | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['count'])"
}

write_status() {
    {
        echo "Updated: $(date)"
        echo "Last event: $1"
        echo "Pending:     $(count_status pending)"
        echo "Done:        $(count_status done)"
        echo "Failed:      $(count_status failed)"
        echo "In_progress: $(count_status in_progress)"
    } > "$STATUS"
}

# ---- pre-flight ----
rm -f "$STOP"
echo "=== Stage 2 loop starting at $(date) ===" | tee -a "$LOG"
echo "  MAX_BATCHES=$MAX_BATCHES  BATCH_SIZE=$BATCH_SIZE  SLEEP_BETWEEN=$SLEEP_BETWEEN" | tee -a "$LOG"
notify "Loop started — $(count_status pending) docs pending. Will not disturb you again unless something goes wrong or it finishes." Submarine
write_status "loop started"

# ---- main loop ----
for i in $(seq 1 "$MAX_BATCHES"); do
    if [ -f "$STOP" ]; then
        echo "$(date) STOP file found — exiting gracefully." | tee -a "$LOG"
        notify "Loop stopped via /tmp/STOP_STAGE2." Pop
        write_status "stopped by STOP file"
        exit 0
    fi

    before=$(count_status pending)
    echo "$(date) Batch $i/$MAX_BATCHES — pending=$before" | tee -a "$LOG"

    # Background status updater: refresh /tmp/ato_stage2_status every 60s
    # while the batch runs, so external pollers see live progress rather
    # than a snapshot from "loop started".
    (
        while sleep 60; do
            write_status "batch $i in progress"
        done
    ) &
    updater_pid=$!

    .venv/bin/python -u scrapers/02_fetch_ato.py --batch-size "$BATCH_SIZE" >> "$LOG" 2>&1
    rc=$?

    kill "$updater_pid" 2>/dev/null
    wait "$updater_pid" 2>/dev/null

    if [ "$rc" -ne 0 ]; then
        echo "$(date) Batch $i exited with code $rc — pausing loop." | tee -a "$LOG"
        notify "Batch $i exited with code $rc. Loop paused. Check /tmp/ato_stage2_loop.log." Basso
        write_status "batch $i exit code $rc — paused"
        exit "$rc"
    fi

    after=$(count_status pending)
    processed=$(( before - after ))
    echo "$(date) Batch $i done — processed $processed, pending now $after" | tee -a "$LOG"
    write_status "batch $i ok — processed $processed"

    if [ "$after" -eq 0 ]; then
        echo "$(date) Queue drained — stopping." | tee -a "$LOG"
        notify "Stage 2 COMPLETE — all docs fetched. Ready for Stage 3 (parser)." Hero
        write_status "complete"
        exit 0
    fi

    if [ "$processed" -lt "$MIN_PROGRESS_PER_BATCH" ]; then
        echo "$(date) Batch $i made only $processed of expected ~$BATCH_SIZE — investigate." | tee -a "$LOG"
        notify "Batch $i only processed $processed docs (expected ~$BATCH_SIZE). Possibly rate-limited. Loop paused." Basso
        write_status "batch $i underprocessed — paused"
        exit 1
    fi

    sleep "$SLEEP_BETWEEN"
done

echo "$(date) Reached MAX_BATCHES=$MAX_BATCHES — stopping loop." | tee -a "$LOG"
notify "Reached $MAX_BATCHES-batch limit. Check counts and rerun the loop." Tink
write_status "max batches reached"
