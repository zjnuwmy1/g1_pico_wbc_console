#!/usr/bin/env python3
"""Postmortem of a real-robot ACT inference run (act_infer with INFER_FRAMESAVE=1).
Confirms on the robot what the offline eval predicted:
  - did the action VARY or collapse/freeze at deploy?
  - (v2) did the WRIST actually move = grasp orienting?
  - is the deploy STATE in-distribution vs the training dataset?
  diag_framesave.py [frame_dir=/tmp/infer_frames] [train_stats.json]
"""
import glob
import json
import os
import sys

import numpy as np

D = sys.argv[1] if len(sys.argv) > 1 else "/tmp/infer_frames"
acts = sorted(glob.glob(f"{D}/*_action.npy"))
sts = sorted(glob.glob(f"{D}/*_state.npy"))
if not acts:
    print("no frames in", D)
    sys.exit(1)
A = np.array([np.load(f) for f in acts])
S = np.array([np.load(f) for f in sts]) if sts else None
ad = A.shape[1]
V2 = ad <= 90
print(f"{len(A)} frames | action_dim={ad} layout={'v2' if V2 else 'v1'}")

comps = ({"smpl_joints": (0, 72), "wrist_jp": (72, 78), "body_quat": (78, 82), "grips": (82, 86)} if V2
         else {"vr_position": (0, 9), "vr_orientation": (9, 21), "smpl_joints": (21, 93),
               "body_quat": (93, 97), "heading": (97, 98), "grips": (98, 102)})
print("action component temporal std (low = collapsed / frozen at deploy):")
for n, (i, j) in comps.items():
    seg = A[:, i:j]
    print(f"  {n:14s} std={seg.std(0).mean():.4f}  range=[{seg.min():.2f},{seg.max():.2f}]")

if V2:
    w = A[:, 72:78]
    rstd = w[:, 3:6].std(0)
    print(f"\nWRIST drive check — right wrist std = {np.round(rstd, 3)}  "
          f"({'✓ orienting' if rstd.max() > 0.05 else '✗ static — wrist still not driven'})")

if S is not None and len(sys.argv) > 2 and os.path.isfile(sys.argv[2]):
    stt = json.load(open(sys.argv[2]))["observation.state"]
    mean, std = np.array(stt["mean"]), np.array(stt["std"])
    z = np.abs((S.mean(0) - mean) / (std + 1e-6))
    print(f"\nstate OOD vs training: max |z| = {z.max():.1f} at dim {int(z.argmax())}  (>3 = OOD)")
    print("  worst dims:", [(int(d), round(float(z[d]), 1)) for d in np.argsort(-z)[:5]])
