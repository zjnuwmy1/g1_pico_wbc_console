#!/usr/bin/env python3
"""Teleop episode recorder for imitation-learning (StageAct) data collection.

Records synchronized (observation, action, stage) frames while you teleoperate
the G1 to perform a task (e.g. pick up a bottle), driving the *existing*
gear_sonic WBC + BrainCo hand stack. This is "Option A": the recorded ACTION is
the operator's teleop command (wrist 3-point targets + hand close), so a policy
trained on it can be replayed straight back through the same stack.

Sources (each optional — records whatever is reachable):
  • ZMQ 5556 'pose'  : action (vr_position[9], vr_orientation[12], triggers,
                       grips, hand_joints[7×2]) + body target joint_pos[29] +
                       timestamps + toggle_data_collection / toggle_data_abort
                       (PICO controller can start/stop/abort episodes hands-free)
  • ZMQ 55555 (PC2)  : head camera colour, raw JPEG bytes (stored as-is)
  • ZMQ 55565 (PC2)  : head camera depth, raw PNG-16 bytes (stored as-is)
  • DDS rt/lowstate           : actual robot proprio (joint q/dq, torso IMU)
  • DDS rt/brainco/{l,r}/state: actual hand motor q (+ tactile if present)

On-disk layout (one session per run):
  recordings/<session_ts>/
    episode_0000/
      frames/000000_head.jpg          # raw colour JPEG
      frames/000000_depth.png         # raw depth PNG-16 (if depth on)
      data.json                       # info + text + per-frame data[]
    episode_0001/ ...

Control — one JSON object per stdin line (the console writes these):
  {"cmd":"start"}                 begin a new episode
  {"cmd":"stop"}                  finish & save current episode
  {"cmd":"discard"}              drop current episode without saving
  {"cmd":"stage","stage":1}       set current stage index (label optional)
  {"cmd":"goal","goal":"拿瓶子"}   set task goal text for the session
  {"cmd":"quit"}                  graceful shutdown
The PICO controller's toggle_data_collection edge also starts/stops; its
toggle_data_abort edge discards. Status is printed as `[rec] ...` stdout lines.

Usage:  episode_recorder.py [iface]      (iface default enp132s0)
"""
import json
import os
import struct
import sys
import threading
import time

import zmq

# ── config (env-overridable) ────────────────────────────────────────────────
IFACE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REC_IFACE", "enp132s0")
FPS = float(os.environ.get("REC_FPS", "30"))
REC_DIR = os.environ.get("REC_DIR", os.path.join(os.path.dirname(__file__), "..", "recordings"))
ZMQ_HOST = os.environ.get("REC_ZMQ_HOST", "127.0.0.1")          # streamer (pose) is local
CAM_HOST = os.environ.get("REC_CAM_HOST", "192.168.123.164")    # PC2
POSE_PORT = int(os.environ.get("REC_POSE_PORT", "5556"))
COLOR_PORT = int(os.environ.get("REC_COLOR_PORT", "55555"))
DEPTH_PORT = int(os.environ.get("REC_DEPTH_PORT", "55565"))
HEADER_SIZE = 1280
# struct format char + byte size per wire dtype
_DTYPE = {"f32": ("f", 4), "f64": ("d", 8), "i32": ("i", 4),
          "i64": ("q", 8), "u8": ("B", 1), "bool": ("?", 1)}
_TOPICS = (b"command", b"planner", b"pose")

# ── shared latest-sample buffers ────────────────────────────────────────────
_lock = threading.Lock()
_latest = {"pose": None, "pose_t": 0.0,
           "color": None, "color_t": 0.0,
           "depth": None, "depth_t": 0.0,
           "lowstate": None, "lowstate_t": 0.0,
           "lhand": None, "lhand_t": 0.0,
           "rhand": None, "rhand_t": 0.0}
_ctrl = {"cmd": None}            # one-shot control from stdin
_run = {"on": True}


def _log(msg):
    print(f"[rec] {msg}", flush=True)


# ── generic self-describing message parser (topic + 1280B JSON header) ───────
def _parse_msg(data: bytes):
    for topic in _TOPICS:
        if data.startswith(topic):
            rest = data[len(topic):]
            try:
                hdr = json.loads(rest[:HEADER_SIZE].rstrip(b"\x00").decode("utf-8"))
            except Exception:
                return None, None
            payload, off, out = rest[HEADER_SIZE:], 0, {}
            for f in hdr.get("fields", []):
                code, sz = _DTYPE.get(f["dtype"], (None, None))
                if code is None:
                    return topic.decode(), out
                n = 1
                for d in f.get("shape", [1]):
                    n *= int(d)
                vals = struct.unpack_from("<" + code * n, payload, off)
                off += sz * n
                out[f["name"]] = list(vals) if n > 1 else vals[0]
            return topic.decode(), out
    return None, None


# ── receiver threads ────────────────────────────────────────────────────────
def _zmq_sub(endpoint, sub_filter=b""):
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.SUB)
    s.setsockopt(zmq.SUBSCRIBE, sub_filter)
    s.setsockopt(zmq.RCVTIMEO, 1000)
    s.setsockopt(zmq.RCVHWM, 2)
    s.connect(endpoint)
    return s


def _pose_thread():
    s = _zmq_sub(f"tcp://{ZMQ_HOST}:{POSE_PORT}", b"pose")
    last_tog, last_abort = 0, 0
    while _run["on"]:
        try:
            data = s.recv()
        except zmq.Again:
            continue
        except Exception:
            break
        topic, fields = _parse_msg(data)
        if topic != "pose" or not fields:
            continue
        with _lock:
            _latest["pose"] = fields
            _latest["pose_t"] = time.monotonic()
        # PICO controller edges -> episode control (hands-free)
        tog = int(fields.get("toggle_data_collection", 0) or 0)
        abrt = int(fields.get("toggle_data_abort", 0) or 0)
        if tog and not last_tog:
            with _lock:
                _ctrl["cmd"] = {"cmd": "stop" if _ep["active"] else "start", "src": "pico"}
        if abrt and not last_abort:
            with _lock:
                _ctrl["cmd"] = {"cmd": "discard", "src": "pico"}
        last_tog, last_abort = tog, abrt


def _img_thread(port, key):
    s = _zmq_sub(f"tcp://{CAM_HOST}:{port}")
    while _run["on"]:
        try:
            buf = s.recv()
        except zmq.Again:
            continue
        except Exception:
            break
        with _lock:
            _latest[key] = buf
            _latest[key + "_t"] = time.monotonic()


def _dds_threads():
    """rt/lowstate + rt/brainco/{l,r}/state via unitree_sdk2py (optional)."""
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_
    except Exception as e:
        _log(f"DDS unavailable, proprio/hand state off: {e}")
        return
    try:
        ChannelFactoryInitialize(0, IFACE)
    except Exception as e:
        _log(f"DDS init failed ({IFACE}): {e}")
        return

    def low_cb(m):
        q = [m.motor_state[i].q for i in range(29)]
        dq = [m.motor_state[i].dq for i in range(29)]
        imu = m.imu_state
        with _lock:
            _latest["lowstate"] = {"joint_q": q, "joint_dq": dq,
                                   "imu_quat": list(imu.quaternion),
                                   "imu_gyro": list(imu.gyroscope)}
            _latest["lowstate_t"] = time.monotonic()

    def hand_cb(side):
        def cb(m):
            n = len(m.states)
            q = [m.states[i].q for i in range(n)]
            with _lock:
                _latest[side] = {"q": q, "n": n}
                _latest[side + "_t"] = time.monotonic()
        return cb

    ChannelSubscriber("rt/lowstate", LowState_).Init(low_cb, 10)
    ChannelSubscriber("rt/brainco/l/state", MotorStates_).Init(hand_cb("lhand"), 10)
    ChannelSubscriber("rt/brainco/r/state", MotorStates_).Init(hand_cb("rhand"), 10)
    _log("DDS subscribers up (rt/lowstate, rt/brainco/{l,r}/state)")
    while _run["on"]:
        time.sleep(0.5)


def _stdin_thread():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        with _lock:
            _ctrl["cmd"] = d


# ── episode state + writing ─────────────────────────────────────────────────
_ep = {"active": False, "dir": None, "idx": -1, "frame": 0, "stage": 0,
       "stage_label": "", "data": []}
_session = {"dir": None, "count": 0, "goal": ""}


def _ensure_session():
    if _session["dir"]:
        return
    base = os.path.abspath(REC_DIR)
    os.makedirs(base, exist_ok=True)
    # session id from existing dirs (no Date.now allowed elsewhere, but this is fine)
    ts = time.strftime("%Y%m%d_%H%M%S")
    _session["dir"] = os.path.join(base, ts)
    os.makedirs(_session["dir"], exist_ok=True)
    _log(f"session dir: {_session['dir']}")


def _start_episode():
    if _ep["active"]:
        _log("already recording; ignoring start")
        return
    _ensure_session()
    _ep["idx"] += 1
    _ep["dir"] = os.path.join(_session["dir"], f"episode_{_ep['idx']:04d}")
    os.makedirs(os.path.join(_ep["dir"], "frames"), exist_ok=True)
    _ep["frame"], _ep["stage"], _ep["stage_label"], _ep["data"] = 0, 0, "", []
    _ep["active"] = True
    _log(f"START episode_{_ep['idx']:04d}")


def _stop_episode(save=True):
    if not _ep["active"]:
        return
    _ep["active"] = False
    if not save or not _ep["data"]:
        _log(f"DISCARD episode_{_ep['idx']:04d} ({len(_ep['data'])} frames)")
        _ep["idx"] -= 1   # reuse index
        return
    have = _have_sources()
    meta = {
        "info": {
            "version": "0.1.0", "fps": FPS, "iface": IFACE,
            "cameras": [k for k in ("head", "depth") if have.get(k)],
            "action_keys": ["vr_position", "vr_orientation", "left_trigger",
                            "right_trigger", "left_grip", "right_grip",
                            "left_hand_joints", "right_hand_joints"],
            "state_keys": ["joint_q", "joint_dq", "imu_quat", "imu_gyro",
                           "left_hand_q", "right_hand_q"],
            "vr_3point": "[L-wrist,R-wrist,Neck] pos(9)+quat_wxyz(12), root frame",
        },
        "text": {"goal": _session["goal"]},
        "data": _ep["data"],
    }
    with open(os.path.join(_ep["dir"], "data.json"), "w") as f:
        json.dump(meta, f, separators=(",", ":"))
    _session["count"] += 1
    _log(f"SAVED episode_{_ep['idx']:04d}: {len(_ep['data'])} frames "
         f"(session total {_session['count']})")


def _have_sources():
    with _lock:
        return {"head": _latest["color"] is not None,
                "depth": _latest["depth"] is not None,
                "pose": _latest["pose"] is not None,
                "lowstate": _latest["lowstate"] is not None,
                "lhand": _latest["lhand"] is not None,
                "rhand": _latest["rhand"] is not None}


_ACT_KEYS = ("vr_position", "vr_orientation", "left_trigger", "right_trigger",
             "left_grip", "right_grip", "left_hand_joints", "right_hand_joints")


def _write_frame():
    """Snapshot latest of every stream and append one frame to the episode."""
    with _lock:
        pose = _latest["pose"]
        color = _latest["color"]
        depth = _latest["depth"]
        low = _latest["lowstate"]
        lh, rh = _latest["lhand"], _latest["rhand"]
    if pose is None and color is None:
        return  # nothing worth recording yet
    i = _ep["frame"]
    frames = {}
    if color is not None:
        p = os.path.join("frames", f"{i:06d}_head.jpg")
        with open(os.path.join(_ep["dir"], p), "wb") as f:
            f.write(color)
        frames["head"] = p
    if depth is not None:
        p = os.path.join("frames", f"{i:06d}_depth.png")
        with open(os.path.join(_ep["dir"], p), "wb") as f:
            f.write(depth)
        frames["depth"] = p
    action = {k: pose.get(k) for k in _ACT_KEYS} if pose else None
    state = None
    if low or lh or rh:
        state = dict(low or {})
        if lh:
            state["left_hand_q"] = lh["q"]
        if rh:
            state["right_hand_q"] = rh["q"]
    rec = {"idx": i, "stage": _ep["stage"], "stage_label": _ep["stage_label"],
           "t_mono": round(time.monotonic(), 6),
           "t_real": round(time.time(), 6),
           "frames": frames, "action": action, "state": state}
    if pose and "timestamp_realtime" in pose:
        rec["pose_t_real"] = pose["timestamp_realtime"]
    _ep["data"].append(rec)
    _ep["frame"] += 1


def _apply_ctrl(d):
    cmd = d.get("cmd")
    if cmd == "start":
        _start_episode()
    elif cmd == "stop":
        _stop_episode(save=True)
    elif cmd == "discard":
        _stop_episode(save=False)
    elif cmd == "stage":
        if "stage" in d:
            _ep["stage"] = int(d["stage"])
        else:
            _ep["stage"] += 1
        _ep["stage_label"] = str(d.get("label", _ep["stage_label"]))
        _log(f"STAGE -> {_ep['stage']} {_ep['stage_label']}".rstrip())
    elif cmd == "goal":
        _session["goal"] = str(d.get("goal", ""))
        _log(f"goal set: {_session['goal']}")
    elif cmd == "quit":
        _run["on"] = False


def main():
    _log(f"recorder starting (iface={IFACE}, fps={FPS}, dir={os.path.abspath(REC_DIR)})")
    threading.Thread(target=_pose_thread, daemon=True).start()
    threading.Thread(target=_img_thread, args=(COLOR_PORT, "color"), daemon=True).start()
    threading.Thread(target=_img_thread, args=(DEPTH_PORT, "depth"), daemon=True).start()
    threading.Thread(target=_dds_threads, daemon=True).start()
    threading.Thread(target=_stdin_thread, daemon=True).start()

    dt = 1.0 / FPS
    next_t = time.monotonic()
    last_status = 0.0
    while _run["on"]:
        with _lock:
            d = _ctrl["cmd"]
            _ctrl["cmd"] = None
        if d:
            _apply_ctrl(d)
        if _ep["active"]:
            _write_frame()
        now = time.monotonic()
        if now - last_status >= 1.0:
            have = _have_sources()
            srcs = ",".join(k for k, v in have.items() if v) or "none"
            _log(f"STATUS rec={'ON' if _ep['active'] else 'off'} "
                 f"ep={_ep['idx'] if _ep['active'] else '-'} "
                 f"frames={_ep['frame'] if _ep['active'] else 0} "
                 f"stage={_ep['stage']} saved={_session['count']} src=[{srcs}]")
            last_status = now
        next_t += dt
        sleep = next_t - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_t = time.monotonic()
    if _ep["active"]:
        _stop_episode(save=True)
    _log("recorder stopped")


if __name__ == "__main__":
    main()
