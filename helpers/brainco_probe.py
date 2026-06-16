#!/usr/bin/env python3
"""Quick health check: is the PC2 BrainCo hand server actually publishing
rt/brainco state? Prints one JSON line. Used at teleop startup to decide whether
the hand server needs an auto-restart (its DDS match can go bad — see memory).

Usage: brainco_probe.py [iface]
"""
import json
import sys
import time

IFACE = sys.argv[1] if len(sys.argv) > 1 else "enp132s0"


def out(d):
    print(json.dumps(d, ensure_ascii=False))
    sys.exit(0)


try:
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorStates_
except Exception as e:
    out({"ok": False, "error": f"unitree_sdk2py 导入失败: {e}"})

try:
    ChannelFactoryInitialize(0, IFACE)
except Exception as e:
    out({"ok": False, "error": f"DDS 初始化失败({IFACE}): {e}"})

c = {"l": 0, "r": 0}
ChannelSubscriber("rt/brainco/left/state", MotorStates_).Init(lambda m: c.__setitem__("l", c["l"] + 1), 10)
ChannelSubscriber("rt/brainco/right/state", MotorStates_).Init(lambda m: c.__setitem__("r", c["r"] + 1), 10)
time.sleep(1.0)   # warm up DDS discovery
t = time.time()
while time.time() - t < 3 and (c["l"] + c["r"]) == 0:
    time.sleep(0.1)
out({"ok": (c["l"] + c["r"]) > 0, "left": c["l"], "right": c["r"]})
