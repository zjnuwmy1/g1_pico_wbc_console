#!/bin/bash
# Recover the 3 crashed ACT trainings: resume each from its last checkpoint with
# the torchcodec SIGILL worked around (pyav decoder + fewer workers) and frequent
# checkpoints, auto-retrying from the newest checkpoint on any residual crash.
# Runs lerobot directly (independent of the console), sequentially.
ACT="$HOME/miniconda3/envs/act_train/bin/python"
cd "$HOME/Desktop/g1_pico_wbc_console" || exit 1
LOGDIR=logs/recovery
mkdir -p "$LOGDIR"

for r in bottle_rgbd_v1 bottle_touch_v1 bottle_rgbd_touch_v1; do
  OUT="training/g1_pico_act_$r"
  LOG="$LOGDIR/act_train__g1_pico_act_$r.log"
  echo "========== $r =========="
  for attempt in $(seq 1 8); do
    LAST=$(ls "$OUT/checkpoints" 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1)
    if [ "$LAST" = "100000" ]; then echo "[$r] ✅ already at 100k"; break; fi
    CFG="$OUT/checkpoints/last/pretrained_model/train_config.json"
    if [ ! -f "$CFG" ]; then echo "[$r] ⚠ no checkpoint to resume"; break; fi
    echo "[$r] attempt $attempt: resume from step ${LAST:-?} (pyav/workers=2/save_freq=5000) $(date +%T)"
    START=$(date +%s)
    "$ACT" -m lerobot.scripts.lerobot_train \
      --config_path="$CFG" --resume=true \
      --num_workers=2 --save_freq=5000 --dataset.video_backend=pyav >> "$LOG" 2>&1
    rc=$?; DUR=$(( $(date +%s) - START ))
    if [ $rc -eq 0 ]; then echo "[$r] ✅ finished (rc=0)"; break; fi
    if [ $DUR -lt 60 ]; then
      echo "[$r] ✗ failed in ${DUR}s rc=$rc — likely a cmd/config error, see $LOG; skipping"; break
    fi
    echo "[$r] crashed rc=$rc after ${DUR}s — auto-resume from newest checkpoint…"; sleep 5
  done
done
echo "=== RECOVERY COMPLETE $(date +%T) ==="
