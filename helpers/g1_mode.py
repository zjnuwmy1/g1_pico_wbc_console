#!/usr/bin/env python3
"""Switch the real G1 into low-level (lowcmd) mode, or restore a high-level mode.

Releasing the high-level mode is the prerequisite for the gear_sonic / GR00T
whole-body deploy to take over via raw lowcmd — exactly the prep the official
`g1_low_level_example.py` does: CheckMode(), then ReleaseMode() in a loop until
no high-level mode is active.

⚠️ SAFETY: releasing the high-level mode REMOVES the robot's balance control —
the robot goes limp and will drop. ONLY run with the robot suspended on a
gantry / in a safe state. Run from the .venv_teleop python.

Usage:
    g1_mode.py <iface> release         # release all high-level modes -> lowcmd ready
    g1_mode.py <iface> check           # just report the current mode
    g1_mode.py <iface> select <name>   # re-select a high-level mode (e.g. normal)
"""
import json
import sys
import time


def _check(msc, retries=3):  # server.run_g1_mode wraps a 2nd subprocess on 3104
    # The motion_switcher RPC intermittently times out (code 3104) on a freshly
    # initialised DDS participant — discovery isn't warm yet. Retry until it
    # answers (code 0). The robot itself is fine (publishes lowstate at ~1kHz).
    code, data = -1, None
    for _ in range(retries):
        code, data = msc.CheckMode()
        if code == 0:
            return code, (data if isinstance(data, dict) else None)
        time.sleep(0.5)
    return code, None


def main():
    iface = sys.argv[1] if len(sys.argv) > 1 else ""
    action = sys.argv[2] if len(sys.argv) > 2 else "check"

    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import (
            MotionSwitcherClient,
        )
    except Exception as e:  # SDK not importable
        print(json.dumps({"ok": False, "error": f"unitree_sdk2py 导入失败: {e}"}))
        return

    try:
        if iface:
            ChannelFactoryInitialize(0, iface)
        else:
            ChannelFactoryInitialize(0)
        msc = MotionSwitcherClient()
        msc.SetTimeout(2.0)   # 8 retries * 2s + sleeps ≈ 20s, under server's 30s cap
        msc.Init()
        time.sleep(1.0)       # let DDS service discovery warm up before first RPC
                              # (cold participant => first CheckMode often 3104)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"DDS/MotionSwitcher 初始化失败(机器人可达?): {e}"}))
        return

    code, before = _check(msc)
    if code != 0:
        print(json.dumps({"ok": False, "error": f"CheckMode 失败 code={code}(机器人主控在跑吗?)"}))
        return

    out = {"before": before}

    if action == "check":
        out["ok"] = True
        out["mode"] = before
        print(json.dumps(out, ensure_ascii=False))
        return

    if action == "select":
        name = sys.argv[3] if len(sys.argv) > 3 else "normal"
        msc.SelectMode(name)
        time.sleep(1.0)
        _, after = _check(msc)
        out.update(ok=bool(after and after.get("name") == name), after=after, selected=name)
        print(json.dumps(out, ensure_ascii=False))
        return

    # action == "release": loop ReleaseMode until no high-level mode is active
    tries = 0
    _, cur = _check(msc)
    while cur and cur.get("name") and tries < 8:
        msc.ReleaseMode()
        time.sleep(1.0)
        _, cur = _check(msc)
        tries += 1
    released = not (cur and cur.get("name"))
    out.update(ok=released, after=cur, tries=tries)
    out["note"] = "已释放高层模式，G1 现在接受外部 lowcmd（机器人已变软，靠吊架）" if released \
        else "仍有高层模式未释放，重试或检查机器人状态"
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
