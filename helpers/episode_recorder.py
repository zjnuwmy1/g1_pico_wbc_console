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
# auto mode: record only while the streamer is in POSE (full-body following) —
# detected by pose freshness, since only POSE sends 'pose' messages (PLANNER /
# frozen / pause stop them). Each continuous POSE segment becomes one episode.
POSE_FRESH = 0.5          # s: pose newer than this => POSE mode active
POSE_OFF_DEBOUNCE = 0.7   # s: pose absent this long => left POSE => stop+save
AUTO_MIN_FRAMES = 15      # auto-episodes shorter than this are discarded
HEADER_SIZE = 1280
# struct format char + byte size per wire dtype
_DTYPE = {"f32": ("f", 4), "f64": ("d", 8), "i32": ("i", 4),
          "i64": ("q", 8), "u8": ("B", 1), "bool": ("?", 1)}
_TOPICS = (b"command", b"planner", b"pose")

# ── shared latest-sample buffers ────────────────────────────────────────────
_lock = threading.Lock()
_latest = {"pose": None, "pose_t": 0.0,
           "color": None, "color_t": 0.0, "color_chg": 0.0,
           "depth": None, "depth_t": 0.0, "depth_chg": 0.0,
           "lowstate": None, "lowstate_t": 0.0,
           "lhand": None, "lhand_t": 0.0,
           "rhand": None, "rhand_t": 0.0,
           "ltouch": None, "ltouch_t": 0.0,
           "rtouch": None, "rtouch_t": 0.0}
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
    # conflate => always hold the freshest frame, never a stuck backlog one
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.SUB)
    s.setsockopt(zmq.SUBSCRIBE, b"")
    s.setsockopt(zmq.CONFLATE, 1)
    s.setsockopt(zmq.RCVTIMEO, 1000)
    s.connect(f"tcp://{CAM_HOST}:{port}")
    last_h = None
    while _run["on"]:
        try:
            buf = s.recv()
        except zmq.Again:
            continue
        except Exception:
            break
        h = hash(buf)
        with _lock:
            _latest[key] = buf
            _latest[key + "_t"] = time.monotonic()
            if h != last_h:                       # content actually changed
                _latest[key + "_chg"] = time.monotonic()
            last_h = h


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
        from touch_idl import TouchHand_   # sits next to this script (helpers/)
    except Exception as e:
        TouchHand_ = None
        _log(f"touch_idl unavailable, tactile off: {e}")
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

    def touch_cb(side):
        # BrainCo Revo2 fingertip sensors: 65 values/hand (5 fingers ×
        # normal/tangential force, proximity, status). Stored flat per field.
        def cb(m):
            with _lock:
                _latest[side] = {
                    "normal_force": list(m.normal_force),
                    "tangential_force": list(m.tangential_force),
                    "tangential_direction": list(m.tangential_direction),
                    "self_proximity": list(m.self_proximity),
                    "mutual_proximity": list(m.mutual_proximity),
                    "status": list(m.status),
                }
                _latest[side + "_t"] = time.monotonic()
        return cb

    ChannelSubscriber("rt/lowstate", LowState_).Init(low_cb, 10)
    # NOTE topics are left/right (full words), NOT l/r — matches brainco_hand_server
    ChannelSubscriber("rt/brainco/left/state", MotorStates_).Init(hand_cb("lhand"), 10)
    ChannelSubscriber("rt/brainco/right/state", MotorStates_).Init(hand_cb("rhand"), 10)
    if TouchHand_ is not None:
        ChannelSubscriber("rt/brainco/left/touch", TouchHand_).Init(touch_cb("ltouch"), 10)
        ChannelSubscriber("rt/brainco/right/touch", TouchHand_).Init(touch_cb("rtouch"), 10)
    _log("DDS subscribers up (rt/lowstate, rt/brainco/{left,right}/{state,touch})")
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
_auto = {"on": False, "last_fresh": 0.0}   # auto-follow-POSE recording
# persist the auto-follow toggle so a recorder restart (camera/DDS recovery,
# crash) doesn't silently disable it — otherwise POSE segments stop recording
# until the operator re-ticks the box, which is easy to miss.
_AUTO_STATE_FILE = os.path.join(os.path.dirname(__file__), ".rec_auto_follow")


def _save_auto_state():
    try:
        with open(_AUTO_STATE_FILE, "w") as f:
            f.write("1" if _auto["on"] else "0")
    except Exception:
        pass


def _load_auto_state():
    try:
        with open(_AUTO_STATE_FILE) as f:
            if f.read().strip() == "1":
                _auto["on"] = True
                _auto["last_fresh"] = time.monotonic()
                _log("AUTO follow-POSE restored = ON (persisted across restart)")
    except Exception:
        pass


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


def _stop_episode(save=True, min_frames=1):
    if not _ep["active"]:
        return
    _ep["active"] = False
    if not save or len(_ep["data"]) < min_frames:
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
            "tactile_keys": ["normal_force", "tangential_force", "tangential_direction",
                             "self_proximity", "mutual_proximity", "status"],
            "tactile_layout": "BrainCo Revo2, 5 fingers [thumb,index,middle,ring,pinky] flattened; 65/hand",
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
                "rhand": _latest["rhand"] is not None,
                "ltouch": _latest["ltouch"] is not None,
                "rtouch": _latest["rtouch"] is not None}


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
        lt, rt = _latest["ltouch"], _latest["rtouch"]
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
    tactile = None
    if lt or rt:
        tactile = {}
        if lt:
            tactile["left"] = lt
        if rt:
            tactile["right"] = rt
    rec = {"idx": i, "stage": _ep["stage"], "stage_label": _ep["stage_label"],
           "t_mono": round(time.monotonic(), 6),
           "t_real": round(time.time(), 6),
           "frames": frames, "action": action, "state": state, "tactile": tactile}
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
    elif cmd == "auto":
        _auto["on"] = bool(d.get("on", True))
        _auto["last_fresh"] = time.monotonic()
        _save_auto_state()
        _log(f"AUTO follow-POSE = {'ON' if _auto['on'] else 'off'}"
             f"（POSE 模式自动录，切走自动停存）")
        if not _auto["on"] and _ep["active"]:
            _stop_episode(save=True, min_frames=AUTO_MIN_FRAMES)
    elif cmd == "quit":
        _run["on"] = False


def main():
    _log(f"recorder starting (iface={IFACE}, fps={FPS}, dir={os.path.abspath(REC_DIR)})")
    _load_auto_state()
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
        now = time.monotonic()
        with _lock:
            pose_fresh = (_latest["pose"] is not None
                          and (now - _latest["pose_t"]) < POSE_FRESH)
        # auto-follow-POSE: each continuous POSE segment = one episode
        if _auto["on"]:
            if pose_fresh:
                _auto["last_fresh"] = now
                if not _ep["active"]:
                    _start_episode()
            elif _ep["active"] and (now - _auto["last_fresh"]) > POSE_OFF_DEBOUNCE:
                _stop_episode(save=True, min_frames=AUTO_MIN_FRAMES)
        # write a frame; in auto mode only while POSE is fresh (no gap frames)
        if _ep["active"] and (not _auto["on"] or pose_fresh):
            _write_frame()
        if now - last_status >= 1.0:
            have = _have_sources()
            srcs = ",".join(k for k, v in have.items() if v) or "none"
            # camera-frozen detector: frame content hasn't changed in >2s while a
            # camera is connected => silently recording duplicate frames (bad data)
            with _lock:
                cam_frozen = (_latest["color"] is not None
                              and (now - _latest["color_chg"]) > 2.0)
            cam = " CAM_FROZEN!" if cam_frozen else ""
            _log(f"STATUS rec={'ON' if _ep['active'] else 'off'} "
                 f"auto={'ON' if _auto['on'] else 'off'} "
                 f"pose={'POSE' if pose_fresh else '-'}{cam} "
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
