#!/usr/bin/env python3
"""Real BrainCo Revo2 hand bridge for the PICO-WBC pipeline.

The gear_sonic deploy controls the BODY (rt/lowcmd) but publishes hand commands
to rt/dex3, which the real BrainCo hands DON'T listen to (they're on rt/brainco).
So this bridge closes the loop for the hands:

  streamer ZMQ (5556, "pose") -> left/right_hand_joints (Dex3 gripper-IK, the
  trigger-driven grasp, symmetric) -> per-hand "closedness" in [0,1] -> curl the
  6 BrainCo joints proportionally -> publish MotorCmds_ to rt/brainco/{l,r}/cmd.

Safety / overload protection (position-servo hand: motor current ~ position
error, so clamping the error clamps the current):
  * STALL CLAMP — per finger, subscribe rt/brainco state and compare measured q
    to the commanded q. If a finger lags the command by > STALL_GAP rad it is
    blocked (object / self-contact): clamp that finger's command to
    measured + STALL_MARGIN instead of pushing toward the far target. Adaptive:
    when the obstruction goes away the measurement catches up and the command
    resumes tracking. Opening is never clamped.
  * SLEW LIMIT — commanded closedness changes at most MAX_RATE per second.
  * MAX_CLOSE — hard cap on closedness (env WBC_HAND_MAX_CLOSE, default 1.0).
  * STALENESS — no pose for STALE_S (streamer stopped / E-stop / PLANNER mode):
    ramp OPEN gently at STALE_OPEN_RATE instead of freezing clenched. The GUI
    E-stop SIGINTs the streamer but leaves this bridge running on purpose —
    the stale path is what opens the hands.

NOTE exactly ONE bridge instance may run: two publishers on rt/brainco/*/cmd
fight each other (verified: a stale bridge holding "open" cancels test grasps).

Requires: the PC2 brainco_hand_server running, and the streamer publishing pose.
Usage:  brainco_hand_bridge.py <robot_iface>   e.g. enp132s0
"""
import json
import os
import signal
import struct
import sys
import threading
import time

import zmq

from unitree_sdk2py.core.channel import (
    ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber,
)
from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorCmds_, MotorStates_
from unitree_sdk2py.idl.default import unitree_go_msg_dds__MotorCmd_

NUM = 6
# BrainCo joint upper limits (rad), open=0 .. closed=upper. Order matches the
# DexPilot target_joint_names: thumb_metacarpal, thumb_proximal, index, middle,
# ring, pinky proximal (from brainco_left.urdf <limit> tags). State topic q is
# in the SAME units (verified: free-space state matches command exactly).
UPPER = [1.5184, 1.0472, 1.4661, 1.4661, 1.4661, 1.4661]
CLOSE_NORM = 0.9          # mean(|streamer 7-DOF joints|) at full grasp ~0.91
RATE_HZ = 100.0
MAX_RATE = 2.5            # closedness units/s while tracking (full close ~0.4s)
STALE_S = 1.0             # no pose for this long -> input considered lost
STALE_OPEN_RATE = 0.3     # closedness units/s auto-open when stale (~3s gentle)
STALL_GAP = 0.22          # rad: measured lags command by more -> finger blocked
STALL_MARGIN = 0.12       # rad: how far beyond the measured q we keep pushing
STATE_FRESH_S = 0.5       # state older than this -> no clamp (warn once)
MAX_CLOSE = float(os.environ.get("WBC_HAND_MAX_CLOSE", "1.0"))
HDR = 1280
SZ = {"f32": 4, "f64": 8, "i32": 4, "i64": 8, "bool": 1}

_hands = {"L": None, "R": None}
# Per-hand analog controller axes from the pose message:
#   trigger (index-finger axis) -> the four FINGERS
#   grip    (middle-finger side axis) -> the THUMB
# Both analog 0..1 -> pinch (grip only), hook (trigger only), full grasp (both).
_axes = {"L": {"trigger": 0.0, "grip": 0.0}, "R": {"trigger": 0.0, "grip": 0.0}}
_have_axes = {"v": False}   # pose stream carries trigger/grip fields
_last_rx = {"t": 0.0}
_state = {"L": None, "R": None}          # measured q[6] (rad)
_state_rx = {"L": 0.0, "R": 0.0}
_running = {"on": True}

_AXIS_FIELDS = {"left_trigger": ("L", "trigger"), "right_trigger": ("R", "trigger"),
                "left_grip": ("L", "grip"), "right_grip": ("R", "grip")}
AXIS_DEADZONE = 0.05


def _parse_pose(msg):
    if msg[:4] != b"pose":
        return
    body = msg[4:]
    try:
        h = json.loads(body[:HDR].rstrip(b"\x00"))
    except Exception:
        return
    off, pay = 0, body[HDR:]
    found = False
    for f in h.get("fields", []):
        n = 1
        for d in (f.get("shape") or [1]):
            n *= int(d)
        sz = SZ.get(f["dtype"], 4)
        name = f["name"]
        if name in ("left_hand_joints", "right_hand_joints") and off + sz * n <= len(pay):
            vals = struct.unpack("<" + "f" * n, pay[off:off + sz * n])
            _hands["L" if name[0] == "l" else "R"] = list(vals)
            found = True
        elif name in _AXIS_FIELDS and off + sz * n <= len(pay):
            v = struct.unpack("<f", pay[off:off + 4])[0]
            side, axis = _AXIS_FIELDS[name]
            _axes[side][axis] = 0.0 if v < AXIS_DEADZONE else min(1.0, float(v))
            _have_axes["v"] = True
            found = True
        off += sz * n
    if found:
        _last_rx["t"] = time.monotonic()


def _zmq_reader():
    ctx = zmq.Context()
    s = ctx.socket(zmq.SUB)
    s.connect("tcp://localhost:5556")
    s.setsockopt(zmq.SUBSCRIBE, b"pose")
    s.setsockopt(zmq.RCVTIMEO, 1000)
    while _running["on"]:
        try:
            _parse_pose(s.recv())
        except zmq.Again:
            continue
        except Exception:
            time.sleep(0.1)


META_I, PROX_I = 0, 1      # thumb_metacarpal (sweep/opposition), thumb_proximal (curl)
FINGER_IDX = (2, 3, 4, 5)  # index/middle/ring/pinky proximal

# Thumb has TWO independent DOF, each on its own controller input:
#   grip      -> thumb_proximal  (curl)        — squeeze to curl the thumb
#   stick Y   -> thumb_metacarpal (opposition)  — rate-controlled, HOLDS on release
# They are decoupled: META_FOLLOW=0 means metacarpal does NOT follow grip, it is
# driven purely by stick Y. (Set WBC_THUMB_META_FOLLOW>0 to re-couple: metacarpal
# follows grip ×factor + stick trim, the old combined-close behaviour.)
# NOTE right-hand thumb_metacarpal motor is mechanically broken — independent
# opposition only physically moves on the LEFT hand. Y is free in POSE mode
# (yaw steering uses right-stick X).
META_FOLLOW = float(os.environ.get("WBC_THUMB_META_FOLLOW", "0.0"))
STICK_RATE = 0.8           # offset units/s at full stick deflection
STICK_DEADZONE = 0.15
STICK_SIGN = -1.0 if os.environ.get("WBC_THUMB_STICK_INVERT") == "1" else 1.0
USE_STICK = os.environ.get("WBC_THUMB_STICK", "1") == "1"

_xrt = None
if USE_STICK:
    try:
        import xrobotoolkit_sdk as _xrt_mod
        _xrt_mod.init()
        _xrt = _xrt_mod
    except Exception as e:
        print(f"[brainco-bridge] xrt unavailable ({e}) -> thumb metacarpal follows grip", flush=True)


def _stick_y(side):
    """Same-side thumbstick Y in [-1,1], deadzoned. 0 when xrt is unavailable."""
    if _xrt is None:
        return 0.0
    try:
        ax = _xrt.get_left_axis() if side == "L" else _xrt.get_right_axis()
        y = float(ax[1]) if len(ax) >= 2 else 0.0
    except Exception:
        return 0.0
    return STICK_SIGN * y if abs(y) >= STICK_DEADZONE else 0.0


def _targets(side):
    """Absolute targets (thumb_proximal, fingers) in closedness [0,1].

    Preferred: analog controller axes (grip -> thumb curl, trigger -> fingers).
    Fallback (old streams without axis fields): collapse hand_joints to one
    closedness and drive both groups with it.
    """
    if _have_axes["v"]:
        a = _axes[side]
        return min(a["grip"], MAX_CLOSE), min(a["trigger"], MAX_CLOSE)
    j = _hands.get(side)
    if not j:
        return 0.0, 0.0
    c = float(min(1.0, max(0.0, (sum(abs(x) for x in j) / len(j)) / CLOSE_NORM)))
    c = min(c, MAX_CLOSE)
    return c, c


def _on_state(side):
    def cb(m):
        try:
            _state[side] = [m.states[i].q for i in range(min(NUM, len(m.states)))]
            _state_rx[side] = time.monotonic()
        except Exception:
            pass
    return cb


def main():
    iface = sys.argv[1] if len(sys.argv) > 1 else "enp132s0"
    ChannelFactoryInitialize(0, iface)
    pub = {"L": ChannelPublisher("rt/brainco/left/cmd", MotorCmds_),
           "R": ChannelPublisher("rt/brainco/right/cmd", MotorCmds_)}
    for p in pub.values():
        p.Init()
    sub = {"L": ChannelSubscriber("rt/brainco/left/state", MotorStates_),
           "R": ChannelSubscriber("rt/brainco/right/state", MotorStates_)}
    for side, s in sub.items():
        s.Init(_on_state(side), 10)

    def make_msg():
        m = MotorCmds_()
        m.cmds = [unitree_go_msg_dds__MotorCmd_() for _ in range(NUM)]
        for i in range(NUM):
            m.cmds[i].dq = 1.0
        return m

    msg = {"L": make_msg(), "R": make_msg()}
    # commanded closedness per group (slewed): [thumb_meta, thumb_prox, fingers]
    cmd = {"L": [0.0, 0.0, 0.0], "R": [0.0, 0.0, 0.0]}
    # stick-held metacarpal trim offset and last raw stick Y (for L/R debugging)
    meta_off = {"L": 0.0, "R": 0.0}
    stick_raw = {"L": 0.0, "R": 0.0}
    signal.signal(signal.SIGTERM, lambda *_: _running.__setitem__("on", False))
    threading.Thread(target=_zmq_reader, daemon=True).start()
    _thumb_mode = (f"grip->prox + stickY->meta INDEPENDENT({'ON' if _xrt else 'stick OFF!'})"
                   if META_FOLLOW == 0.0 else
                   f"grip->thumb(meta×{META_FOLLOW}+prox)+stickY trim({'ON' if _xrt else 'OFF'})")
    print(f"[brainco-bridge] running (iface={iface}) | {_thumb_mode} trigger->fingers | "
          f"slew {MAX_RATE}/s | max_close {MAX_CLOSE} | "
          f"stall clamp gap {STALL_GAP}rad margin {STALL_MARGIN}rad | "
          f"stale>{STALE_S}s -> open {STALE_OPEN_RATE}/s", flush=True)

    dt = 1.0 / RATE_HZ
    dbg, was_stale = 0, False
    warned_nofb = {"L": False, "R": False}
    stalled_n = {"L": 0, "R": 0}
    try:
        while _running["on"]:
            now = time.monotonic()
            stale = (now - _last_rx["t"]) > STALE_S
            if stale != was_stale:
                print(f"[brainco-bridge] {'input STALE -> auto-opening hands' if stale else 'input live'}"
                      f"{' | axes mode' if _have_axes['v'] else ''}", flush=True)
                was_stale = stale
            for side in ("L", "R"):
                if stale:
                    for g in (0, 1, 2):
                        cmd[side][g] = max(0.0, cmd[side][g] - STALE_OPEN_RATE * dt)
                    # decay the stick trim too so a fresh session starts neutral
                    o = meta_off[side]
                    meta_off[side] = max(0.0, o - STALE_OPEN_RATE * dt) if o > 0 \
                        else min(0.0, o + STALE_OPEN_RATE * dt)
                else:
                    prox_tgt, fing_tgt = _targets(side)
                    # stick Y trims a held metacarpal offset on top of the
                    # grip-following base (symmetric L/R when stick untouched)
                    y = _stick_y(side)
                    stick_raw[side] = y
                    # stick Y integrates the metacarpal opposition target in [0,1]
                    # and HOLDS on release. Decoupled from grip (META_FOLLOW=0).
                    meta_off[side] = min(1.0, max(0.0, meta_off[side] + STICK_RATE * y * dt))
                    meta_tgt = min(MAX_CLOSE, max(0.0, prox_tgt * META_FOLLOW + meta_off[side]))
                    step = MAX_RATE * dt
                    cmd[side][0] += min(step, max(-step, meta_tgt - cmd[side][0]))
                    cmd[side][1] += min(step, max(-step, prox_tgt - cmd[side][1]))
                    cmd[side][2] += min(step, max(-step, fing_tgt - cmd[side][2]))

                meas = _state[side]
                fresh = (now - _state_rx[side]) < STATE_FRESH_S
                if not fresh and not warned_nofb[side] and max(cmd[side]) > 0.05:
                    print(f"[brainco-bridge] WARN {side}: no fresh hand state -> "
                          f"stall clamp inactive (slew/max_close only)", flush=True)
                    warned_nofb[side] = True
                if fresh:
                    warned_nofb[side] = False

                stalled = 0
                for i in range(NUM):
                    if i == META_I:
                        c = cmd[side][0]
                    elif i == PROX_I:
                        c = cmd[side][1]
                    else:
                        c = cmd[side][2]
                    q_des = c * UPPER[i]
                    if fresh and meas and i < len(meas) and q_des > meas[i] + STALL_GAP:
                        # finger blocked: cap the push (current ~ position error)
                        q_des = meas[i] + STALL_MARGIN
                        stalled += 1
                    msg[side].cmds[i].q = q_des
                pub[side].Write(msg[side])
                stalled_n[side] = stalled
            dbg += 1
            if dbg % 200 == 0:
                extra = "".join(f" {s}:stall×{stalled_n[s]}" for s in ("L", "R") if stalled_n[s])
                print(f"[brainco-bridge] meta/prox/fing L={cmd['L'][0]:.2f}/{cmd['L'][1]:.2f}/{cmd['L'][2]:.2f} "
                      f"R={cmd['R'][0]:.2f}/{cmd['R'][1]:.2f}/{cmd['R'][2]:.2f} | "
                      f"stickY L={stick_raw['L']:+.2f} R={stick_raw['R']:+.2f} "
                      f"off L={meta_off['L']:+.2f} R={meta_off['R']:+.2f}"
                      f"{' (stale)' if stale else ''}{extra}", flush=True)
            time.sleep(dt)
    except KeyboardInterrupt:
        pass
    print("[brainco-bridge] exiting (last command holds on the hand server)", flush=True)
    if _xrt is not None:
        os._exit(0)  # xrt destructor segfaults on teardown (known); skip it


if __name__ == "__main__":
    main()
