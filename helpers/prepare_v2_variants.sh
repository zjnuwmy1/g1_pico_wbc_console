#!/bin/bash
# Prepare the v2 input-modality variant set so they can be A/B'd in ONE session.
# All share the v2 fix (wrist drive + dropped dead left_hand_q & left touch +
# image aug); they differ ONLY by input modality:
#     bottle_v2            rgb              (already trained — skipped)
#     bottle_rgbd_v2       rgb + depth
#     bottle_touch_v2      rgb + R-touch
#     bottle_rgbd_touch_v2 rgb + depth + R-touch
#
# SAFETY: the whole pipeline waits until the robot is IDLE (no g1_deploy_onnx_ref
# AND no act_infer.py) before doing ANY cpu/gpu work, and re-checks before each
# train. So it never adds jitter to a live deploy nor steals the GPU from a live
# act_infer test. Trains sequentially (one GPU). Hardened: pyav + save 10k +
# auto-resume. Shows up in the GUI as live trainings (logs/restore/).
set -u
ACT="$HOME/miniconda3/envs/act_train/bin/python"
cd "$HOME/Desktop/g1_pico_wbc_console" || exit 1
mkdir -p logs/restore
SESS="all"
EVAL_EP="recordings/20260618_135044/episode_0020"

# name images touch
VARIANTS="
bottle_rgbd_v2 rgbd 0
bottle_touch_v2 rgb 1
bottle_rgbd_touch_v2 rgbd 1
"

robot_busy() { pgrep -f "[g]1_deploy_onnx_ref" >/dev/null || pgrep -f "[a]ct_infer.py" >/dev/null; }
wait_idle() {
  if robot_busy; then echo "[$1] robot busy (deploy/act_infer) — holding until idle… $(date +%T)"; fi
  while robot_busy; do sleep 20; done
}
ndim() { "$ACT" -c "import json;i=json.load(open('$1/meta/info.json'));f=i['features'];print(i['total_episodes'],f['action']['shape'][0],f['observation.state']['shape'][0])" 2>/dev/null; }

echo "=== prepare v2 variants $(date +%T) ==="
wait_idle "init"

# ---- 1) build datasets ----
echo "$VARIANTS" | while read NAME IMG TCH; do
  [ -z "$NAME" ] && continue
  DS="$HOME/.cache/huggingface/lerobot/g1_pico_act/$NAME"
  read EP AD SD < <(ndim "$DS"); EP=${EP:-0}
  if [ "$EP" -ge 100 ] 2>/dev/null; then echo "[$NAME] dataset ok (${EP}ep action=$AD state=$SD)"; continue; fi
  [ -d "$DS" ] && rm -rf "$DS"
  echo "[$NAME] building ($IMG touch=$TCH)… $(date +%T)"
  "$ACT" helpers/build_act_dataset.py --repo-id "g1_pico_act/$NAME" \
    --sessions "$SESS" --images "$IMG" --touch "$TCH" >> "logs/restore/build__$NAME.log" 2>&1
  read EP AD SD < <(ndim "$DS")
  echo "[$NAME] built ${EP:-0}ep action=${AD:-?} state=${SD:-?}"
done
echo "=== datasets done $(date +%T) ==="

# ---- 2) train each (gated on robot-idle), then offline-eval ----
echo "$VARIANTS" | while read NAME IMG TCH; do
  [ -z "$NAME" ] && continue
  DS="$HOME/.cache/huggingface/lerobot/g1_pico_act/$NAME"
  OUT="training/g1_pico_act_$NAME"
  LOG="logs/restore/act_train__g1_pico_act_$NAME.log"
  read EP _ _ < <(ndim "$DS")
  [ "${EP:-0}" -ge 100 ] 2>/dev/null || { echo "[$NAME] dataset missing — skip"; continue; }
  LAST=$(ls "$OUT/checkpoints" 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1)
  [ "$LAST" = "100000" ] && { echo "[$NAME] already 100k — skip"; continue; }
  [ -z "$LAST" ] && [ -d "$OUT" ] && rm -rf "$OUT"   # drop partial w/o checkpoint

  for attempt in $(seq 1 8); do
    LAST=$(ls "$OUT/checkpoints" 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1)
    [ "$LAST" = "100000" ] && break
    wait_idle "$NAME"
    START=$(date +%s)
    if [ -d "$OUT/checkpoints/last" ]; then
      CFG="$OUT/checkpoints/last/pretrained_model/train_config.json"
      echo "[$NAME] attempt $attempt resume from ${LAST} $(date +%T)"
      "$ACT" -m lerobot.scripts.lerobot_train --config_path="$CFG" --resume=true \
        --num_workers=2 --save_freq=10000 --dataset.video_backend=pyav >> "$LOG" 2>&1
    else
      echo "[$NAME] attempt $attempt fresh ($IMG touch=$TCH, v2+aug) $(date +%T)"
      "$ACT" -m lerobot.scripts.lerobot_train \
        --dataset.repo_id="g1_pico_act/$NAME" --policy.type=act --policy.push_to_hub=false \
        --steps=100000 --output_dir="$OUT" \
        --num_workers=2 --save_freq=10000 --dataset.video_backend=pyav \
        --dataset.image_transforms.enable=true >> "$LOG" 2>&1
    fi
    rc=$?; DUR=$(( $(date +%s) - START ))
    LAST=$(ls "$OUT/checkpoints" 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1)
    [ "$LAST" = "100000" ] && { echo "[$NAME] ✅ 100k $(date +%T)"; break; }
    [ $rc -eq 0 ] && { echo "[$NAME] returned 0 (last=${LAST})"; break; }
    [ $DUR -lt 60 ] && { echo "[$NAME] ✗ failed in ${DUR}s rc=$rc — flag error"; tail -8 "$LOG"; break; }
    echo "[$NAME] crash rc=$rc after ${DUR}s — auto-resume…"; sleep 5
  done

  if [ -d "$OUT/checkpoints/last/pretrained_model" ]; then
    echo "[$NAME] offline-eval (best-effort)…"
    "$ACT" helpers/diag_offline_eval.py "$OUT/checkpoints/last/pretrained_model" \
      "$EVAL_EP" 80 >> "logs/restore/eval__$NAME.log" 2>&1 || echo "[$NAME] eval skipped"
  fi
  echo "[$NAME] DONE $(date +%T)"
done
echo "=== ALL v2 VARIANTS PREPARED $(date +%T) ==="
