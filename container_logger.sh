#!/bin/bash

LOGFILE=~/container_history.log
SEEN_FILE=~/.container_seen
ACTIVE_FILE=~/.container_active
START_TIMES=~/.container_start_times   # stores NAME:StartedAt for active containers
HIST_DIR=~/container_history_commands    # per-session command history snapshots

mkdir -p "$HIST_DIR"
touch $SEEN_FILE
touch $ACTIVE_FILE
touch $START_TIMES

snapshot_history() {
    local NAME="$1"
    local OUTFILE="$HIST_DIR/${NAME}.txt"
    # Try common history locations
    for HIST_PATH in /root/.bash_history /home/user/.bash_history /workspace/.bash_history; do
        HIST=$(docker exec "$NAME" cat "$HIST_PATH" 2>/dev/null)
        if [[ -n "$HIST" ]]; then
            echo "=== History snapshot: $(date -u) ===" >> "$OUTFILE"
            echo "$HIST" >> "$OUTFILE"
            echo "" >> "$OUTFILE"
            return
        fi
    done
    # Fallback: grab running processes
    PROCS=$(docker top "$NAME" -eo pid,pcpu,args --sort=-pcpu 2>/dev/null | head -10)
    if [[ -n "$PROCS" ]]; then
        echo "=== Process snapshot: $(date -u) ===" >> "$OUTFILE"
        echo "$PROCS" >> "$OUTFILE"
        echo "" >> "$OUTFILE"
    fi
}

SNAPSHOT_COUNTER=0

while true; do
    # Get currently running containers
    CURRENT=$(docker ps --format "{{.Names}}" | grep "^C\.")

    # Check for new containers
    for NAME in $CURRENT; do
        ACTUAL_START=$(docker inspect $NAME --format '{{.State.StartedAt}}' 2>/dev/null)
        LOGGED_START=$(grep "^$NAME:" $START_TIMES | tail -1 | cut -d: -f2-)
        NEW_SESSION=false
        if ! grep -q "^$NAME$" $ACTIVE_FILE; then
            # Not currently tracked as active -> new session (first time OR returning customer)
            sed -i "/^$NAME$/d" $SEEN_FILE
            sed -i "/^$NAME$/d" $ACTIVE_FILE
            sed -i "/^$NAME:/d" $START_TIMES
            NEW_SESSION=true
        fi
        if [ "$NEW_SESSION" = true ]; then
            echo "$NAME" >> $SEEN_FILE
            echo "$NAME" >> $ACTIVE_FILE

            IMAGE=$(docker inspect $NAME --format '{{.Config.Image}}' 2>/dev/null)
            STARTED=$(docker inspect $NAME --format '{{.State.StartedAt}}' 2>/dev/null)
            PORTS=$(docker inspect $NAME --format '{{range $p, $conf := .NetworkSettings.Ports}}{{$p}}->{{(index $conf 0).HostPort}} {{end}}' 2>/dev/null)
            RUNTYPE=$(sudo grep -a "$NAME" /var/lib/vastai_kaalia/kaalia.log | grep "runtype" | head -1 | grep -oP "runtype: \K[^ ]+")

            if [[ "$RUNTYPE" == "args" ]]; then
                TYPE="SERVERLESS"
            elif [[ "$RUNTYPE" == *"jupyter"* ]]; then
                TYPE="JUPYTER (interactive)"
            elif [[ "$RUNTYPE" == *"ssh"* ]]; then
                TYPE="SSH (interactive)"
            else
                TYPE="UNKNOWN"
            fi

            # Store start time for duration calculation later
            echo "$NAME:$STARTED" >> $START_TIMES

            echo "===================================" >> $LOGFILE
            echo "Container: $NAME" >> $LOGFILE
            echo "Started:   $STARTED" >> $LOGFILE
            echo "Image:     $IMAGE" >> $LOGFILE
            echo "Ports:     $PORTS" >> $LOGFILE
            echo "Runtype:   $RUNTYPE" >> $LOGFILE
            echo "Type:      $TYPE" >> $LOGFILE
            echo "Logged:    $(date)" >> $LOGFILE

            # Initialize command history file for this session
            OUTFILE="$HIST_DIR/${NAME}.txt"
            echo "Container: $NAME" > "$OUTFILE"
            echo "Started:   $STARTED" >> "$OUTFILE"
            echo "Image:     $IMAGE" >> "$OUTFILE"
            echo "Type:      $TYPE" >> "$OUTFILE"
            echo "" >> "$OUTFILE"

            curl -s -o /dev/null -X POST \
                -H "Title: New Rental: $NAME" \
                -H "Tags: moneybag" \
                -H "Priority: 3" \
                -d "Image: $IMAGE | Type: $TYPE" \
                https://ntfy.sh/vastai-notifs &
        fi
    done

    # Snapshot history for all active containers every 30s (3 x 10s loops)
    SNAPSHOT_COUNTER=$((SNAPSHOT_COUNTER + 1))
    if [[ $SNAPSHOT_COUNTER -ge 3 ]]; then
        for NAME in $CURRENT; do
            snapshot_history "$NAME"
        done
        SNAPSHOT_COUNTER=0
    fi

    # Check for containers that have stopped
    while IFS= read -r NAME; do
        if ! echo "$CURRENT" | grep -q "^$NAME$"; then
            # Container stopped — do a final history snapshot before inspect
            snapshot_history "$NAME"

            # ── IMAGE CAPTURE (all containers, respects toggle) ──
            TOGGLE_FILE="$HIST_DIR/.image_capture_enabled"
            if [[ -f "$TOGGLE_FILE" ]]; then
                SAMPLE_IMG=$(docker exec "$NAME" find                     /ComfyUI/output                     /workspace/ComfyUI/output                     /workspace/stable-diffusion-webui/outputs                     /workspace/SD.Next/outputs                     /workspace/invokeai/outputs                     /workspace/Fooocus/outputs                     /workspace/SwarmUI/Output                     /workspace/outputs                     /app/outputs                     /root/outputs                     /tmp/outputs                     -maxdepth 4 -name "*.png" -not -name "*.preview.png"                     2>/dev/null | sort | tail -1)
                if [[ -n "$SAMPLE_IMG" ]]; then
                    mkdir -p "$HIST_DIR/images"
                    DEST="$HIST_DIR/images/${NAME}_sample.png"
                    docker cp "$NAME:$SAMPLE_IMG" "$DEST" 2>/dev/null &&                         echo "Sample image saved: $DEST" >> "$HIST_DIR/${NAME}.txt" ||                         echo "Image copy failed" >> "$HIST_DIR/${NAME}.txt"
                fi
            fi
            # ── END IMAGE CAPTURE ──

            # Try docker inspect first (may still exist briefly)
            FINISHED=$(docker inspect $NAME --format '{{.State.FinishedAt}}' 2>/dev/null)
            EXIT_CODE=$(docker inspect $NAME --format '{{.State.ExitCode}}' 2>/dev/null)
            STARTED=$(docker inspect $NAME --format '{{.State.StartedAt}}' 2>/dev/null)

            # If docker inspect failed (container already removed), use stored start time
            # and current time as finish time
            if [[ -z "$FINISHED" || "$FINISHED" == "0001-01-01T00:00:00Z" ]]; then
                FINISHED=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
                EXIT_CODE="—"
            fi

            # Get StartedAt from stored file if inspect failed
            if [[ -z "$STARTED" ]]; then
                STARTED=$(grep "^$NAME:" $START_TIMES | tail -1 | cut -d: -f2-)
            fi

            # Calculate duration
            START_TS=$(date -d "$STARTED" +%s 2>/dev/null)
            END_TS=$(date -d "$FINISHED" +%s 2>/dev/null)
            if [[ -n "$START_TS" && -n "$END_TS" && "$END_TS" -gt "$START_TS" ]]; then
                DURATION=$(( END_TS - START_TS ))
                HOURS=$(( DURATION / 3600 ))
                MINS=$(( (DURATION % 3600) / 60 ))
                SECS=$(( DURATION % 60 ))
                DUR_STR="${HOURS}h ${MINS}m ${SECS}s"
            else
                DUR_STR="unknown"
            fi

            # Append completion block to log
            echo "" >> $LOGFILE
            echo "--- $NAME COMPLETED ---" >> $LOGFILE
            echo "Finished:  $FINISHED" >> $LOGFILE
            echo "Duration:  $DUR_STR" >> $LOGFILE
            echo "ExitCode:  $EXIT_CODE" >> $LOGFILE
            echo "" >> $LOGFILE

            # Append completion info to command history file
            OUTFILE="$HIST_DIR/${NAME}.txt"
            echo "" >> "$OUTFILE"
            echo "=== Session ended: $(date -u) ===" >> "$OUTFILE"
            echo "Finished:  $FINISHED" >> "$OUTFILE"
            echo "Duration:  $DUR_STR" >> "$OUTFILE"
            echo "ExitCode:  $EXIT_CODE" >> "$OUTFILE"

	    # Flat storage — no move needed, history.json written directly to HIST_DIR

            # Clean up tracking files
            sed -i "/^$NAME$/d" $ACTIVE_FILE
            sed -i "/^$NAME:/d" $START_TIMES

            curl -s -o /dev/null -X POST \
                -H "Title: Rental Ended: $NAME" \
                -H "Tags: white_check_mark" \
                -H "Priority: 2" \
                -d "Duration: $DUR_STR | ExitCode: $EXIT_CODE" \
                https://ntfy.sh/vastai-notifs &
        fi
    done < $ACTIVE_FILE

    sleep 10
done
