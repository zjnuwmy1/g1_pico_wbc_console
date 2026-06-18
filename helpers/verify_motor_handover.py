#!/usr/bin/env python3
"""Verify the robot's motors actually EXECUTE rt/lowcmd (motor authority handed over).

The hard lesson: releasing the high-level mode (MotionSwitcher, software) is NOT
enough — the robot only executes rt/lowcmd after the operator enters Unitree's
controller debug mode (L2+R2, native control detached). Until then the robot
holds its own pose and ignores the deploy, so "Init Done" is hollow: the deploy
ran its init ramp but the robot never physically moved.

This is detectable: after Init Done the deploy commands the default standing pose
with real kp. If motors are under lowcmd, the ACTUAL joint q tracks the COMMANDED
q (small diff). If not, actual diverges from cmd despite kp>0.

CAVEAT (learned the hard way): this snapshot can give a FALSE POSITIVE. The
robot's idle standing pose ~ the deploy's default standing pose, so the LEG/waist
joints can match by COINCIDENCE even when the robot isn't executing lowcmd. The
ARM is the real discriminator — the robot's idle arm pose differs from the
deploy's commanded arm, so a TRACKING arm = real handover, while an off arm =
either not-executing (robot sits in its own pose) OR the arm is obstructed. So a
confident "✓ handed over" needs the ARM to track too, not just the legs. The
ground truth is motion: if the robot doesn't physically react to the deploy
(playback doesn't move it), it isn't executing lowcmd regardless of this snapshot.

Compares rt/lowcmd vs rt/lowstate. Prints a JSON verdict.

Usage: verify_motor_handover.py [iface]
"""
import json
import sys
import time

IFACE = sys.argv[1] if len(sys.argv) > 1 else "enp132s0"
THRESH = 0.2  # rad; following gives ~0.02-0.11, not-following gives ~0.3-0.76
# Handover is judged ONLY by the load-bearing CORE joints (legs + waist, high kp):
# if those track the deploy's commanded default, the robot is executing lowcmd =
# handover succeeded. ARM joints have low kp (~14) and are soft — a free or
# resting arm can sit far from its target WITHOUT meaning handover failed, so they
# are informational only (a big arm diff = arm obstructed / resting, not no-auth).
JOINTS = [(0, "左髋pitch", "core"), (3, "左膝", "core"), (9, "右膝", "core"),
          (12, "腰yaw", "core"), (15, "左肩pitch", "arm"), (18, "左肘", "arm")]


def _out(d):
    print(json.dumps(d, ensure_ascii=False))
    sys.exit(0)


try:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_, LowCmd_
except Exception as e:
    _out({"ok": False, "error": f"unitree_sdk2py 导入失败: {e}"})

try:
    ChannelFactoryInitialize(0, IFACE)
except Exception as e:
    _out({"ok": False, "error": f"DDS 初始化失败({IFACE}): {e}"})

st = {"cmd": None, "state": None, "nc": 0, "ns": 0}
ChannelSubscriber("rt/lowcmd", LowCmd_).Init(
    lambda m: (st.update(cmd=m), st.update(nc=st["nc"] + 1)), 10)
ChannelSubscriber("rt/lowstate", LowState_).Init(
    lambda m: (st.update(state=m), st.update(ns=st["ns"] + 1)), 10)

t = time.time()
while time.time() - t < 5 and not (st["cmd"] and st["state"]):
    time.sleep(0.1)
time.sleep(0.4)  # let a couple fresh frames land

if not st["cmd"]:
    _out({"ok": False, "error": "没收到 rt/lowcmd —— deploy 在跑吗?",
          "lowcmd": st["nc"], "lowstate": st["ns"]})
if not st["state"]:
    _out({"ok": False, "error": "没收到 rt/lowstate —— 机器人在线吗?"})

rows, bad_core, sag_arm = [], 0, 0
for i, name, cat in JOINTS:
    cq = st["cmd"].motor_cmd[i].q
    aq = st["state"].motor_state[i].q
    kp = st["cmd"].motor_cmd[i].kp
    d = cq - aq
    ok = abs(d) <= THRESH or kp <= 0
    if not ok:
        if cat == "core":
            bad_core += 1     # legs/waist off -> real handover failure
        else:
            sag_arm += 1      # arm off -> soft/obstructed, NOT a handover failure
    rows.append({"joint": name, "cmd": round(cq, 3), "actual": round(aq, 3),
                 "kp": round(kp), "diff": round(d, 3), "ok": ok, "cat": cat})

# A confident YES needs ALL joints to track. The ARM is the key discriminator:
# the robot's IDLE standing pose ~ the deploy's default standing pose, so the
# legs can match by COINCIDENCE without the robot executing lowcmd. The arm is
# where the robot's idle pose differs from the deploy's command, so a tracking
# arm = real handover; an off arm = either NOT executing (robot sits in its own
# pose) OR the arm is physically obstructed. Don't claim handover unless the arm
# tracks too.
handover = (bad_core == 0 and sag_arm == 0)
if bad_core:
    verdict = (f"✗ {bad_core} 个承重关节（腿/腰）没跟上 —— 电机没交权，机器人在无视 deploy。"
               "去手柄进 Unitree 调试模式（L2+R2，原生运控脱离）后重起 deploy。")
elif sag_arm:
    verdict = (f"⚠ 腿/腰数值匹配，但 {sag_arm} 个手臂关节没跟上（停在机器人自己的姿势、≠deploy命令）。"
               "腿匹配可能只是巧合（机器人闲置站姿≈deploy默认站姿），手臂才暴露真相。两种可能："
               "(a) **根本没交权** —— 若机器人对 deploy 完全没反应、回放不动，就是这个，去手柄 "
               "L2+R2 调试模式后重起 deploy；(b) 手臂被挡住/搭在东西上。先确认手臂自由，再看机器人动不动。")
else:
    verdict = "✓ 电机已交权（含手臂都跟上，可靠）—— Init Done 是真的，机器人在执行 lowcmd"

_out({
    "ok": True, "following": handover, "bad": bad_core, "arm_off": sag_arm,
    "joints": rows, "verdict": verdict,
})
