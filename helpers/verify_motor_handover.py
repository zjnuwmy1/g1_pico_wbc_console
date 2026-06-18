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

Handover is judged ONLY by the CORE load-bearing joints (legs + waist, high kp) —
those tracking = the robot executes lowcmd = handover happened. The ARM joints
(low kp ~14) are SOFT: a free/resting arm sits far from target without meaning no
handover, so a big arm diff is reported as "arm obstructed/soft", not "no auth".

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

# handover is decided by the CORE (load-bearing) joints only
handover = bad_core == 0
if not handover:
    verdict = (f"✗ {bad_core} 个承重关节（腿/腰）没跟上 —— 电机没交权，机器人在无视 deploy。"
               "去手柄进 Unitree 调试模式（L2+R2，原生运控脱离）后重起 deploy。")
elif sag_arm:
    verdict = (f"✓ 电机已交权（承重关节腿/腰都跟上，Init Done 是真的）。"
               f"但 {sag_arm} 个手臂关节没到位（kp 低、软）—— 多半是手臂搭在东西上/被挡住"
               "（左肘比命令更弯=不是重力下垂）。检查手臂周围、必要时扳直再看，"
               "不影响交权，可继续。")
else:
    verdict = "✓ 电机已交权，机器人在跟随 lowcmd —— Init Done 是真的"

_out({
    "ok": True, "following": handover, "bad": bad_core, "arm_sag": sag_arm,
    "joints": rows, "verdict": verdict,
})
