#!/usr/bin/env python3
"""PICO-output-level playback: replay a recorded episode's teleop COMMAND back
onto the real G1 through the running SONIC deploy.

Each recorded frame's `action` is repacked into the exact v3 'pose' wire message
the streamer would have sent (pack_pose_message + original shapes, so it is
byte-compatible with what the deploy already parses) and published on ZMQ 5556 at
the recorded timing / speed. Playback ITSELF sends the POSE-mode policy-start
command (so the deploy only needs to be at Init Done / WAIT_FOR_CONTROL, not
pre-engaged via the streamer) -> the deploy enters CONTROL, turns the poses into
motor commands and balances the body; if the BrainCo hand bridge is running it
replays the grasp too (triggers/grips are in the message).

The lower body is driven by the recorded smpl_joints (24-joint SMPL incl. feet
22-23) + body_quat_w + heading_increment; the arms by vr 3-point. joint_vel is
not recorded (it is zeros in the live stream) so it is synthesised as zeros.

SAFETY
  * EXACTLY one 5556 publisher — pico_streamer / gui_drive MUST be stopped.
  * Stand first: after engaging CONTROL the robot holds frame 0 (the recording's
    standing start pose) and WAITS — forward playback begins only on an explicit
    {"cmd":"play"} once the operator has eyeballed it standing stable (the
    recording itself started from a standing pose). PB_AUTOPLAY=1 auto-plays after
    RAMP_HOLD_S instead.
  * Default speed 0.5x; live-tunable via stdin {"speed": x}; {"cmd":"quit"} stops.
  * Stops cleanly on SIGINT/SIGTERM (stops publishing; the deploy holds its last
    command — E-stop the deploy with 'o' to damp).

Usage:  playback_pico.py <episode_dir> [speed]
"""
import json
import os
import signal
import sys
import threading
import time

import numpy as np
import zmq

# import the upstream packer (byte-identical v3 format the deploy parses)
_ROOT = os.environ.get("GEAR_SONIC_ROOT",
                       os.path.expanduser("~/gear_sonic_elbow_crawling_isaacsim/GR00T-WholeBodyControl"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from gear_sonic.utils.teleop.zmq.zmq_planner_sender import (
    pack_pose_message, build_command_message,
)

POSE_PORT = int(os.environ.get("PB_POSE_PORT", "5556"))
RAMP_HOLD_S = float(os.environ.get("PB_RAMP_HOLD_S", "2.0"))  # auto-play delay when PB_AUTOPLAY=1
AUTOPLAY = os.environ.get("PB_AUTOPLAY", "0") == "1"          # default: wait for explicit 'play'
SPEED_MIN, SPEED_MAX = 0.25, 1.0

_run = {"on": True}
_ctl = {"speed": 0.5}
_play = {"go": False}   # forward playback starts only after the operator confirms stand


def _log(m):
    print(f"[playback] {m}", flush=True)


def _stdin_thread():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("cmd") == "quit":
            _run["on"] = False
            break
        if d.get("cmd") == "play":
            _play["go"] = True
            _log("play confirmed -> forward playback")
        if "speed" in d:
            try:
                _ctl["speed"] = max(SPEED_MIN, min(SPEED_MAX, float(d["speed"])))
                _log(f"speed -> {_ctl['speed']:.2f}x")
            except (TypeError, ValueError):
                pass


def _arr(v, dtype, shape=None):
    a = np.asarray(v, dtype=dtype)
    return a.reshape(shape) if shape is not None else a


def _build_msgs(data):
    """Pre-build every frame's packed v3 'pose' message from recorded actions.
    Returns (msgs, dts) where dts[i] = recorded seconds from frame i to i+1."""
    frames = [f for f in data if f.get("action")]
    msgs, times = [], []
    for f in frames:
        a = f["action"]
        N = len(a.get("frame_index") or [1])          # buffer depth (=5)
        sj = a.get("smpl_joints") or []
        sp = a.get("smpl_pose") or []
        bq = a.get("body_quat_w") or []
        jp = a.get("joint_pos") or []
        # per-frame inner sizes from the totals (robust to N drift)
        def per(x):
            return (len(x) // N) if N else len(x)
        nd = {
            "smpl_pose":   _arr(sp, np.float32, (N, per(sp) // 3, 3)) if per(sp) % 3 == 0 else _arr(sp, np.float32, (N, per(sp))),
            "smpl_joints": _arr(sj, np.float32, (N, 24, 3)),
            "body_quat_w": _arr(bq, np.float32, (N, per(bq))),
            "joint_pos":   _arr(jp, np.float64, (N, per(jp))),
            "joint_vel":   np.zeros((N, per(jp) if jp else 29), dtype=np.float64),  # zeros in the live stream
            "frame_index": _arr(a.get("frame_index") or list(range(N)), np.int64),
            "vr_position":    _arr(a.get("vr_position") or [0.0] * 9, np.float32),
            "vr_orientation": _arr(a.get("vr_orientation") or ([1, 0, 0, 0] * 3), np.float32),
            "left_trigger":  _arr([a.get("left_trigger", 0.0) or 0.0], np.float32),
            "right_trigger": _arr([a.get("right_trigger", 0.0) or 0.0], np.float32),
            "left_grip":  _arr([a.get("left_grip", 0.0) or 0.0], np.float32),
            "right_grip": _arr([a.get("right_grip", 0.0) or 0.0], np.float32),
            "left_hand_joints":  _arr(a.get("left_hand_joints") or [0.0] * 7, np.float32),
            "right_hand_joints": _arr(a.get("right_hand_joints") or [0.0] * 7, np.float32),
            "heading_increment": _arr([a.get("heading_increment", 0.0) or 0.0], np.float32),
            "toggle_data_collection": np.array([False]),  # never re-trigger recording
            "toggle_data_abort": np.array([False]),
        }
        msgs.append(pack_pose_message(nd, topic="pose", version=3))
        times.append(f.get("t_mono"))
    dts = []
    for i in range(len(times)):
        if i + 1 < len(times) and times[i] is not None and times[i + 1] is not None:
            dts.append(max(0.0, min(0.5, times[i + 1] - times[i])))  # clamp gaps to 0.5s
        else:
            dts.append(1.0 / 30.0)
    return msgs, dts


def main():
    if len(sys.argv) < 2:
        _log("usage: playback_pico.py <episode_dir> [speed]")
        sys.exit(2)
    ep = sys.argv[1]
    if len(sys.argv) > 2:
        try:
            _ctl["speed"] = max(SPEED_MIN, min(SPEED_MAX, float(sys.argv[2])))
        except ValueError:
            pass
    dj = os.path.join(ep, "data.json")
    if not os.path.isfile(dj):
        _log(f"no data.json in {ep}")
        sys.exit(2)
    data = json.load(open(dj)).get("data", [])
    msgs, dts = _build_msgs(data)
    if not msgs:
        _log("episode has no action frames to replay")
        sys.exit(2)

    signal.signal(signal.SIGTERM, lambda *_: _run.__setitem__("on", False))
    signal.signal(signal.SIGINT, lambda *_: _run.__setitem__("on", False))
    threading.Thread(target=_stdin_thread, daemon=True).start()

    ctx = zmq.Context()
    s = ctx.socket(zmq.PUB)
    s.bind(f"tcp://*:{POSE_PORT}")
    time.sleep(0.5)  # let the deploy's SUB connect (slow joiner)

    _log(f"loaded {len(msgs)} frames from {os.path.basename(ep)} @ {_ctl['speed']:.2f}x")

    # STAND FIRST + ENGAGE: the recording started from a standing pose, so settle
    # there before moving. We hold frame 0 AND keep (re)sending the POSE-mode
    # policy-start command — because we freshly bound this PUB, the deploy's SUB
    # needs time to reconnect, and a one-shot start burst gets dropped by the ZMQ
    # slow-joiner (=> CONTROL never engages, robot ignores the poses). Sending it
    # every tick guarantees the deploy enters CONTROL once its SUB is up. Hold
    # until the operator confirms it is standing stable and sends 'play'.
    # (PB_AUTOPLAY=1 auto-plays after RAMP_HOLD_S instead.)
    _log("engaging -> CONTROL + STANDING at start pose (frame 0) — waiting for 'play'"
         + (f", auto in {RAMP_HOLD_S:.0f}s" if AUTOPLAY else ""))
    t0 = time.monotonic()
    while _run["on"]:
        s.send(build_command_message(start=True, stop=False, planner=False))  # (re)engage CONTROL
        s.send(msgs[0])                                                        # hold start pose
        if _play["go"]:
            break
        if AUTOPLAY and (time.monotonic() - t0) >= RAMP_HOLD_S:
            _log("auto-play")
            break
        time.sleep(1.0 / 30.0)
    if _run["on"]:
        _log("playing forward")

    # play forward at the recorded cadence / speed (live-tunable)
    for i, msg in enumerate(msgs):
        if not _run["on"]:
            break
        s.send(msg)
        if i % 60 == 0:
            _log(f"frame {i}/{len(msgs)} ({_ctl['speed']:.2f}x)")
        time.sleep(dts[i] / max(SPEED_MIN, _ctl["speed"]))

    _log("playback finished" if _run["on"] else "playback stopped")
    s.close()
    ctx.term()


if __name__ == "__main__":
    main()
