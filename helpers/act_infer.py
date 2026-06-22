#!/usr/bin/env python3
"""ACT autonomous execution: a trained ACT policy drives the real G1 through the
running SONIC deploy (the inference counterpart of playback_pico.py).

Each control tick we read the SAME observation the policy trained on — head
camera (RGB, +depth if the model is RGBD) + robot proprioception (joint_q[29] +
both hand_q[6], +tactile if the model used touch) — ask the policy for the 102-d
PICO command, repack it into the exact v3 'pose' wire message the deploy parses,
and publish it on ZMQ 5556. The deploy turns the poses into motor commands and
SONIC balances the body; the BrainCo hand bridge (if running) replays the grasp
from the triggers/grips in the message.

Torch/lerobot live in the act_train conda env; unitree_sdk2py + the camera live
in the teleop venv. So THIS process (teleop venv) spawns act_policy_server.py in
the act_train env and talks to it over a local socket — keeping torch out of the
DDS process and the cyclonedds setup untouched.

The model predicts vr_3point + smpl_joints + body_quat + heading + grips. It does
NOT predict joint_pos/joint_vel/smpl_pose. Per observation_config.yaml those are
not primary policy inputs for standing manipulation, so we fill joint_pos with
the robot's CURRENT measured joint_q (safe "hold"), joint_vel + smpl_pose = 0.

SAFETY (same as playback)
  * EXACTLY one 5556 publisher — pico_streamer / gui_drive / playback MUST be off.
  * Stand first: after engaging CONTROL the robot HOLDS the first predicted pose
    and WAITS; live autonomous execution begins only on an explicit {"cmd":"play"}
    once the operator has eyeballed it standing stable. PB_AUTOPLAY=1 auto-plays.
  * {"cmd":"pause"} returns to the held pose; {"cmd":"quit"} stops publishing (the
    deploy holds its last command — E-stop the deploy with 'o' to damp).

Usage: act_infer.py <checkpoint_dir> <iface>
       env WBC_ACT_PY = python of the act_train env (for the policy server)
"""
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque

import cv2
import numpy as np
import zmq

# upstream v3 packer (byte-identical to the streamer / what the deploy parses)
_ROOT = os.environ.get("GEAR_SONIC_ROOT",
                       os.path.expanduser("~/gear_sonic_elbow_crawling_isaacsim/GR00T-WholeBodyControl"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from gear_sonic.utils.teleop.zmq.zmq_planner_sender import (  # noqa: E402
    pack_pose_message, build_command_message)

# ── config ───────────────────────────────────────────────────────────────────
CKPT = sys.argv[1] if len(sys.argv) > 1 else ""
IFACE = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("REC_IFACE", "enp132s0")
ACT_PY = os.environ.get("WBC_ACT_PY", "")
HERE = os.path.dirname(os.path.abspath(__file__))
POLICY_SCRIPT = os.path.join(HERE, "act_policy_server.py")
POLICY_PORT = int(os.environ.get("INFER_POLICY_PORT", "5560"))
# latest head-camera frames (what the model sees) -> the console serves these so
# the GUI shows the robot's live viewpoint (RGB always, depth for RGBD models).
CAM_OUT = os.path.join(os.path.dirname(HERE), "state", "infer_cam.jpg")
DEPTH_OUT = os.path.join(os.path.dirname(HERE), "state", "infer_depth.jpg")
RESET_BASE_S = float(os.environ.get("INFER_RESET_S", "3.0"))  # glide-to-initial time @ 1x
# SAFETY: the deploy applies NO smoothing on CONTROL entry (verified in
# g1_deploy_onnx_ref.cpp: q_target = default + policy_out*scale, no ramp/clip).
# So engaging straight into frame1 can jerk the motors. We glide from a SEED that
# matches the robot's CURRENT measured pose into frame1 over RAMP_IN_S.
RAMP_IN_S = float(os.environ.get("INFER_RAMP_IN_S", "1.5"))   # engage glide-in time @ 1x
# frame-save postmortem: INFER_FRAMESAVE=1 dumps (state, predicted action, head
# image) per ~3rd RUN tick to FRAMESAVE_DIR for diag_framesave.py (on-robot A/B).
FRAMESAVE = os.environ.get("INFER_FRAMESAVE", "0") == "1"
FRAMESAVE_DIR = os.environ.get("INFER_FRAMESAVE_DIR", "/tmp/infer_frames")

CAM_HOST = os.environ.get("REC_CAM_HOST", "192.168.123.164")
COLOR_PORT = int(os.environ.get("REC_COLOR_PORT", "55555"))
DEPTH_PORT = int(os.environ.get("REC_DEPTH_PORT", "55565"))
POSE_PORT = int(os.environ.get("PB_POSE_PORT", "5556"))
RATE_HZ = float(os.environ.get("INFER_RATE_HZ", "30"))
RAMP_HOLD_S = float(os.environ.get("PB_RAMP_HOLD_S", "2.0"))
AUTOPLAY = os.environ.get("PB_AUTOPLAY", "0") == "1"
N = 5  # rolling window depth (mirrors the streamer / playback)
WRIST_IDX = [19, 20, 21, 26, 27, 28]   # G1 29-dof mujoco: L/R wrist roll/pitch/yaw
# action/state layout, learned from the model at ping. v2 (action_dim 86): action
# = smpl_joints(72)+wrist_jp(6)+body_quat(4)+grips(4); state drops dead left_hand.
# v1 (102): the original layout. Kept so old models still deploy for A/B.
_V2 = {"on": False}

_run = {"on": True}
# live test controls (set via stdin): speed scales the action rate; loop auto-
# repeats a round every round_s seconds; release_s = how long fingers stay closed
# after a round ends before opening; cmd = one-shot play/pause/next.
_ctl = {"speed": 1.0, "loop": False, "round_s": 0.0, "release_s": 2.0,
        "ramp_in_s": RAMP_IN_S, "cmd": None}
_lock = threading.Lock()
_latest = {"color": None, "color_t": 0.0, "depth": None, "depth_t": 0.0,
           "lowstate": None, "lhand": None, "rhand": None, "ltouch": None, "rtouch": None}


def _log(m):
    print(f"[infer] {m}", flush=True)


# ── policy-server IPC (stdlib socket frames; see act_policy_server.py) ─────────
def _recv_n(sock, n):
    buf = bytearray()
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            return None
        buf += c
    return bytes(buf)


def _recv_frame(sock):
    h = _recv_n(sock, 4)
    if not h:
        return None
    (ln,) = struct.unpack(">I", h)
    body = _recv_n(sock, ln)
    (hl,) = struct.unpack(">I", body[:4])
    return json.loads(body[4:4 + hl].decode()), body[4 + hl:]


def _send_frame(sock, hdr, payload=b""):
    hb = json.dumps(hdr).encode()
    body = struct.pack(">I", len(hb)) + hb + payload
    sock.sendall(struct.pack(">I", len(body)) + body)


# ── camera + DDS receivers (mirror episode_recorder) ──────────────────────────
def _img_thread(port, key):
    ctx = zmq.Context.instance()
    s = ctx.socket(zmq.SUB)
    s.setsockopt(zmq.SUBSCRIBE, b"")
    s.setsockopt(zmq.CONFLATE, 1)
    s.setsockopt(zmq.RCVTIMEO, 1000)
    s.connect(f"tcp://{CAM_HOST}:{port}")
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


def _dds_threads(want_touch):
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_
    except Exception as e:
        _log(f"DDS unavailable (proprio/hands off): {e}")
        return
    TouchHand_ = None
    if want_touch:
        try:
            from touch_idl import TouchHand_
        except Exception as e:
            _log(f"touch_idl unavailable, tactile zeros: {e}")
    try:
        ChannelFactoryInitialize(0, IFACE)
    except Exception as e:
        _log(f"DDS init failed ({IFACE}): {e}")
        return

    def low_cb(m):
        q = [m.motor_state[i].q for i in range(29)]
        with _lock:
            _latest["lowstate"] = {"joint_q": q}

    def hand_cb(side):
        def cb(m):
            q = [m.states[i].q for i in range(len(m.states))]
            with _lock:
                _latest[side] = {"q": q}
        return cb

    def touch_cb(side):
        def cb(m):
            with _lock:
                _latest[side] = {"normal_force": list(m.normal_force)}
        return cb

    ChannelSubscriber("rt/lowstate", LowState_).Init(low_cb, 10)
    ChannelSubscriber("rt/brainco/left/state", MotorStates_).Init(hand_cb("lhand"), 10)
    ChannelSubscriber("rt/brainco/right/state", MotorStates_).Init(hand_cb("rhand"), 10)
    if TouchHand_ is not None:
        ChannelSubscriber("rt/brainco/left/touch", TouchHand_).Init(touch_cb("ltouch"), 10)
        ChannelSubscriber("rt/brainco/right/touch", TouchHand_).Init(touch_cb("rtouch"), 10)
    _log("DDS subscribers up (rt/lowstate, rt/brainco/{left,right})")
    while _run["on"]:
        time.sleep(0.5)


def _cam_writer():
    """Mirror the freshest head-camera JPEG to state/infer_cam.jpg (~10 fps) so
    the GUI can show the robot's live viewpoint. Atomic via tmp+rename."""
    tmp = CAM_OUT + ".tmp"
    while _run["on"]:
        with _lock:
            buf = _latest["color"]
        if buf:
            try:
                with open(tmp, "wb") as f:
                    f.write(buf)
                os.replace(tmp, CAM_OUT)
            except OSError:
                pass
        time.sleep(0.1)


def _depth_writer():
    """For RGBD models, mirror a colorized depth view to state/infer_depth.jpg so
    the GUI can show what the depth channel looks like."""
    tmp = DEPTH_OUT + ".tmp"
    while _run["on"]:
        with _lock:
            buf = _latest["depth"]
        if buf:
            try:
                arr = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_UNCHANGED)
                if arr is not None:
                    d = np.clip(arr.astype(np.float32) / 4000.0 * 255.0, 0, 255).astype(np.uint8)
                    viz = cv2.applyColorMap(d, cv2.COLORMAP_JET)
                    ok, enc = cv2.imencode(".jpg", viz, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok:
                        with open(tmp, "wb") as f:
                            f.write(enc.tobytes())
                        os.replace(tmp, DEPTH_OUT)
            except Exception:
                pass
        time.sleep(0.12)


_fs = {"k": 0}


def _framesave(st, action, color):
    """Dump (state, predicted action, head image) every ~3rd RUN tick to
    FRAMESAVE_DIR so diag_framesave.py can check, on the real robot, whether the
    wrist actually moves + the obs is in-distribution (the v3b postmortem method)."""
    k = _fs["k"]
    _fs["k"] = k + 1
    if k % 3:
        return
    try:
        os.makedirs(FRAMESAVE_DIR, exist_ok=True)
        np.save(f"{FRAMESAVE_DIR}/{k:06d}_state.npy", np.asarray(st, np.float32))
        np.save(f"{FRAMESAVE_DIR}/{k:06d}_action.npy", np.asarray(action, np.float32))
        if color is not None:
            cv2.imwrite(f"{FRAMESAVE_DIR}/{k:06d}_head.jpg", cv2.cvtColor(color, cv2.COLOR_RGB2BGR))
    except Exception:
        pass


def _stdin_thread():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        c = d.get("cmd")
        if c == "quit":
            _run["on"] = False
            break
        if c in ("play", "pause", "next", "refresh"):
            _ctl["cmd"] = c
            _log(f"cmd: {c}")
        for k in ("speed", "round_s", "release_s", "ramp_in_s"):
            if k in d:
                try:
                    _ctl[k] = float(d[k])
                except (TypeError, ValueError):
                    pass
        if "loop" in d:
            _ctl["loop"] = bool(d["loop"])
        if any(k in d for k in ("speed", "round_s", "release_s", "loop", "ramp_in_s")):
            _log(f"ctl: speed={_ctl['speed']:.2f} loop={_ctl['loop']} "
                 f"round={_ctl['round_s']:.0f}s release={_ctl['release_s']:.1f}s "
                 f"ramp={_ctl['ramp_in_s']:.1f}s")


# ── observation builders ──────────────────────────────────────────────────────
def _get_color():
    with _lock:
        buf = _latest["color"]
    if not buf:
        return None
    img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    if img.shape[:2] != (480, 640):
        img = cv2.resize(img, (640, 480))
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def _get_depth():
    with _lock:
        buf = _latest["depth"]
    if not buf:
        return None
    arr = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_UNCHANGED)
    if arr is None:
        return None
    if arr.shape[:2] != (480, 640):
        arr = cv2.resize(arr, (640, 480), interpolation=cv2.INTER_NEAREST)
    d = np.clip(arr.astype(np.float32) / 4000.0 * 255.0, 0, 255).astype(np.uint8)
    return np.repeat(d[:, :, None], 3, axis=2)


def _build_state(state_dim, want_touch):
    with _lock:
        jq = list((_latest["lowstate"] or {}).get("joint_q") or [])
        lhq = list((_latest["lhand"] or {}).get("q") or [])
        rhq = list((_latest["rhand"] or {}).get("q") or [])
        lt = list((_latest["ltouch"] or {}).get("normal_force") or [])
        rt = list((_latest["rtouch"] or {}).get("normal_force") or [])
    jq = (jq + [0.0] * 29)[:29]
    rh = (rhq + [0.0] * 6)[:6]
    if _V2["on"]:
        parts, base = jq + rh, 35                          # v2: drop dead left_hand
    else:
        parts, base = jq + (lhq + [0.0] * 6)[:6] + rh, 41  # v1
    if want_touch and state_dim > base:
        if _V2["on"]:
            flat = list(rt)                       # v2: right-hand touch only (left dead)
        else:
            m = max(1, len(lt), len(rt))
            flat = (lt + [0.0] * m)[:m] + (rt + [0.0] * m)[:m]   # v1: both hands
        parts += (flat + [0.0] * (state_dim - base))[:state_dim - base]
    return np.asarray((parts + [0.0] * state_dim)[:state_dim], np.float32), jq


def _predict(sock, state, images):
    imeta = [{"key": k, "shape": list(a.shape)} for k, a in images]
    payload = state.astype(np.float32).tobytes()
    for _, a in images:
        payload += np.ascontiguousarray(a, np.uint8).tobytes()
    _send_frame(sock, {"cmd": "predict", "state_len": int(state.size), "images": imeta}, payload)
    hdr, pl = _recv_frame(sock)
    if not hdr.get("ok"):
        raise RuntimeError(hdr.get("error", "predict failed"))
    return np.frombuffer(pl, np.float32, hdr["action_len"]).copy()


# ── action(102) -> v3 message ─────────────────────────────────────────────────
_dq_sj = deque(maxlen=N)
_dq_bq = deque(maxlen=N)
_dq_jp = deque(maxlen=N)
_tick = {"k": 0}


def _norm_q(q):
    q = np.asarray(q, np.float32)
    n = float(np.linalg.norm(q))
    # degenerate (near-zero) -> identity, NEVER a zero/garbage orientation on the
    # wire (a component-wise quat blend of near-opposite quats can hit ~zero).
    return q / n if n > 1e-6 else np.array([1, 0, 0, 0], np.float32)


def _slerp(q1, q2, frac):
    """Shortest-arc spherical interp of two (w,x,y,z) quaternions; valid at all
    angles (no zero-magnitude pass-through that a 4-vector lerp can produce)."""
    q1 = np.asarray(q1, np.float32)
    q2 = np.asarray(q2, np.float32)
    d = float(np.dot(q1, q2))
    if d < 0.0:                       # take the shorter arc
        q2, d = -q2, -d
    d = min(1.0, max(-1.0, d))
    if d > 0.9995:                    # nearly aligned -> plain lerp + normalize
        return _norm_q((1.0 - frac) * q1 + frac * q2)
    th = float(np.arccos(d))
    return (np.sin((1.0 - frac) * th) * q1 + np.sin(frac * th) * q2) / np.sin(th)


def _interp(a, b, frac):
    """Blend two action vectors for the ramp/reset glide: linear on all channels,
    but SLERP the body_quat so a near-opposite quat blend can't pass through zero."""
    v = (1.0 - frac) * np.asarray(a, np.float32) + frac * np.asarray(b, np.float32)
    qs = slice(78, 82) if _V2["on"] else slice(93, 97)
    v[qs] = _slerp(np.asarray(a, np.float32)[qs], np.asarray(b, np.float32)[qs], frac)
    return v


def _split(a):
    if len(a) <= 90:   # v2 (86): smpl(72)+wrist(6)+body_quat(4)+grips(4)
        return {"vr_position": np.zeros(9, np.float32),
                "vr_orientation": np.zeros(12, np.float32),
                "smpl_joints": np.asarray(a[0:72], np.float32).reshape(24, 3),
                "wrist_jp": np.asarray(a[72:78], np.float64),
                "body_quat": _norm_q(a[78:82]), "heading": 0.0,
                "left_grip": float(a[82]), "right_grip": float(a[83]),
                "left_trigger": float(a[84]), "right_trigger": float(a[85])}
    # v1 (102): vr_position(9)+vr_orientation(12)+smpl(72)+body_quat(4)+heading(1)+grips(4)
    return {"vr_position": np.asarray(a[0:9], np.float32),
            "vr_orientation": np.asarray(a[9:21], np.float32),
            "smpl_joints": np.asarray(a[21:93], np.float32).reshape(24, 3),
            "wrist_jp": None,
            "body_quat": _norm_q(a[93:97]), "heading": float(a[97]),
            "left_grip": float(a[98]), "right_grip": float(a[99]),
            "left_trigger": float(a[100]), "right_trigger": float(a[101])}


def _stack(dq, fill):
    items = list(dq)
    if not items:
        items = [fill]
    while len(items) < N:
        items = [items[0]] + items
    return np.stack(items[-N:])


def _pack(pred, jq29):
    _dq_sj.append(pred["smpl_joints"].astype(np.float32))
    _dq_bq.append(pred["body_quat"].astype(np.float32))
    # joint_pos base = measured (hold); v2 drives the WRISTS from the prediction
    # (mode 2 reads joint_pos[WRIST_IDX] for grasp orientation — the fix).
    jp = np.asarray((jq29 + [0.0] * 29)[:29], np.float64)
    wj = pred.get("wrist_jp")
    if wj is not None:
        jp[WRIST_IDX] = np.asarray(wj, np.float64)
    _dq_jp.append(jp)
    sj = _stack(_dq_sj, np.zeros((24, 3), np.float32))
    bq = _stack(_dq_bq, np.array([1, 0, 0, 0], np.float32))
    jp = _stack(_dq_jp, np.zeros(29, np.float64))
    k = _tick["k"]
    fidx = np.array([k + i for i in range(N)], np.int64)
    _tick["k"] = k + 1
    nd = {
        "smpl_pose": np.zeros((N, 21, 3), np.float32),       # unused by policy
        "smpl_joints": sj.astype(np.float32),
        "body_quat_w": bq.astype(np.float32),
        "joint_pos": jp.astype(np.float64),                  # = measured (hold)
        "joint_vel": np.zeros((N, 29), np.float64),
        "frame_index": fidx,
        "vr_position": pred["vr_position"],
        "vr_orientation": pred["vr_orientation"],
        "left_trigger": np.array([pred["left_trigger"]], np.float32),
        "right_trigger": np.array([pred["right_trigger"]], np.float32),
        "left_grip": np.array([pred["left_grip"]], np.float32),
        "right_grip": np.array([pred["right_grip"]], np.float32),
        "left_hand_joints": np.zeros(7, np.float32),
        "right_hand_joints": np.zeros(7, np.float32),
        "heading_increment": np.array([pred["heading"]], np.float32),
        "toggle_data_collection": np.array([False]),
        "toggle_data_abort": np.array([False]),
    }
    return pack_pose_message(nd, topic="pose", version=3)


def _pack_vec(a, jq29, grip=None):
    """Pack a raw action vector (v1 102-d or v2 86-d; _split auto-detects). grip
    (0..1 or None) overrides all four grip/trigger channels — used to open the
    fingers after a round ends."""
    pred = _split(a)
    if grip is not None:
        pred["left_grip"] = pred["right_grip"] = float(grip)
        pred["left_trigger"] = pred["right_trigger"] = float(grip)
    return _pack(pred, jq29)


def _seed_vec(p0_vec, jq29):
    """SEED to ramp FROM at engage: p0_vec copied, with the directly-actuated
    channels pinned to the robot's CURRENT measured pose so frame 1 is ~a no-op
    and the motors GLIDE (not jump) to the policy's first pose. smpl_joints /
    body_quat pass through (policy-mediated; no FK from joint_q exists, and a
    fixed neutral would disagree with p0 → more motion, not less). v2: wrist_jp <-
    measured wrist q, grips OPEN. v1: only grips grounded (no wrist channel)."""
    seed = np.asarray(p0_vec, np.float32).copy()
    if _V2["on"]:                                  # 86-d: wrist_jp[72:78], grips[82:86]
        wq = np.asarray((list(jq29) + [0.0] * 29)[:29], np.float32)[WRIST_IDX]
        seed[72:78] = wq
        seed[82:86] = 0.0                          # fingers OPEN at engage
    elif seed.size >= 102:                         # v1 102-d: grips[98:102]
        seed[98:102] = 0.0
    return seed


# ── policy server lifecycle ───────────────────────────────────────────────────
def _spawn_policy():
    if not ACT_PY or not os.path.isfile(ACT_PY):
        _log(f"WBC_ACT_PY not set/found ({ACT_PY!r}) — cannot start policy server")
        return None
    p = subprocess.Popen([ACT_PY, POLICY_SCRIPT, CKPT, "--port", str(POLICY_PORT),
                          "--device", "cuda"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def relay():
        for ln in iter(p.stdout.readline, ""):
            print(f"[policy] {ln.rstrip()}", flush=True)
    threading.Thread(target=relay, daemon=True).start()
    return p


def _connect(timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _run["on"]:
        try:
            s = socket.create_connection(("127.0.0.1", POLICY_PORT), timeout=2.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return s
        except OSError:
            time.sleep(0.5)
    return None


def main():
    if not CKPT:
        _log("usage: act_infer.py <checkpoint_dir> <iface>")
        sys.exit(2)
    signal.signal(signal.SIGTERM, lambda *_: _run.__setitem__("on", False))
    signal.signal(signal.SIGINT, lambda *_: _run.__setitem__("on", False))
    threading.Thread(target=_stdin_thread, daemon=True).start()

    _log(f"starting policy server ({os.path.basename(CKPT)}) …")
    srv_proc = _spawn_policy()
    if srv_proc is None:
        sys.exit(2)
    sock = _connect()
    if sock is None:
        _log("policy server did not come up")
        srv_proc.terminate()
        sys.exit(2)
    # ping -> learn rgb/rgbd + state_dim + action layout (v1/v2) from the model
    _send_frame(sock, {"cmd": "ping"})
    info, _ = _recv_frame(sock)
    img_keys = info.get("images", ["observation.images.cam_high"])
    state_dim = int(info.get("state_dim", 41))
    action_dim = int(info.get("action_dim", 102))
    _V2["on"] = (action_dim <= 90)            # v2 (86) drives wrists + drops left_hand
    rgbd = any(k.endswith("_depth") for k in img_keys)
    want_touch = state_dim > (35 if _V2["on"] else 41)
    _log(f"model: layout={'v2' if _V2['on'] else 'v1'} action_dim={action_dim} "
         f"images={img_keys} state_dim={state_dim} rgbd={rgbd} touch={want_touch}")

    threading.Thread(target=_img_thread, args=(COLOR_PORT, "color"), daemon=True).start()
    if rgbd:
        threading.Thread(target=_img_thread, args=(DEPTH_PORT, "depth"), daemon=True).start()
    threading.Thread(target=_dds_threads, args=(want_touch,), daemon=True).start()
    threading.Thread(target=_cam_writer, daemon=True).start()   # live viewpoint -> GUI
    if rgbd:
        threading.Thread(target=_depth_writer, daemon=True).start()

    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://*:{POSE_PORT}")
    time.sleep(0.5)

    color_key = next((k for k in img_keys if not k.endswith("_depth")), "observation.images.cam_high")
    depth_key = next((k for k in img_keys if k.endswith("_depth")), None)

    def build_images():
        c = _get_color()
        if c is None:
            return None
        ims = [(color_key, c)]
        if rgbd:
            d = _get_depth()
            ims.append((depth_key, d if d is not None else np.zeros((480, 640, 3), np.uint8)))
        return ims

    # wait for first camera + proprio so the first prediction is real
    _log("waiting for camera + proprio …")
    t0 = time.monotonic()
    while _run["on"] and time.monotonic() - t0 < 20:
        if build_images() is not None and _latest["lowstate"]:
            break
        time.sleep(0.1)
    if not _run["on"]:
        return
    if build_images() is None:
        _log("no camera frames — check PC2 image_server / 55555")

    _send_frame(sock, {"cmd": "reset"})
    _recv_frame(sock)

    # ENGAGE + state machine. The FIRST prediction is the "initial pose": we hold
    # it (and glide back to it between rounds). States:
    #   INIT_HOLD  hold initial pose, wait for ② 执行
    #   RUN        policy drives in real time (tick rate scaled by speed)
    #   END_HOLD   round paused -> hold still; fingers open after release_s
    #   RESET      glide back to the initial pose, then auto-start the next round
    base_dt = 1.0 / max(5.0, RATE_HZ)
    st, jq = _build_state(state_dim, want_touch)
    ims = build_images() or [(color_key, np.zeros((480, 640, 3), np.uint8))]
    p0_vec = _predict(sock, st, ims)                 # the policy's first pose (frame1)
    last_vec = p0_vec.copy()
    reset_from = p0_vec.copy()
    ramp_from = _seed_vec(p0_vec, jq)                # SEED = p0 w/ wrist+grips at current
    reset_to = "RUN"                                 # where RESET lands (refresh -> INIT_HOLD)
    if RAMP_IN_S > 0:
        state = "RAMP_IN"                            # glide into frame1 first (motor-safe)
    else:
        state = "RUN" if AUTOPLAY else "INIT_HOLD"
    run_t0 = end_t0 = reset_t0 = ramp_t0 = time.monotonic()
    _log("engaging -> CONTROL + 柔顺斜坡进 frame1"
         + ("（autoplay）" if AUTOPLAY else "（斜坡完点「② 执行」）"))

    while _run["on"]:
        t = time.monotonic()
        sp = max(0.2, min(1.5, float(_ctl["speed"])))
        cmd, _ctl["cmd"] = _ctl["cmd"], None
        if cmd == "play" and state in ("INIT_HOLD", "END_HOLD"):
            state, run_t0 = "RUN", t
            _log("▶ 执行")
        elif cmd == "pause" and state == "RUN":
            state, end_t0 = "END_HOLD", t
            _log("⏸ 本轮保持（手指稍后松开）")
        elif cmd == "next":
            reset_from, reset_t0, state, reset_to = last_vec.copy(), t, "RESET", "RUN"
            _log("⟲ 回初始位…")
        elif cmd == "refresh" and state in ("INIT_HOLD", "END_HOLD"):
            # operator nudged the hand into frame -> re-predict a fresh frame1 and
            # glide the held pose to it (RESET machinery), landing in INIT_HOLD.
            try:
                _send_frame(sock, {"cmd": "reset"})
                _recv_frame(sock)
                ims_r = build_images() or [(color_key, np.zeros((480, 640, 3), np.uint8))]
                st_r, _ = _build_state(state_dim, want_touch)
                p0_vec = _predict(sock, st_r, ims_r)            # fresh frame1
                reset_from, reset_t0, state, reset_to = last_vec.copy(), t, "RESET", "INIT_HOLD"
                _log("🔄 刷新frame1：重新预测，平滑过渡到新初始姿势")
            except (ConnectionError, OSError, RuntimeError) as e:
                _log(f"refresh 预测失败: {e}")
        elif cmd == "refresh":
            _log("refresh 忽略：先 ⏸ 暂停或处于初始保持(INIT_HOLD)")

        st, jq = _build_state(state_dim, want_touch)
        grip = None
        if state == "RUN":
            ims = build_images()
            if ims is not None:
                try:
                    last_vec = _predict(sock, st, ims)
                    if FRAMESAVE:
                        _framesave(st, last_vec, ims[0][1])
                except (ConnectionError, OSError, RuntimeError) as e:
                    _log(f"predict error: {e}")
                    _run["on"] = False
                    break
            vec = last_vec
            if _ctl["loop"] and _ctl["round_s"] > 0 and (t - run_t0) >= _ctl["round_s"]:
                reset_from, reset_t0, state, reset_to = last_vec.copy(), t, "RESET", "RUN"
                _log("🔁 单轮到点 -> 回初始")
        elif state == "END_HOLD":
            vec = last_vec
            if (t - end_t0) >= max(0.0, float(_ctl["release_s"])):
                grip = 0.0                            # open the fingers
        elif state == "RESET":
            dur = min(20.0, max(0.3, RESET_BASE_S / sp))
            frac = (t - reset_t0) / dur
            if frac >= 1.0:
                _send_frame(sock, {"cmd": "reset"})   # fresh ACT action-chunk queue
                _recv_frame(sock)
                last_vec, state, run_t0, vec = p0_vec.copy(), reset_to, t, p0_vec
                _log("▶ 新一轮" if reset_to == "RUN" else "🔄 已刷新 -> 初始保持（点②执行）")
            else:
                vec = _interp(reset_from, p0_vec, frac)
        elif state == "RAMP_IN":
            dur = min(20.0, max(0.3, float(_ctl["ramp_in_s"]) / sp))
            frac = (t - ramp_t0) / dur
            if frac >= 1.0:
                state = "RUN" if AUTOPLAY else "INIT_HOLD"
                run_t0, vec = t, p0_vec
                _log("斜坡完成 -> " + ("▶ 执行(autoplay)" if AUTOPLAY else "初始保持（目视站稳后点②执行）"))
            else:
                vec = _interp(ramp_from, p0_vec, frac)           # glide SEED -> frame1
        else:  # INIT_HOLD
            vec = p0_vec

        pub.send(build_command_message(start=True, stop=False, planner=False))
        pub.send(_pack_vec(vec, jq, grip))
        time.sleep(max(0.0, base_dt / sp - (time.monotonic() - t)))

    _log("stopping — deploy holds last command (E-stop with 'o' to damp)")
    try:
        sock.close()
    except OSError:
        pass
    if srv_proc and srv_proc.poll() is None:
        srv_proc.terminate()
        try:
            srv_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv_proc.kill()
    pub.close()
    _log("infer stopped")


if __name__ == "__main__":
    main()
