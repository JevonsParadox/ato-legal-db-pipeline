#!/bin/bash
# =====================================================================
# Run Stage 3 (parser) once Stage 2 (fetcher) has drained the queue.
#
# Intended use: launch alongside the Stage 2 loop. This script polls
# fetch_queue every few minutes. When pending == 0, it fires the parser
# against the whole corpus. The parser is itself a long-running job
# (~6-8 hours estimated against the ~15k-doc corpus) so this is
# overnight-friendly.
#
# Notifications: macOS osascript on completion / failure / start, plus
# a greppable status file at /tmp/ato_stage3_status.
#
# Stop cleanly: `touch /tmp/STOP_STAGE3` before Stage 3 has begun. Once
# the parser is running it doesn't watch the stop file (we'd have to
# kill it explicitly).
# =====================================================================

set -u
cd "$(dirname "$0")/.."

set -a
source .env.local
set +a

POLL_INTERVAL=${POLL_INTERVAL:-300}   # seconds between Stage 2 checks
LOG=/tmp/ato_stage3.log
STATUS=/tmp/ato_stage3_status
STOP=/tmp/STOP_STAGE3

notify() {
    local msg="$1"
    local sound="${2:-Glass}"
    osascript -e "display notification \"${msg//\"/\\\"}\" with title \"ATO Stage 3 parser\" sound name \"${sound}\"" 2>/dev/null || true
}

count_pending() {
    curl -sS \
        -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
        -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" \
        "$SUPABASE_URL/rest/v1/fetch_queue?select=count&status=eq.pending" \
      | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['count'])"
}

rm -f "$STOP"
echo "$(date) Stage 3 driver starting. Will poll fetch_queue every ${POLL_INTERVAL}s." | tee -a "$LOG"
{ echo "Updated: $(date)"; echo "Last event: waiting for Stage 2 to drain"; } > "$STATUS"
notify "Stage 3 driver armed. Will fire parser when Stage 2 drains." Submarine

# ---- Wait for Stage 2 to drain ----
while true; do
    if [ -f "$STOP" ]; then
        echo "$(date) STOP file found before Stage 2 drained — exiting." | tee -a "$LOG"
        { echo "Updated: $(date)"; echo "Last event: stopped before Stage 3 began"; } > "$STATUS"
        notify "Stage 3 stopped before Stage 2 drained." Pop
        exit 0
    fi
    PENDING=$(count_pending)
    echo "$(date) Stage 2 pending = $PENDING" | tee -a "$LOG"
    { echo "Updated: $(date)"; echo "Last event: waiting (Stage 2 pending = $PENDING)"; } > "$STATUS"
    if [ "$PENDING" = "0" ]; then
        echo "$(date) Stage 2 drained — firing parser." | tee -a "$LOG"
        break
    fi
    sleep "$POLL_INTERVAL"
done

# ---- Run Stage 3 ----
notify "Stage 3 parser starting. ~6-8 hours expected." Glass
{ echo "Updated: $(date)"; echo "Last event: parser running"; } > "$STATUS"

START=$(date +%s)
.venv/bin/python -u parsers/03_parse_document.py >> "$LOG" 2>&1
RC=$?
END=$(date +%s)
ELAPSED=$(( END - START ))

if [ "$RC" -eq 0 ]; then
    echo "$(date) Stage 3 complete (rc=0, ${ELAPSED}s)." | tee -a "$LOG"
    { echo "Updated: $(date)"; echo "Last event: parser complete (${ELAPSED}s)"; } > "$STATUS"
    notify "Stage 3 COMPLETE — parser finished in $(( ELAPSED / 60 )) min. Pipeline done." Hero
else
    echo "$(date) Stage 3 exited with code $RC after ${ELAPSED}s." | tee -a "$LOG"
    { echo "Updated: $(date)"; echo "Last event: parser exited $RC (${ELAPSED}s)"; } > "$STATUS"
    notify "Stage 3 FAILED with exit code $RC. Check /tmp/ato_stage3.log." Basso
fi

exit "$RC"
