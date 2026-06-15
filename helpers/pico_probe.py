#!/usr/bin/env python
"""PICO connectivity probe — MUST run under .venv_teleop/bin/python.

Prints one JSON line with a layered diagnosis so the GUI can tell *where*
the chain breaks: headset link → motion trackers paired → tracker data
streaming → full-body SMPL ready.

  {"sdk", "init", "body_data",            # the original 3
   "headset", "controllers",              # link is live (head/controller data)
   "trackers_paired", "tracker_serials",  # ankle Motion Trackers paired
   "motion_data",                         # tracker frames flowing (max seen)
   "stage", "error"}                      # stage = where it stopped

Does NOT launch the PC service — that's an explicit GUI action.
"""
from __future__ import annotations

import json
import os
import sys
import time

OUT = {
    "sdk": False, "init": False, "body_data": False,
    "headset": False, "controllers": False,
    "trackers_paired": 0, "tracker_serials": [],
    "motion_data": 0, "stage": "init", "error": "",
}
TIMEOUT_S = 12.0


def _nonzero(v) -> bool:
    try:
        return any(abs(float(x)) > 1e-9 for x in list(v))
    except Exception:  # noqa: BLE001
        return bool(v)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def main() -> None:
    try:
        import xrobotoolkit_sdk as xrt
    except ImportError as e:
        OUT["error"] = f"xrobotoolkit_sdk 导入失败: {e}"
        print(json.dumps(OUT, ensure_ascii=False), flush=True)
        return
    OUT["sdk"] = True
    try:
        xrt.init()
        OUT["init"] = True
    except Exception as e:  # noqa: BLE001
        OUT["error"] = f"xrt.init() 失败（PC Service 未运行？）: {e}"
        print(json.dumps(OUT, ensure_ascii=False), flush=True)
        return

    deadline = time.time() + TIMEOUT_S
    while time.time() < deadline:
        # 1) headset link live?
        if not OUT["headset"] and _nonzero(_safe(lambda: xrt.get_headset_pose(), [])):
            OUT["headset"] = True
        # 2) controllers live?
        if not OUT["controllers"] and _nonzero(_safe(lambda: xrt.get_left_controller_pose(), [])):
            OUT["controllers"] = True
        # 3) motion trackers paired?
        serials = _safe(lambda: list(xrt.get_motion_tracker_serial_numbers()), []) or []
        serials = [s for s in serials if s]
        if len(serials) > OUT["trackers_paired"]:
            OUT["trackers_paired"] = len(serials)
            OUT["tracker_serials"] = serials
        # 4) tracker data flowing?
        n = _safe(lambda: int(xrt.num_motion_data_available()), 0) or 0
        if n > OUT["motion_data"]:
            OUT["motion_data"] = n
        # 5) full-body SMPL ready?
        if _safe(lambda: bool(xrt.is_body_data_available()), False):
            OUT["body_data"] = True
            break
        time.sleep(0.4)

    # layered diagnosis
    if OUT["body_data"]:
        OUT["stage"] = "ok"
    elif not OUT["headset"] and not OUT["controllers"]:
        OUT["stage"] = "no_link"
        OUT["error"] = ("未收到头显/手柄数据：app 里的 PC IP 是否填对、Status 是否 WORKING、"
                        "头显与工作站是否互通（同网/同 USB 链路）。")
    elif OUT["trackers_paired"] == 0:
        OUT["stage"] = "no_trackers"
        OUT["error"] = ("头显已连上，但未检测到 Motion Tracker。请在头显里配对 2 个脚踝 tracker"
                        "（长按 6 秒进配对，红蓝闪烁），app 的 Motion Tracker 选 Full body。")
    elif OUT["motion_data"] == 0:
        OUT["stage"] = "trackers_no_data"
        OUT["error"] = (f"已配对 {OUT['trackers_paired']} 个 tracker，但收不到数据：检查 tracker 是否开机/有电、"
                        "灯朝上未被裤腿遮挡、且在头显摄像头视野内。")
    else:
        OUT["stage"] = "no_body"
        OUT["error"] = ("Tracker 数据已在流动，但全身姿态未就绪：在头显完成全身校准"
                        "（站直贴身 → 低头看脚两段序列），确保 app 里 Data/Control 选了 Send、Full body。")
    print(json.dumps(OUT, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
    # xrt SDK static destructors can throw on teardown; result already printed.
    os._exit(0)
