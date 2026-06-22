#!/usr/bin/env python3
"""Phase-0 offline diagnostic: run a trained ACT checkpoint on (training) frames
and compare predictions to the demo actions — to bisect "model didn't learn"
vs "model learned but deploy/inference is the problem".

Signals:
  MAE(pred vs demo)  low  -> model fits this data
  pred_std (temporal) << gt_std  -> COLLAPSE (outputs near-constant = mean-collapse)

Run with the act_train env python.
  diag_offline_eval.py <checkpoint_pretrained_model_dir> <episode_dir> [n_frames]
"""
import json
import os
import sys

import cv2
import numpy as np
import torch
from lerobot.policies.act.modeling_act import ACTPolicy
from lerobot.policies.factory import make_pre_post_processors

CK = sys.argv[1]
EP = sys.argv[2]
N = int(sys.argv[3]) if len(sys.argv) > 3 else 120


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_act_dataset as B   # current (v2) layout — reused so eval matches training


def _flat(v):
    return np.asarray(v, np.float32).reshape(-1)


def _latest(buf, per):
    x = _flat(buf)
    return x[-per:] if x.size >= per else np.pad(x, (0, per - x.size))


def _state_v1(st):
    jq = _flat(st.get("joint_q") or [])[:29]
    lh = _flat(st.get("left_hand_q") or [])[:6]
    rh = _flat(st.get("right_hand_q") or [])[:6]
    return np.concatenate([np.pad(jq, (0, 29 - len(jq))), np.pad(lh, (0, 6 - len(lh))),
                           np.pad(rh, (0, 6 - len(rh)))]).astype(np.float32)


def _action_v1(a):
    g = lambda k, n: (_flat(a[k])[:n] if a.get(k) is not None else np.zeros(n, np.float32))
    return np.concatenate([
        g("vr_position", 9), g("vr_orientation", 12),
        _latest(a.get("smpl_joints") or [0.0] * 72, 72),
        _latest(a.get("body_quat_w") or [1, 0, 0, 0], 4),
        np.array([float(a.get("heading_increment") or 0.0)], np.float32),
        np.array([float(a.get("left_grip") or 0.0), float(a.get("right_grip") or 0.0),
                  float(a.get("left_trigger") or 0.0), float(a.get("right_trigger") or 0.0)], np.float32),
    ]).astype(np.float32)


dev = "cuda" if torch.cuda.is_available() else "cpu"
policy = ACTPolicy.from_pretrained(CK); policy.to(dev); policy.eval()
pre, post = make_pre_post_processors(policy_cfg=policy.config, pretrained_path=CK,
                                     preprocessor_overrides={"device_processor": {"device": dev}})

# layout-aware: v2 (action<=90) uses build_act_dataset's vectors + v2 components
ACTION_DIM = int(policy.config.output_features["action"].shape[0])
V2 = ACTION_DIM <= 90
state_vec = (lambda st: B._state_vec(st, None)) if V2 else _state_v1
action_vec = (lambda a: B._action_vec(a)) if V2 else _action_v1
COMPS = ({"smpl_joints": (0, 72), "wrist_jp": (72, 78), "body_quat": (78, 82), "grips": (82, 86)} if V2
         else {"vr_position": (0, 9), "vr_orientation": (9, 21), "smpl_joints": (21, 93),
               "body_quat": (93, 97), "heading": (97, 98), "grips": (98, 102)})

data = json.load(open(os.path.join(EP, "data.json")))["data"]
try:
    cur = json.load(open(os.path.join(EP, "curation.json")))
    s = cur.get("start") or 0
    e = cur.get("end") if cur.get("end") is not None else len(data) - 1
except Exception:
    s, e = 0, len(data) - 1
frames = [f for f in data[s:e + 1]
          if f.get("state") and f.get("action") and (f.get("frames") or {}).get("head")]
frames = frames[:N]

policy.reset()
preds, gts = [], []
for f in frames:
    img = cv2.imread(os.path.join(EP, f["frames"]["head"]))
    if img is None:
        continue
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    if img.shape[:2] != (480, 640):
        img = cv2.resize(img, (640, 480))
    obs = {"observation.state": torch.from_numpy(state_vec(f["state"])),
           "observation.images.cam_high": torch.from_numpy(img.astype(np.float32) / 255.0).permute(2, 0, 1)}
    with torch.inference_mode():
        a = post(policy.select_action(pre(obs)))
    preds.append(a.cpu().numpy().ravel())
    gts.append(action_vec(f["action"]))

P, G = np.array(preds), np.array(gts)
print(f"{len(P)} frames  |  {os.path.basename(os.path.dirname(EP))}/{os.path.basename(EP)}")
print(f"layout={'v2' if V2 else 'v1'} action_dim={ACTION_DIM}")
print(f"{'component':16s} {'MAE(pred-demo)':>15s} {'pred_std':>10s} {'gt_std':>10s}   verdict")
comps = COMPS
for n, (i, j) in comps.items():
    mae = float(np.abs(P[:, i:j] - G[:, i:j]).mean())
    pstd = float(P[:, i:j].std(0).mean())
    gstd = float(G[:, i:j].std(0).mean())
    v = "<<COLLAPSE" if (gstd > 1e-4 and pstd < 0.3 * gstd) else ("fits" if mae < 0.15 * (gstd + 1e-6) + 0.02 else "")
    print(f"{n:16s} {mae:15.4f} {pstd:10.4f} {gstd:10.4f}   {v}")
# correlation: does prediction TRACK the demo over time (not just match mean)?
print("\nper-component temporal correlation (pred vs demo; 1=tracks, ~0=ignores input):")
for n, (i, j) in comps.items():
    cs = []
    for d in range(i, j):
        if G[:, d].std() > 1e-4 and P[:, d].std() > 1e-6:
            cs.append(np.corrcoef(P[:, d], G[:, d])[0, 1])
    print(f"  {n:16s} mean_corr={np.nanmean(cs):.3f}" if cs else f"  {n:16s} (flat)")
