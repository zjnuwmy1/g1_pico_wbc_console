#!/usr/bin/env python3
"""GUI manual-drive publisher — control the G1 from the console WITHOUT a PICO.

Replaces the PICO streamer on the deploy's input ZMQ (binds PUB on 5556) and
sends the same 'command' + 'planner' messages the streamer would, but driven by
console buttons instead of a headset. This is the PLANNER ("行走规划") path: a
gait (LocomotionMode) + a movement vector + a facing heading — no body tracking
needed, so the robot can walk / turn / change gait/pose from the GUI alone.

Intent arrives as one JSON object per stdin line (the console writes it):
    {"policy": "start"|"stop",   # one-shot: engage / stop the policy
     "gait": <int LocomotionMode>,
     "fwd": -1..1, "strafe": -1..1,   # body-frame move (X-forward, Y-left)
     "turn": -1..1,               # yaw rate (left +)
     "speed": <float or -1>}      # -1 = gait default
Latest values are held; planner messages stream at 20 Hz.

⚠️ Mutually exclusive with pico_streamer (both bind 5556). Deploy (real/sim)
must be running and subscribed. E-stop still goes through the deploy ('o').

Usage:  gui_drive.py
"""
import json
import math
import sys
import threading
import time

import zmq

from gear_sonic.utils.teleop.zmq.zmq_planner_sender import (
    build_command_message, build_planner_message,
)

PORT = 5556
RATE_HZ = 20.0
YAW_GAIN = 1.5            # rad/s at full turn
DEADZONE = 0.05
IDLE = 0                 # LocomotionMode.IDLE

_intent = {"gait": 2, "fwd": 0.0, "strafe": 0.0, "turn": 0.0, "speed": -1.0}
_policy_cmd = {"v": None}   # one-shot "start"/"stop"
_lock = threading.Lock()


def _stdin_reader():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        with _lock:
            if "policy" in d:
                _policy_cmd["v"] = d["policy"]
            for k in ("gait", "fwd", "strafe", "turn", "speed"):
                if k in d:
                    _intent[k] = d[k]


def main():
    ctx = zmq.Context()
    s = ctx.socket(zmq.PUB)
    s.bind(f"tcp://*:{PORT}")
    time.sleep(0.2)
    # prime the deploy: not started, planner idle facing +X
    s.send(build_command_message(start=False, stop=False, planner=False))
    s.send(build_planner_message(IDLE, [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], -1.0, -1.0))
    threading.Thread(target=_stdin_reader, daemon=True).start()
    print("[gui-drive] PUB bound on 5556 (PLANNER mode). Deploy must be running. "
          "Awaiting console intent; E-stop still via deploy 'o'.", flush=True)

    dt = 1.0 / RATE_HZ
    yaw = 0.0
    dbg = 0
    while True:
        with _lock:
            pc = _policy_cmd["v"]
            _policy_cmd["v"] = None
            gait = int(_intent["gait"])
            fwd = float(_intent["fwd"])
            strafe = float(_intent["strafe"])
            turn = float(_intent["turn"])
            speed = float(_intent["speed"])

        if pc == "start":
            s.send(build_command_message(start=True, stop=False, planner=True))
            print("[gui-drive] policy START (planner)", flush=True)
        elif pc == "stop":
            s.send(build_command_message(start=False, stop=True, planner=True))
            print("[gui-drive] policy STOP", flush=True)

        # facing heading from turn rate (turn=+1 -> CCW/left: yaw increases, since
        # facing=[cos,sin] and robot +Y is left). Hold to rotate, keep on release.
        yaw += turn * YAW_GAIN * dt
        facing = [math.cos(yaw), math.sin(yaw), 0.0]

        # body-frame [left, forward] -> world via facing (mirror of streamer math)
        left, forward = strafe, fwd
        mag = math.hypot(left, forward)
        if mag < DEADZONE:
            mode, movement = IDLE, [0.0, 0.0, 0.0]
        else:
            perp_x, perp_y = -facing[1], facing[0]
            mg0 = perp_x * left + facing[0] * forward
            mg1 = perp_y * left + facing[1] * forward
            mode, movement = gait, [mg0, mg1, 0.0]

        s.send(build_planner_message(mode, movement, facing, speed=speed, height=-1.0))
        dbg += 1
        if dbg % 40 == 0:
            print(f"[gui-drive] gait={gait} mode={mode} move=({movement[0]:.2f},{movement[1]:.2f}) "
                  f"yaw={math.degrees(yaw):.0f}deg", flush=True)
        time.sleep(dt)


if __name__ == "__main__":
    main()
