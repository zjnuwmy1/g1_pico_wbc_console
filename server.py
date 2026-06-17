#!/usr/bin/env python3
"""G1 PICO Whole-Body Teleop Wizard Console — zero-dependency stdlib server.

Run:  python3 server.py [--port 8090]
"""
from __future__ import annotations

import argparse
import atexit
import json
import mimetypes
import secrets
import shutil
import signal
import subprocess
import sys
import time
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import checks
import config
from procman import ProcessManager

PM = ProcessManager()

# ── wizard step persistence ──────────────────────────────────────────────────
_state_lock = threading.Lock()


def _load_state() -> dict:
    try:
        return json.loads(config.STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"steps": {}}


def _save_state(st: dict) -> None:
    config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2))


WIZARD = _load_state()
# Step 5's safety confirmation is session-only by design (user safety rule):
WIZARD.get("steps", {}).pop("5", None)

# ── safety token (real-robot gate) ───────────────────────────────────────────
_safety = {"token": None, "expires": 0.0}
SAFETY_CHECKLIST_KEYS = [
    "sim_rehearsed",      # ① sim 流程已演练
    "robot_secured",      # ② 机器人已悬挂或处于空旷场地
    "debug_mode",         # ③ 已进入 debug 模式，原生控制已脱离
    "estop_person",       # ④ 已指定急停员
]


def mint_safety_token(body: dict) -> tuple[int, dict]:
    cl = body.get("checklist") or {}
    missing = [k for k in SAFETY_CHECKLIST_KEYS if not cl.get(k)]
    if missing:
        return 400, {"ok": False, "error": f"checklist 未全部确认: {missing}"}
    typed = (body.get("typed") or "").strip()
    if typed not in config.SAFETY_TYPED_PHRASES:
        return 400, {"ok": False, "error": "请输入「确认姿态」以确认机器人当前姿态安全"}
    # auto checks
    if PM.any_alive(("sim_loop", "deploy_sim")):
        return 400, {"ok": False, "error": "sim 进程仍在运行，先全部停止"}
    if checks.find_real_robot_interface() is None:
        return 400, {"ok": False, "error": "未检测到 192.168.123.x 机器人网卡"}
    token = secrets.token_urlsafe(16)
    _safety["token"] = token
    _safety["expires"] = time.time() + config.SAFETY_TOKEN_TTL_S
    return 200, {"ok": True, "safety_token": token,
                 "expires_in": config.SAFETY_TOKEN_TTL_S}


def consume_safety_token(token: str | None) -> str | None:
    """Return error string, or None if the token is valid (single-use)."""
    if not token or token != _safety["token"]:
        return "缺少或错误的 safety_token —— 请先完成安全确认"
    if time.time() > _safety["expires"]:
        return "safety_token 已过期（120s），请重新确认"
    _safety["token"] = None  # single-use
    return None


# ── pico probe ───────────────────────────────────────────────────────────────
_probe = {"running": False, "result": None, "ts": 0.0}


def run_pico_probe() -> dict:
    if not config.VENV_PY.is_file():
        return {"ok": False, "error": ".venv_teleop 不存在，先完成步骤 2"}
    if _probe["running"]:
        return {"ok": False, "error": "探测已在进行中"}
    _probe["running"] = True

    def worker():
        try:
            p = subprocess.run(
                [str(config.VENV_PY), str(config.PROBE_PY)],
                capture_output=True, text=True, timeout=25,
                cwd=str(config.REPO_ROOT),
            )
            line = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "{}"
            try:
                _probe["result"] = json.loads(line)
            except ValueError:
                _probe["result"] = {"error": (p.stdout + p.stderr)[-400:]}
        except subprocess.TimeoutExpired:
            _probe["result"] = {"error": "探测超时（25s）"}
        finally:
            _probe["ts"] = time.time()
            _probe["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "note": "探测已启动，结果在 /api/state 的 pico 字段"}


# ── real G1 high-level mode switch ───────────────────────────────────────────
def run_g1_mode(action: str, name: str = "") -> dict:
    if not config.VENV_PY.is_file():
        return {"ok": False, "error": ".venv_teleop 不存在"}
    # A running deploy saturates the DDS domain on the robot interface and starves
    # the MotionSwitcher RPC — every CheckMode/ReleaseMode call then times out and
    # the button looks dead ("按不动"). Correct order is: stop deploy → switch mode
    # → restart deploy. Refuse fast with a clear message instead of hanging 30s.
    if PM.any_alive(("deploy_real", "deploy_sim")):
        return {"ok": False, "error": "请先停止 deploy 再操作模式 —— deploy 占满机器人 "
                "DDS 域会让模式 RPC 卡死超时(实测 30s)。正确顺序:停 deploy → 释放/切换模式 → 再起 deploy。"}
    robot = checks.find_real_robot_interface()
    if robot is None:
        return {"ok": False, "error": "未检测到机器人网接口(192.168.123.x)，检查机器人网/上电"}
    cmd = [str(config.VENV_PY), str(config.G1_MODE_PY), robot[0], action]
    if action == "select" and name:
        cmd.append(name)
    # The first DDS participant after an idle period reliably fails CheckMode with
    # code 3104 (cold service discovery) and primes the kernel multicast state; a
    # fresh subprocess then succeeds. Each GUI click is a brand-new subprocess, so
    # it always pays that first-call cost. Auto-retry the whole subprocess on a
    # 3104 to make the button just work. (≤2 attempts, still bounded < server cap.)
    res = {"ok": False, "error": "未执行"}
    for attempt in range(2):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                               cwd=str(config.REPO_ROOT))
            line = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "{}"
            try:
                res = json.loads(line)
            except ValueError:
                res = {"ok": False, "error": (p.stdout + p.stderr)[-400:]}
        except subprocess.TimeoutExpired:
            res = {"ok": False, "error": "G1 模式操作超时(30s)，机器人主控在跑吗?"}
        if res.get("ok") or "3104" not in str(res.get("error", "")):
            break
    return res


def run_verify_motors() -> dict:
    """One-shot: does the robot actually track rt/lowcmd? (Init Done real vs fake)"""
    if not config.VENV_PY.is_file():
        return {"ok": False, "error": ".venv_teleop 不存在"}
    robot = checks.find_real_robot_interface()
    if robot is None:
        return {"ok": False, "error": "未检测到机器人网接口(192.168.123.x)"}
    cmd = [str(config.VENV_PY), str(config.VERIFY_MOTORS_PY), robot[0]]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           cwd=str(config.REPO_ROOT))
        line = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "{}"
        try:
            return json.loads(line)
        except ValueError:
            return {"ok": False, "error": (p.stdout + p.stderr)[-400:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "验证超时(20s)"}


def _hand_selftest_subproc(iface: str) -> dict:
    try:
        cmd = [str(config.VENV_PY), str(config.HAND_SELFTEST_PY), iface]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=45,
                           cwd=str(config.REPO_ROOT))
        line = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else "{}"
        try:
            return json.loads(line)
        except ValueError:
            return {"ok": False, "error": (p.stdout + p.stderr)[-400:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "自检超时(45s)"}


def run_hand_selftest() -> dict:
    """Drive both hands through each motor group and check the encoder follows.
    Stops the hand bridge first (needs sole publisher on rt/brainco/*/cmd) and
    restarts it after. SELF-HEALS: if there's no rt/brainco state (PC2 server in
    a bad DDS state), auto-restarts the server and retries once. MOVES HANDS ~12s."""
    if not config.VENV_PY.is_file():
        return {"ok": False, "error": ".venv_teleop 不存在"}
    # NEVER run heavy DDS diagnostics while a real deploy is controlling the robot:
    # the extra participants can starve rt/lowstate and trip the deploy's safety
    # stop ("Lost LowState"), killing a live teleop session. Self-test before the
    # deploy, or stop the deploy first.
    if PM.any_alive(("deploy_real",)):
        return {"ok": False, "error": "deploy(real) 在跑 —— 自检手会停手桥+驱动手+起 DDS 探针，"
                "可能让 deploy 丢 lowstate 安全停。请在起 deploy 前自检，或先停 deploy。"}
    robot = checks.find_real_robot_interface()
    if robot is None:
        return {"ok": False, "error": "未检测到机器人网接口(192.168.123.x)"}
    iface = robot[0]
    bridge_was = PM.any_alive(("brainco_hand",))
    if bridge_was:
        PM.stop("brainco_hand")
        time.sleep(1.2)
    res = {"ok": False, "error": "未执行"}
    try:
        res = _hand_selftest_subproc(iface)
        # self-heal: "no rt/brainco state" usually means the PC2 hand server is in
        # a bad DDS state — restart it and retry once, transparently.
        if not res.get("ok") and "rt/brainco state" in str(res.get("error", "")):
            rec = run_brainco_restart()
            time.sleep(3.0)
            res = _hand_selftest_subproc(iface)
            res["auto_recovered"] = True
            res["recover_server_ok"] = bool(rec.get("ok"))
    finally:
        if bridge_was:
            PM.start("brainco_hand")
            res["bridge_restarted"] = True
    return res


def run_brainco_ensure() -> dict:
    """Probe rt/brainco state; if the PC2 hand server isn't publishing (bad DDS
    state), restart it and re-probe. Called at teleop startup so the hand server
    is healthy before the bridge connects — folds the hand debug into bring-up."""
    if not config.VENV_PY.is_file():
        return {"ok": False, "error": ".venv_teleop 不存在"}
    robot = checks.find_real_robot_interface()
    if robot is None:
        return {"ok": False, "error": "未检测到机器人网接口(192.168.123.x)"}
    iface = robot[0]

    def _probe():
        try:
            p = subprocess.run([str(config.VENV_PY), str(config.BRAINCO_PROBE_PY), iface],
                               capture_output=True, text=True, timeout=12,
                               cwd=str(config.REPO_ROOT))
            return json.loads(p.stdout.strip().splitlines()[-1])
        except Exception as e:
            return {"ok": False, "error": str(e)}

    r = _probe()
    if r.get("ok"):
        return {"ok": True, "healthy": True, "restarted": False}
    rec = run_brainco_restart()
    time.sleep(3.0)
    r2 = _probe()
    return {"ok": bool(r2.get("ok")), "healthy": bool(r2.get("ok")),
            "restarted": True, "recover_server_ok": bool(rec.get("ok"))}


def run_brainco_restart() -> dict:
    """Restart the PC2 BrainCo hand server (it dies on PC2 reboot/net blip and
    takes hand q + tactile with it). Runs start_brainco.sh twice — the left hand
    often fails its first serial scan. No sudo needed (dialout group)."""
    remote = ("bash ~/start_brainco.sh >/dev/null 2>&1; sleep 2; "
              "bash ~/start_brainco.sh 2>&1 | grep -iE 'bound|hand' | tail -4; "
              "pgrep -f '[b]rainco_hand_server' >/dev/null && echo SERVER_ALIVE")
    cmd = ["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", "g1pc2", remote]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        out = (p.stdout + p.stderr).strip()[-500:]
        return {"ok": "SERVER_ALIVE" in p.stdout, "out": out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh PC2 超时（PC2 在线吗？）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_camera_restart() -> dict:
    """Restart the PC2 head-camera image server (systemd pc2-image-server.service,
    runs pc2_image_server_with_depth.py --rs). Recovery for a frozen camera feed:
    the RealSense/USB or the server wedges and keeps re-publishing the same frame.
    systemctl restart kills the whole CGroup (parent + worker) and reopens the
    RealSense; ZMQ subscribers (recorder/deploy) reconnect to the fresh PUB."""
    remote = ("echo 123 | sudo -S systemctl restart pc2-image-server.service 2>/dev/null; "
              "sleep 3; systemctl is-active pc2-image-server.service; "
              "pgrep -f '[p]c2_image_server_with_depth' >/dev/null && echo CAMERA_ALIVE")
    cmd = ["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", "g1pc2", remote]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        out = (p.stdout + p.stderr).strip()[-500:]
        return {"ok": "CAMERA_ALIVE" in p.stdout, "out": out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh PC2 超时（PC2 在线吗？）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── checks cache ─────────────────────────────────────────────────────────────
_checks_cache: dict[str, list] = {}


def run_checks(scope: str) -> list[dict]:
    result = checks.run_all_checks(scope)
    _checks_cache[scope] = result
    return result


# ── consolidated state ───────────────────────────────────────────────────────

def teleop_state() -> dict:
    out = {"stream_mode": None, "loco_mode": None, "loco_mode_name": None,
           "deploy_control": False, "deploy_ready": False, "estop_ack": False,
           "deploy_waiting_lowstate": False}
    s = PM.get("pico_streamer")
    if s:
        sm = s.parsed.get("stream_mode")
        if isinstance(sm, tuple) and len(sm) == 2:
            out["stream_mode"] = sm[1]
        lm = s.parsed.get("loco_mode")
        if isinstance(lm, tuple) and len(lm) == 2:
            out["loco_mode"] = int(lm[0])
            out["loco_mode_name"] = lm[1]
        if not s.alive() and s.returncode is not None:
            out["stream_mode"] = "OFF"
    for name in ("deploy_real", "deploy_sim"):
        d = PM.get(name)
        if d and d.alive():
            out["deploy_ready"] = d.status == "ready" or "ready_marker" in d.parsed
            out["deploy_control"] = "control" in d.parsed
            out["estop_ack"] = "estop_ack" in d.parsed
            out["deploy_waiting_lowstate"] = "waiting_lowstate" in d.parsed
            out["active_deploy"] = name
            break
    return out


def _pc_service_alive() -> bool:
    """The runService.sh launcher forks the real daemon and exits rc=0, so the
    managed-proc status is misleading. Check the actual PC Service socket."""
    import socket as _s
    sk = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sk.settimeout(0.2)
    try:
        return sk.connect_ex(("127.0.0.1", 60061)) == 0
    finally:
        sk.close()


def api_state() -> dict:
    robot = checks.find_real_robot_interface()
    return {
        "steps": WIZARD.get("steps", {}),
        "procs": PM.snapshot(),
        "teleop": teleop_state(),
        "pico": {"running": _probe["running"], "result": _probe["result"], "ts": _probe["ts"]},
        "pc_service_alive": _pc_service_alive(),
        "hand_mode": PM.hand_mode,
        "checks_cache": _checks_cache,
        "ips": {
            "wifi": checks.wifi_lan_ips(),
            "all": checks.lan_ips(),
            "headset_host": checks.headset_host(),
            "robot_iface": list(robot) if robot else None,
            "robot_prefix": config.ROBOT_SUBNET_PREFIX,
        },
        "safety": {"armed": bool(_safety["token"]) and time.time() < _safety["expires"],
                   "ttl": max(0, int(_safety["expires"] - time.time())) if _safety["token"] else 0},
        "config": {
            "deb_url": config.DEB_URL_2404,
            "apk_url": config.APK_URL,
            "gui_port": config.GUI_PORT,
        },
        "now": time.time(),
    }


def _dir_size(path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def api_recordings() -> dict:
    """List recorded sessions + episodes for the data viewer / overview, with
    enough metadata to see what was recorded at a glance (frames, duration,
    on-disk size, which modalities, whether stage-annotated)."""
    base = config.RECORDINGS_DIR
    sessions = []
    if base.is_dir():
        for sess in sorted([p for p in base.iterdir() if p.is_dir()], reverse=True):
            eps, s_bytes = [], 0
            for ep in sorted([p for p in sess.iterdir() if p.is_dir()
                              and p.name.startswith("episode_")]):
                info = {"name": ep.name,
                        "path": f"/recordings/{sess.name}/{ep.name}",
                        "frames": 0, "goal": "", "cameras": [], "seconds": 0.0,
                        "bytes": 0, "touch": False, "state": False, "stages": 1}
                dj = ep / "data.json"
                if dj.is_file():
                    try:
                        d = json.loads(dj.read_text())
                        data = d.get("data", [])
                        meta = d.get("info", {})
                        info["frames"] = len(data)
                        info["goal"] = d.get("text", {}).get("goal", "")
                        info["cameras"] = meta.get("cameras", [])
                        fps = float(meta.get("fps", 30)) or 30.0
                        info["seconds"] = round(len(data) / fps, 1)
                        info["touch"] = any(f.get("tactile") for f in data)
                        info["state"] = any(f.get("state") for f in data)
                        info["stages"] = len({f.get("stage", 0) for f in data}) or 1
                    except Exception:
                        pass
                b = _dir_size(ep)
                info["bytes"] = b
                s_bytes += b
                eps.append(info)
            if eps:
                sessions.append({"session": sess.name, "episodes": eps,
                                 "count": len(eps),
                                 "frames": sum(e["frames"] for e in eps),
                                 "bytes": s_bytes})
    return {"ok": True, "sessions": sessions}


def delete_recording(rel_path: str) -> dict:
    """Permanently delete an episode (or a whole session) directory under
    recordings/. Path is resolved and must sit strictly inside RECORDINGS_DIR —
    the root itself and any path escaping it are refused."""
    if not rel_path.startswith("/recordings/"):
        return {"ok": False, "error": "bad path"}
    sub = rel_path[len("/recordings/"):].strip("/")
    if not sub:
        return {"ok": False, "error": "拒绝删除 recordings 根目录"}
    root = config.RECORDINGS_DIR.resolve()
    target = (config.RECORDINGS_DIR / sub).resolve()
    if target == root or root not in target.parents:
        return {"ok": False, "error": "bad path"}
    if not target.is_dir():
        return {"ok": False, "error": "目录不存在"}
    try:
        shutil.rmtree(target)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "deleted": rel_path}


def apply_annotation(rel_path: str, boundaries: list) -> dict:
    """Write stage labels into an episode's data.json (offline annotation). Each
    boundary {frame, stage, label} sets the stage from that frame index onward
    until the next boundary. Frames before the first boundary -> stage 0."""
    if not rel_path.startswith("/recordings/"):
        return {"ok": False, "error": "bad path"}
    ep = (config.RECORDINGS_DIR / rel_path[len("/recordings/"):]).resolve()
    if not str(ep).startswith(str(config.RECORDINGS_DIR.resolve())):
        return {"ok": False, "error": "bad path"}
    dj = ep / "data.json"
    if not dj.is_file():
        return {"ok": False, "error": "data.json 不存在"}
    try:
        d = json.loads(dj.read_text())
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        bs = sorted(({"frame": int(b["frame"]), "stage": int(b["stage"]),
                      "label": str(b.get("label", ""))} for b in boundaries),
                    key=lambda x: x["frame"])
    except (KeyError, ValueError, TypeError):
        return {"ok": False, "error": "bad boundaries"}
    for i, fr in enumerate(d.get("data", [])):
        cur = {"stage": 0, "label": ""}
        for b in bs:
            if b["frame"] <= i:
                cur = b
            else:
                break
        fr["stage"], fr["stage_label"] = cur["stage"], cur["label"]
    dj.write_text(json.dumps(d, separators=(",", ":")))
    return {"ok": True, "frames": len(d.get("data", [])), "boundaries": len(bs)}


def export_episode_mp4(rel_path: str, kind: str = "head") -> dict:
    """ffmpeg-encode an episode into an MP4 for download. Frames are contiguous
    000000.._head.jpg (+ _depth.png), so the image2 demuxer reads them straight.
      kind="head" -> head-camera RGB only            -> head.mp4
      kind="rgbd" -> RGB | turbo-colorised depth      -> head_rgbd.mp4
                     (16-bit depth normalised then pseudocolor'd, hstacked)
    Returns the product's /recordings URL (served by _serve_recording as
    video/mp4)."""
    if not rel_path.startswith("/recordings/"):
        return {"ok": False, "error": "bad path"}
    ep = (config.RECORDINGS_DIR / rel_path[len("/recordings/"):]).resolve()
    if not str(ep).startswith(str(config.RECORDINGS_DIR.resolve())) or not ep.is_dir():
        return {"ok": False, "error": "bad path"}
    frames = ep / "frames"
    if not frames.is_dir() or not any(frames.glob("*_head.jpg")):
        return {"ok": False, "error": "该 episode 没有 head 帧可导出"}
    # input framerate from data.json (default 30) so playback matches capture
    fps = 30.0
    dj = ep / "data.json"
    if dj.is_file():
        try:
            fps = float(json.loads(dj.read_text()).get("info", {}).get("fps", 30)) or 30.0
        except Exception:
            pass
    fr = f"{fps:g}"
    if kind == "rgbd":
        if not any(frames.glob("*_depth.png")):
            return {"ok": False, "error": "该 episode 没有深度帧（无法导 RGBD）"}
        out = ep / "head_rgbd.mp4"
        cmd = ["ffmpeg", "-y",
               "-framerate", fr, "-i", str(frames / "%06d_head.jpg"),
               "-framerate", fr, "-i", str(frames / "%06d_depth.png"),
               "-filter_complex",
               "[0:v]format=yuv420p[rgb];"
               "[1:v]format=gray16le,normalize=smoothing=30,format=yuv444p,"
               "pseudocolor=preset=turbo,format=yuv420p[d];"
               "[rgb][d]hstack=inputs=2[v]",
               "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(out)]
    else:
        out = ep / "head.mp4"
        cmd = ["ffmpeg", "-y", "-framerate", fr,
               "-i", str(frames / "%06d_head.jpg"),
               "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(out)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        return {"ok": False, "error": "ffmpeg 未安装（apt install ffmpeg）"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffmpeg 编码超时"}
    if p.returncode != 0 or not out.is_file():
        return {"ok": False, "error": "ffmpeg 失败：" + (p.stderr or "")[-300:]}
    return {"ok": True, "url": rel_path + "/" + out.name,
            "size": out.stat().st_size, "fps": fps, "kind": kind}


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "G1PicoWBCConsole/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel: str) -> None:
        if rel in ("", "/"):
            rel = "index.html"
        target = (config.STATIC_DIR / rel.lstrip("/")).resolve()
        if not str(target).startswith(str(config.STATIC_DIR.resolve())) or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        # Text assets are UTF-8 (Chinese UI). Without an explicit charset the
        # HTTP header wins over <meta charset> in some browsers → mojibake.
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_recording(self, rel: str) -> None:
        # serve files under recordings/ (episode images + data.json) for the viewer
        sub = rel[len("/recordings/"):]
        target = (config.RECORDINGS_DIR / sub).resolve()
        if not str(target).startswith(str(config.RECORDINGS_DIR.resolve())) or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype == "application/json":
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode()) if n else {}
        except (ValueError, OSError):
            return {}

    # ── routes ───────────────────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/state":
            self._json(200, api_state())
        elif u.path == "/api/recordings":
            self._json(200, api_recordings())
        elif u.path.startswith("/recordings/"):
            self._serve_recording(u.path)
        elif u.path.startswith("/api/logs/"):
            name = u.path.rsplit("/", 1)[-1]
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            self._json(200, PM.logs_since(name, since))
        else:
            self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        parts = [p for p in u.path.split("/") if p]

        if u.path == "/api/estop":
            self._json(200, PM.estop())
            return
        if u.path == "/api/checks/run":
            self._json(200, {"ok": True, "checks": run_checks(body.get("scope", "env"))})
            return
        if u.path == "/api/pico/probe":
            self._json(200, run_pico_probe())
            return
        if u.path == "/api/safety/confirm":
            code, obj = mint_safety_token(body)
            self._json(code, obj)
            return
        if u.path == "/api/hand_mode":
            res = PM.set_hand_mode(body.get("mode", ""))
            self._json(200 if res.get("ok") else 409, res)
            return
        if u.path == "/api/drive":
            # relay manual-drive intent to the gui_drive process via its stdin
            allowed = ("policy", "gait", "fwd", "strafe", "turn", "speed")
            intent = {k: body[k] for k in allowed if k in body}
            line = (json.dumps(intent) + "\n").encode()
            self._json(200, PM.write_stdin("gui_drive", line))
            return
        if u.path == "/api/recordings/annotate":
            self._json(200, apply_annotation(body.get("path", ""), body.get("boundaries", [])))
            return
        if u.path == "/api/recordings/export":
            self._json(200, export_episode_mp4(body.get("path", ""), body.get("kind", "head")))
            return
        if u.path == "/api/recordings/delete":
            self._json(200, delete_recording(body.get("path", "")))
            return
        if u.path == "/api/record":
            # relay episode-recorder control to the recorder process via its stdin
            cmd = body.get("cmd")
            if cmd not in ("start", "stop", "discard", "stage", "goal", "quit", "auto"):
                self._json(400, {"ok": False, "error": "bad cmd"})
                return
            allowed = ("cmd", "stage", "label", "goal", "on")
            intent = {k: body[k] for k in allowed if k in body}
            line = (json.dumps(intent) + "\n").encode()
            self._json(200, PM.write_stdin("recorder", line))
            return
        if u.path == "/api/brainco/restart":
            self._json(200, run_brainco_restart())
            return
        if u.path == "/api/camera/restart":
            self._json(200, run_camera_restart())
            return
        if u.path == "/api/brainco/ensure":
            self._json(200, run_brainco_ensure())
            return
        if u.path == "/api/hand/selftest":
            self._json(200, run_hand_selftest())
            return
        if u.path == "/api/verify/motors":
            self._json(200, run_verify_motors())
            return
        if u.path == "/api/g1/mode":
            action = body.get("action", "check")
            if action not in ("check", "release", "select"):
                self._json(400, {"ok": False, "error": "bad action"})
                return
            # release removes the robot's balance control (it goes limp) — gate it.
            if action == "release":
                if not body.get("confirm"):
                    self._json(400, {"ok": False, "error": "release 需要 confirm（机器人会变软，确认吊架）"})
                    return
                if PM.any_alive(("deploy_real", "deploy_sim")):
                    self._json(409, {"ok": False, "error": "先停掉 deploy 再释放模式（避免机器人猛跳）"})
                    return
            self._json(200, run_g1_mode(action, body.get("name", "")))
            return
        if u.path == "/api/deploy/key":
            name = body.get("name", "")
            key = (body.get("key") or "")[:1]
            if name not in ("deploy_sim", "deploy_real") or not key:
                self._json(400, {"ok": False, "error": "bad name/key"})
                return
            if key.lower() == "o":  # E-stop key goes through the estop path
                self._json(200, PM.estop())
                return
            self._json(200, PM.write_stdin(name, key.encode()))
            return

        # /api/procs/<name>/start|stop
        if len(parts) == 4 and parts[:2] == ["api", "procs"]:
            name, action = parts[2], parts[3]
            if action == "start":
                if name == "deploy_real":
                    err = consume_safety_token(body.get("safety_token"))
                    if err:
                        self._json(403, {"ok": False, "error": err})
                        return
                self._json(200, PM.start(name, vis=bool(body.get("vis"))))
                return
            if action == "stop":
                self._json(200, PM.stop(name))
                return

        # /api/steps/<n>/complete|reopen
        if len(parts) == 4 and parts[:2] == ["api", "steps"]:
            n, action = parts[2], parts[3]
            if n == "5" and action == "complete":
                self._json(400, {"ok": False, "error": "步骤5不可标记完成（每次都需安全确认）"})
                return
            with _state_lock:
                steps = WIZARD.setdefault("steps", {})
                if action == "complete":
                    steps[n] = {"done": True, "ts": time.time()}
                elif action == "reopen":
                    steps.pop(n, None)
                _save_state(WIZARD)
            self._json(200, {"ok": True, "steps": WIZARD["steps"]})
            return

        self._json(404, {"error": "not found"})


# ── shutdown ─────────────────────────────────────────────────────────────────

def _graceful_exit(*_):
    print("\n[console] shutting down: stopping managed processes gracefully …")
    PM.shutdown_all()
    sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=config.GUI_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    atexit.register(PM.shutdown_all)
    signal.signal(signal.SIGTERM, _graceful_exit)
    signal.signal(signal.SIGINT, _graceful_exit)

    # warm the env checks cache so first page load isn't empty
    threading.Thread(target=run_checks, args=("env",), daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[console] G1 PICO WBC Console → http://{args.host}:{args.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
