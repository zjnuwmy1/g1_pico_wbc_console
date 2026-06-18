#!/usr/bin/env python3
"""Build a LeRobot dataset for ACT training from CURATED console recordings.

Option-A: the policy predicts the operator's PICO teleop COMMAND, which at
inference is fed to the SONIC deploy (like playback) — so the ACTION here is the
full PICO command (incl. waist via SMPL), and the OBSERVATION is the head camera
(RGB or RGB+depth) + robot proprioception (+ optional tactile).

Reads only KEPT episodes (curation.json keep==true) and TRIMS each to its useful
[start,end] frame range, across one or more sessions.

  observation.state : joint_q[29] (incl. waist 12-14) + left_hand_q[6] +
                      right_hand_q[6]  (+ tactile normal_force L/R if --touch)
  observation.images.cam_high       : head RGB  (480x640x3)
  observation.images.cam_high_depth : head depth as uint8 grayscale (if --images rgbd)
  action            : vr_position[9] + vr_orientation[12] + smpl_joints_latest[72]
                      + body_quat_w_latest[4] + heading_increment[1]
                      + left/right grip + left/right trigger [4]   = 102

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

# action layout (Option-A PICO command -> SONIC deploy)
SMPL_PER_FRAME = 72   # 24 SMPL joints x 3
BODYQ = 4


def _flat(v):
    return np.asarray(v, dtype=np.float32).reshape(-1)


def _latest(buf, per):
    """Last `per` values of a flattened N-frame buffer (newest frame)."""
    a = _flat(buf)
    return a[-per:] if a.size >= per else np.pad(a, (0, per - a.size))


def _action_vec(a):
    """102-d PICO command from one frame's recorded `action`."""
    g = lambda k, n: _flat(a.get(k) if a.get(k) is not None else [0.0] * n)[:n]
    parts = [
        g("vr_position", 9),
        g("vr_orientation", 12),
        _latest(a.get("smpl_joints") or [0.0] * SMPL_PER_FRAME, SMPL_PER_FRAME),
        _latest(a.get("body_quat_w") or [1, 0, 0, 0], BODYQ),
        np.array([float(a.get("heading_increment") or 0.0)], np.float32),
        np.array([float(a.get("left_grip") or 0.0), float(a.get("right_grip") or 0.0),
                  float(a.get("left_trigger") or 0.0), float(a.get("right_trigger") or 0.0)], np.float32),
    ]
    return np.concatenate(parts).astype(np.float32)


def _state_vec(st, touch):
    parts = [_flat(st.get("joint_q") or [])[:29],
             _flat(st.get("left_hand_q") or [])[:6],
             _flat(st.get("right_hand_q") or [])[:6]]
    if touch is not None:
        parts.append(touch)
    return np.concatenate(parts).astype(np.float32)


def _touch_vec(frame):
    """Per-hand normal_force (contact signal) when --touch; zeros if absent."""
    tac = frame.get("tactile") or {}
    out = []
    for side in ("left", "right"):
        nf = (tac.get(side) or {}).get("normal_force") or []
        out.append(_flat(nf))
    n = max(1, max(x.size for x in out))
    return np.concatenate([np.pad(x, (0, n - x.size)) for x in out]).astype(np.float32)


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
