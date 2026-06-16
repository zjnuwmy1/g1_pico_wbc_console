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

Compares rt/lowcmd vs rt/lowstate on load-bearing joints. Prints a JSON verdict.

Usage: verify_motor_handover.py [iface]
"""
import json
import sys
import time

IFACE = sys.argv[1] if len(sys.argv) > 1 else "enp132s0"
THRESH = 0.2  # rad; following gives ~0.02-0.11, not-following gives ~0.3-0.76
JOINTS = [(0, "左髋pitch"), (3, "左膝"), (9, "右膝"), (12, "腰yaw"),
          (15, "左肩pitch"), (18, "左肘")]


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

rows, bad = [], 0
for i, name in JOINTS:
    cq = st["cmd"].motor_cmd[i].q
    aq = st["state"].motor_state[i].q
    kp = st["cmd"].motor_cmd[i].kp
    d = cq - aq
    following = abs(d) <= THRESH or kp <= 0
    if not following:
        bad += 1
    rows.append({"joint": name, "cmd": round(cq, 3), "actual": round(aq, 3),
                 "kp": round(kp), "diff": round(d, 3), "ok": following})

following = bad == 0
_out({
    "ok": True, "following": following, "bad": bad, "joints": rows,
    "verdict": ("✓ 电机已交权，机器人在跟随 lowcmd —— Init Done 是真的" if following
                else f"✗ {bad} 个承重关节没跟上 —— 电机没交权，机器人在无视 deploy。"
                     "去手柄进 Unitree 调试模式（L2+R2，原生运控脱离）后重起 deploy。"),
})
