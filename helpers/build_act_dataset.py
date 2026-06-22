#!/usr/bin/env python3
"""Build a LeRobot dataset for ACT training from CURATED console recordings.

Option-A: the policy predicts the operator's PICO teleop COMMAND, which at
inference is fed to the SONIC deploy (like playback) — so the ACTION here is the
full PICO command (incl. waist via SMPL), and the OBSERVATION is the head camera
(RGB or RGB+depth) + robot proprioception (+ optional tactile).

Reads only KEPT episodes (curation.json keep==true) and TRIMS each to its useful
[start,end] frame range, across one or more sessions.

  observation.state : joint_q[29] + right_hand_q[6]  (+ tactile if --touch)  = 35
                      (drops dead left_hand_q — right-handed teleop)
  observation.images.cam_high       : head RGB  (480x640x3)
  observation.images.cam_high_depth : head depth as uint8 grayscale (if --images rgbd)
  action (v2)       : smpl_joints_latest[72] + wrist_joint_pos[6] +
                      body_quat_w_latest[4] + grips[4]              = 86
                      (only what SONIC encoder mode 2 tracks; +wrist for grasp)

Run with the act_train env python (has lerobot + torch+cu128).
Usage:
  build_act_dataset.py --repo-id g1_pico_act/bottle_v1 \
      --sessions 20260618_135044,20260618_151230 --images rgb --touch 0
  (--sessions all  uses every session under recordings/)
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REC_DIR = os.path.join(HERE, "..", "recordings")

# action layout v2 (only what the deploy's SMPL encoder mode 2 actually tracks)
SMPL_PER_FRAME = 72   # 24 SMPL joints x 3 (root-relative POSITIONS -> reach)
BODYQ = 4
# G1 29-dof mujoco order: L/R wrist roll/pitch/yaw. mode 2 reads joint_pos at
# THESE indices for grasp orientation (which smpl_joints positions don't encode).
WRIST_IDX = [19, 20, 21, 26, 27, 28]


def _flat(v):
    return np.asarray(v, dtype=np.float32).reshape(-1)


def _latest(buf, per):
    """Last `per` values of a flattened N-frame buffer (newest frame)."""
    a = _flat(buf)
    return a[-per:] if a.size >= per else np.pad(a, (0, per - a.size))


def _action_vec(a):
    """86-d action (v2): exactly what the deploy's SMPL encoder (mode 2) tracks.
      smpl_joints(72)  root-relative joint POSITIONS  -> arm/body REACH
      wrist_jp(6)      joint_pos at WRIST_IDX          -> wrist/grasp ORIENTATION
      body_quat(4)     -> smpl_anchor orientation
      grips(4)         L/R grip + L/R trigger
    Drops vr_3point(21) (ignored by mode 2) + heading(1, always 0)."""
    jp = _latest(a.get("joint_pos") or [0.0] * 29, 29)
    wrist = jp[WRIST_IDX] if jp.size >= 29 else np.zeros(6, np.float32)
    parts = [
        _latest(a.get("smpl_joints") or [0.0] * SMPL_PER_FRAME, SMPL_PER_FRAME),
        wrist.astype(np.float32),
        _latest(a.get("body_quat_w") or [1, 0, 0, 0], BODYQ),
        np.array([float(a.get("left_grip") or 0.0), float(a.get("right_grip") or 0.0),
                  float(a.get("left_trigger") or 0.0), float(a.get("right_trigger") or 0.0)], np.float32),
    ]
    return np.concatenate(parts).astype(np.float32)


def _state_vec(st, touch):
    """v2 state: joint_q[29] + right_hand_q[6] (+touch). Drops the dead left_hand_q
    (right-handed teleop -> its std≈0 dims blew up normalization at deploy)."""
    parts = [_flat(st.get("joint_q") or [])[:29],
             _flat(st.get("right_hand_q") or [])[:6]]
    if touch is not None:
        parts.append(touch)
    return np.concatenate(parts).astype(np.float32)


def _touch_vec(frame):
    """RIGHT-hand fingertip normal_force only (v2). Right-handed teleop -> the left
    touch is dead (std≈0), the same dead-dim normalization bomb as left_hand_q."""
    nf = ((frame.get("tactile") or {}).get("right") or {}).get("normal_force") or []
    return _flat(nf).astype(np.float32) if len(nf) else np.zeros(1, np.float32)


def _depth_to_u8(png_bytes):
    import cv2
    arr = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_UNCHANGED)  # uint16 mm
    if arr is None:
        return None
    d = np.clip(arr.astype(np.float32) / 4000.0 * 255.0, 0, 255).astype(np.uint8)  # 0..4m -> 0..255
    return np.repeat(d[:, :, None], 3, axis=2)


def _collect_episodes(sessions):
    """Return [(episode_dir, curation, goal)] for KEPT, curated episodes."""
    base = os.path.abspath(REC_DIR)
    sel = [d for d in sorted(os.listdir(base)) if os.path.isdir(os.path.join(base, d))] \
        if sessions == ["all"] else sessions
    eps = []
    for sess in sel:
        sdir = os.path.join(base, sess)
        if not os.path.isdir(sdir):
            print(f"  ! session not found: {sess}")
            continue
        for ep in sorted(os.listdir(sdir)):
            epdir = os.path.join(sdir, ep)
            cf = os.path.join(epdir, "curation.json")
            dj = os.path.join(epdir, "data.json")
            if not (ep.startswith("episode_") and os.path.isfile(dj) and os.path.isfile(cf)):
                continue
            cur = json.load(open(cf))
            if cur.get("keep") is not True:
                continue
            try:
                goal = json.load(open(dj)).get("text", {}).get("goal", "")
            except Exception:
                goal = ""
            eps.append((epdir, cur, goal))
    return eps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", required=True)
    ap.add_argument("--sessions", default="all", help="comma list of session names, or 'all'")
    ap.add_argument("--images", choices=["rgb", "rgbd"], default="rgb")
    ap.add_argument("--touch", type=int, default=0)
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()
    import cv2
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    try:
        from lerobot.constants import HF_LEROBOT_HOME
    except Exception:
        from lerobot.datasets.lerobot_dataset import HF_LEROBOT_HOME  # fallback
    import shutil

    sessions = [s.strip() for s in args.sessions.split(",") if s.strip()]
    eps = _collect_episodes(sessions)
    if not eps:
        print("没有「要(keep)」的已标注 episode —— 先去步骤8 标注取舍")
        sys.exit(2)
    print(f"==> {len(eps)} 条 keep episode 来自 {len(set(os.path.dirname(e[0]) for e in eps))} 个会话")

    # probe dims from the first frame
    d0 = json.load(open(os.path.join(eps[0][0], "data.json")))["data"]
    f0 = next(f for f in d0 if f.get("state") and f.get("action"))
    touch0 = _touch_vec(f0) if args.touch else None
    state_dim = _state_vec(f0["state"], touch0).shape[0]
    action_dim = _action_vec(f0["action"]).shape[0]
    print(f"==> state_dim={state_dim} action_dim={action_dim} images={args.images} touch={bool(args.touch)}")

    features = {
        "observation.state": {"dtype": "float32", "shape": (state_dim,),
                              "names": [[f"s{i}" for i in range(state_dim)]]},
        "action": {"dtype": "float32", "shape": (action_dim,),
                   "names": [[f"a{i}" for i in range(action_dim)]]},
        "observation.images.cam_high": {"dtype": "video", "shape": (480, 640, 3),
                                        "names": ["height", "width", "channel"]},
    }
    if args.images == "rgbd":
        features["observation.images.cam_high_depth"] = {"dtype": "video", "shape": (480, 640, 3),
                                                         "names": ["height", "width", "channel"]}

    if (HF_LEROBOT_HOME / args.repo_id).exists():
        shutil.rmtree(HF_LEROBOT_HOME / args.repo_id)
    ds = LeRobotDataset.create(repo_id=args.repo_id, fps=args.fps, robot_type="g1_pico",
                               features=features, use_videos=True)

    total = 0
    for epdir, cur, goal in eps:
        data = json.load(open(os.path.join(epdir, "data.json")))["data"]
        s, e = cur.get("start"), cur.get("end")
        s = 0 if s is None else max(0, int(s))
        e = (len(data) - 1) if e is None else min(len(data) - 1, int(e))
        kept = 0
        for i in range(s, e + 1):
            f = data[i]
            st, ac = f.get("state"), f.get("action")
            if not st or not ac or not (f.get("frames") or {}).get("head"):
                continue
            head = cv2.imread(os.path.join(epdir, f["frames"]["head"]))
            if head is None:
                continue
            head = cv2.cvtColor(head, cv2.COLOR_BGR2RGB)
            tv = _touch_vec(f) if args.touch else None
            frame = {
                "observation.state": _state_vec(st, tv),
                "action": _action_vec(ac),
                "observation.images.cam_high": head,
                "task": goal or "g1 pico manipulation",
            }
            if args.images == "rgbd":
                dpath = (f.get("frames") or {}).get("depth")
                du8 = _depth_to_u8(open(os.path.join(epdir, dpath), "rb").read()) if dpath else None
                frame["observation.images.cam_high_depth"] = du8 if du8 is not None else np.zeros((480, 640, 3), np.uint8)
            ds.add_frame(frame)
            kept += 1
        if kept:
            ds.save_episode()
            total += kept
        print(f"  {os.path.basename(os.path.dirname(epdir))}/{os.path.basename(epdir)}: {kept} 帧 [{s}-{e}]")

    print(f"\n==> 数据集完成: {len(eps)} episode, {total} 帧 -> {HF_LEROBOT_HOME / args.repo_id}")
    print(f"    训练: lerobot-train --dataset.repo_id={args.repo_id} --policy.type=act --policy.push_to_hub=false")


if __name__ == "__main__":
    main()
