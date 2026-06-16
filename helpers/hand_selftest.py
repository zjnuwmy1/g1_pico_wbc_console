#!/usr/bin/env python3
"""Hand self-test — drive both BrainCo hands through each motor group and verify
the encoder actually follows. Catches a dead motor / unplugged hand / serial
issue / dead server, WITHOUT needing PICO.

⚠️ MOVES THE HANDS (~12 s). Must be the SOLE publisher on rt/brainco/*/cmd, so
the caller (server) stops the hand bridge first and restarts it after.

A motor "works" if its encoder moves ≥ MOVE_MIN rad from the open baseline when
commanded closed (delta-based, so the hand's open rest offset doesn't matter).

Usage: hand_selftest.py [iface]   → prints one JSON verdict line.
"""
import json
import sys
import time

IFACE = sys.argv[1] if len(sys.argv) > 1 else "enp132s0"
UPPER = [1.5184, 1.0472, 1.4661, 1.4661, 1.4661, 1.4661]  # closed rad per motor
MOVE_MIN = 0.3   # rad; encoder must move at least this much to count as "works"


def out(d):
    print(json.dumps(d, ensure_ascii=False))
    sys.exit(0)


try:
    from unitree_sdk2py.core.channel import (
        ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber)
    from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorCmds_, MotorStates_
    from unitree_sdk2py.idl.default import unitree_go_msg_dds__MotorCmd_
except Exception as e:
    out({"ok": False, "error": f"unitree_sdk2py 导入失败: {e}"})

try:
    ChannelFactoryInitialize(0, IFACE)
except Exception as e:
    out({"ok": False, "error": f"DDS 初始化失败({IFACE}): {e}"})

pub = {"L": ChannelPublisher("rt/brainco/left/cmd", MotorCmds_),
       "R": ChannelPublisher("rt/brainco/right/cmd", MotorCmds_)}
for p in pub.values():
    p.Init()
st = {"L": None, "R": None}
ChannelSubscriber("rt/brainco/left/state", MotorStates_).Init(lambda m: st.__setitem__("L", m), 10)
ChannelSubscriber("rt/brainco/right/state", MotorStates_).Init(lambda m: st.__setitem__("R", m), 10)

t = time.time()
while time.time() - t < 3 and not (st["L"] or st["R"]):
    time.sleep(0.1)
if not (st["L"] or st["R"]):
    out({"ok": False, "error": "没收到 rt/brainco state —— PC2 手服务端在跑吗？手通电了吗？"})


def send(tg):
    for s in ("L", "R"):
        m = MotorCmds_()
        m.cmds = [unitree_go_msg_dds__MotorCmd_() for _ in range(6)]
        for i in range(6):
            m.cmds[i].q = tg[i] * UPPER[i]
            m.cmds[i].dq = 1.0
        pub[s].Write(m)


def goto(frm, to, ramp=0.8, hold=1.2):
    n = int(ramp * 50)
    for k in range(n + 1):
        r = k / n
        send([frm[i] * (1 - r) + to[i] * r for i in range(6)])
        time.sleep(0.02)
    e = time.time()
    while time.time() - e < hold:
        send(to)
        time.sleep(0.02)


def snap():
    return {s: ([round(st[s].states[i].q, 3) for i in range(6)] if st[s] else None)
            for s in ("L", "R")}


O = [0] * 6
goto(O, O, 0.4, 1.0)
base = snap()

GROUPS = [
    ("thumb_metacarpal(对掌)", [0], [0.7, 0, 0, 0, 0, 0]),
    ("thumb_proximal(弯曲)", [1], [0, 0.7, 0, 0, 0, 0]),
    ("index/middle/ring/pinky(四指)", [2, 3, 4, 5], [0, 0, 0.7, 0.7, 0.7, 0.7]),
]
hands = {"L": {"present": st["L"] is not None, "motors": []},
         "R": {"present": st["R"] is not None, "motors": []}}
for gname, idxs, tg in GROUPS:
    goto(O, tg)
    peak = snap()
    goto(tg, O, 0.8, 0.3)
    for s in ("L", "R"):
        if base[s] is None or peak[s] is None:
            continue
        for i in idxs:
            delta = peak[s][i] - base[s][i]
            hands[s]["motors"].append(
                {"motor": i, "group": gname, "delta": round(delta, 2), "ok": delta >= MOVE_MIN})

goto(O, O, 0.3, 0.4)

bad, total, okc = [], 0, 0
for s in ("L", "R"):
    for mo in hands[s]["motors"]:
        total += 1
        if mo["ok"]:
            okc += 1
        else:
            bad.append(f"{s}.motor{mo['motor']}")
out({"ok": True, "hands": hands, "pass": okc, "total": total, "bad": bad,
     "verdict": (f"✓ 双手 {okc}/{total} 个电机都动了，手正常"
                 if not bad else
                 f"✗ {len(bad)} 个没动: {', '.join(bad)} —— 查该指 机械/串口/通电")})
