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
import os
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

# ── safe posture ladder (real-robot safety) ──────────────────────────────────
# SONIC needs a PREPARATORY descent before prone/crawl — jumping straight from
# standing to crawl can slam/damage the robot. The official PICO flow steps modes
# one-by-one (A+B = mode+1) which naturally walks this descent. Manual drive used
# to set the target mode DIRECTLY (a dangerous jump); now it STEPS through the
# ladder, dwelling at each posture so the policy lands it before descending the
# next. Rising reverses it. Movement + turning are LOCKED while transitioning.
# Ladder + dwell operator-confirmed 2026-06-19: 站→蹲→跪(两腿)→趴→爬→肘爬, 4–5 s/step.
POSTURE_LADDER = [0, 4, 5, 7, 8, 14]   # Idle/Stand, Squat, KneelTwoLegs, Lying, Crawl, ElbowCrawl
LADDER_NAMES = {0: "站Idle", 4: "蹲Squat", 5: "跪Kneel2", 7: "趴Lying", 8: "爬Crawl", 14: "肘爬Elbow"}
MARK_NAMES = {0: "Idle", 1: "SlowWalk", 2: "Walk", 3: "Run", 4: "Squat",
              5: "Kneel", 7: "Lying", 8: "Crawling", 14: "ElbowCrawling"}
DWELL_S = float(os.environ.get("DRIVE_POSTURE_DWELL_S", "4.5"))   # dwell per ladder step
MOVING_MODES = {1, 2, 3, 8, 14}        # can take movement; postures (4/5/7) are static


def _ladder_level(mode):
    return POSTURE_LADDER.index(mode) if mode in POSTURE_LADDER else 0

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
    cur_level = 0                       # published ladder level (start standing)
    last_step_t = time.monotonic()
    last_pub_mode = None
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

        # ── SAFE POSTURE SEQUENCING ──────────────────────────────────────────
        # Walk the ladder one step toward the requested pose, dwelling DWELL_S at
        # each so SONIC lands each posture before descending/rising the next.
        # Until settled, publish the intermediate posture and LOCK movement+turn.
        tgt_level = _ladder_level(gait)
        now = time.monotonic()
        if cur_level != tgt_level and (now - last_step_t) >= DWELL_S:
            cur_level += 1 if tgt_level > cur_level else -1
            last_step_t = now
        settled = (cur_level == tgt_level)
        left, forward = strafe, fwd
        mag = math.hypot(left, forward)
        moving_input = mag >= DEADZONE
        # mode resolution (release = stop, but DON'T pop a ground posture up to
        # IDLE — that would trigger a rise). So: standing gait + no input -> IDLE
        # (stand still); a ground posture -> stay in that posture (in place);
        # while transitioning -> the intermediate ladder posture.
        if not settled:
            mode = POSTURE_LADDER[cur_level]
        elif gait in (1, 2, 3) and not moving_input:
            mode = IDLE
        else:
            mode = gait

        # facing rotates only when settled; movement only when settled, the mode
        # can move, and there's input — never walk/turn mid-transition.
        if settled:
            yaw += turn * YAW_GAIN * dt
        facing = [math.cos(yaw), math.sin(yaw), 0.0]
        if settled and mode in MOVING_MODES and moving_input:
            perp_x, perp_y = -facing[1], facing[0]
            movement = [perp_x * left + facing[0] * forward,
                        perp_y * left + facing[1] * forward, 0.0]
        else:
            movement = [0.0, 0.0, 0.0]

        s.send(build_planner_message(mode, movement, facing, speed=speed, height=-1.0))

        # surface posture changes (log + loco-mode marker so the GUI pill follows)
        if mode != last_pub_mode:
            note = "到位" if settled else f"过渡中 → 目标 {LADDER_NAMES.get(gait, gait)}"
            print(f"[gui-drive] 姿势 -> {LADDER_NAMES.get(mode, mode)}  ({note})", flush=True)
            print(f"[PlannerLoop] Mode -> {mode}: {MARK_NAMES.get(mode, 'Mode' + str(mode))}", flush=True)
            last_pub_mode = mode

        dbg += 1
        if dbg % 40 == 0:
            print(f"[gui-drive] mode={mode} settled={settled} "
                  f"move=({movement[0]:.2f},{movement[1]:.2f}) yaw={math.degrees(yaw):.0f}deg", flush=True)
        time.sleep(dt)


if __name__ == "__main__":
    main()
