"""Preflight checks. Each returns
{"id", "label", "ok", "level": "ok|warn|fail", "detail", "fix_hint"}.

Helper logic copied (not imported) from the repo's
gear_sonic/scripts/launch_pico_sonic_real.py:37-135 so the upstream repo
stays untouched and the console has zero runtime coupling to it.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import config

PATH_EXTRA = f"{Path.home()}/miniconda3/bin:{Path.home()}/.local/bin"


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = PATH_EXTRA + ":" + env.get("PATH", "")
    return env


def _which(name: str) -> str | None:
    return shutil.which(name, path=_env()["PATH"])


def _run(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=_env()
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def _check(id_: str, label: str, ok: bool, detail: str = "", fix_hint: str = "",
           warn_only: bool = False) -> dict:
    return {
        "id": id_, "label": label, "ok": ok,
        "level": "ok" if ok else ("warn" if warn_only else "fail"),
        "detail": detail, "fix_hint": fix_hint if not ok else "",
    }


# ── helpers copied from launch_pico_sonic_real.py ────────────────────────────

def is_lfs_pointer(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        head = path.read_bytes()[:128]
    except OSError:
        return False
    return head.startswith(b"version https://git-lfs.github.com/spec/v1")


def list_ipv4_interfaces() -> list[tuple[str, str]]:
    if shutil.which("ip"):
        proc = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        rows: list[tuple[str, str]] = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "inet":
                rows.append((parts[1], parts[3].split("/", 1)[0]))
        return rows
    return []


def find_real_robot_interface() -> tuple[str, str] | None:
    for iface, ip in list_ipv4_interfaces():
        if ip.startswith(config.ROBOT_SUBNET_PREFIX):
            return iface, ip
    return None


def lan_ips() -> list[tuple[str, str]]:
    """All reachable LAN IPs (iface, ip): non-loopback, non-docker/bridge.
    Includes the robot subnet — caller labels them. Used so the step-3 IP
    display and APK URL never dead-end just because WiFi flapped."""
    out = []
    for iface, ip in list_ipv4_interfaces():
        if iface == "lo" or ip.startswith("127."):
            continue
        if iface.startswith(("docker", "br-", "veth")):
            continue
        out.append((iface, ip))
    return out


def headset_host() -> str | None:
    """Preferred IP for the headset / APK download: a non-robot-subnet LAN IP
    (typically WiFi) if available, else any LAN IP, else None."""
    lans = lan_ips()
    for iface, ip in lans:
        if not ip.startswith(config.ROBOT_SUBNET_PREFIX):
            return ip
    return lans[0][1] if lans else None


def wifi_lan_ips() -> list[tuple[str, str]]:
    """Candidate IPs the PICO headset should connect to (non-loopback,
    non-robot-subnet, non-docker)."""
    out = []
    for iface, ip in list_ipv4_interfaces():
        if iface == "lo" or ip.startswith("127."):
            continue
        if ip.startswith(config.ROBOT_SUBNET_PREFIX):
            continue
        if iface.startswith(("docker", "br-", "veth")):
            continue
        out.append((iface, ip))
    return out


# ── individual checks ────────────────────────────────────────────────────────

def check_repo() -> dict:
    ok = config.REPO_ROOT.is_dir() and config.STREAMER_PY.is_file()
    return _check("repo", "GR00T-WholeBodyControl 仓库", ok,
                  str(config.REPO_ROOT),
                  "git clone https://github.com/NVlabs/GR00T-WholeBodyControl.git")


def check_git_lfs() -> dict:
    rc, out = _run(["git", "lfs", "version"])
    return _check("git_lfs", "git-lfs", rc == 0, out.splitlines()[0] if out else "",
                  "sudo apt install git-lfs")


def check_disk() -> dict:
    st = shutil.disk_usage(str(config.REPO_ROOT if config.REPO_ROOT.exists() else Path.home()))
    free_gb = st.free / 1e9
    return _check("disk", "磁盘空间 ≥10GB", free_gb >= 10, f"{free_gb:.1f} GB 可用",
                  "清理磁盘")


def check_cuda() -> dict:
    rc, out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    return _check("cuda", "NVIDIA GPU / 驱动", rc == 0, out if rc == 0 else "nvidia-smi 失败",
                  "检查 NVIDIA 驱动")


def check_models() -> dict:
    missing, pointers = [], []
    for f in config.DEPLOY_MODEL_FILES:
        if not f.is_file():
            missing.append(f.name)
        elif is_lfs_pointer(f):
            pointers.append(f.name)
    motion_ok = config.MOTION_DATA_DIR.is_dir() and any(config.MOTION_DATA_DIR.iterdir())
    ok = not missing and not pointers and motion_ok
    detail = "全部就绪" if ok else f"缺失: {missing} LFS指针: {pointers} motion_data: {motion_ok}"
    return _check("models", "部署模型 (policy/planner/motion)", ok, detail,
                  "cd repo && git lfs pull && python download_from_hf.py")


def check_deploy_binary() -> dict:
    ok = config.DEPLOY_BIN.is_file() and os.access(config.DEPLOY_BIN, os.X_OK)
    return _check("deploy_bin", "C++ 部署二进制", ok, str(config.DEPLOY_BIN),
                  "cd gear_sonic_deploy && bash deploy.sh ... (首次会自动编译)")


def check_venv() -> dict:
    if not config.VENV_PY.is_file():
        return _check("venv", ".venv_teleop 环境", False, "不存在",
                      "到步骤 2 点击安装（运行 install_pico.sh）")
    rc, out = _run([str(config.VENV_PY), "-c", "import zmq, msgpack, torch"], timeout=30)
    return _check("venv", ".venv_teleop 环境", rc == 0,
                  "zmq/msgpack/torch 可用" if rc == 0 else out[-300:],
                  "到步骤 2 重新安装")


def check_xrt_sdk() -> dict:
    if not config.VENV_PY.is_file():
        return _check("xrt", "xrobotoolkit_sdk", False, "venv 不存在", "先完成 venv 安装")
    rc, out = _run([str(config.VENV_PY), "-c", "import xrobotoolkit_sdk"], timeout=30)
    return _check("xrt", "xrobotoolkit_sdk", rc == 0,
                  "可导入" if rc == 0 else out[-300:], "到步骤 2 重新安装")


def check_pc_service_installed() -> dict:
    ok = config.SERVICE_SH.is_file()
    return _check("pc_service", "XRoboToolkit PC Service 已安装", ok,
                  str(config.SERVICE_SH) if ok else "/opt/apps/roboticsservice 不存在",
                  "到步骤 2 复制 sudo 安装命令")


def check_pc_service_running() -> dict:
    found = ""
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmdline = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "roboticsservice" in cmdline.lower() or "RoboticsService" in cmdline:
            found = f"pid {p.name}"
            break
    return _check("pc_service_run", "PC Service 运行中", bool(found), found,
                  "到步骤 3 点击「启动 PC Service」", warn_only=True)


def check_network() -> dict:
    robot = find_real_robot_interface()
    wifi = wifi_lan_ips()
    detail = (f"机器人网: {robot[0]}={robot[1]}" if robot else "机器人网(192.168.123.x): 无") + \
             " | 头显可连: " + (", ".join(f"{i}={ip}" for i, ip in wifi) if wifi else "无")
    return _check("network", "网络接口", bool(wifi), detail,
                  "连接 WiFi（头显与工作站需同网段）", warn_only=robot is not None)


def check_robot_ping() -> dict:
    rc, _ = _run(["ping", "-c", "1", "-W", "1", config.ROBOT_PC2_IP], timeout=5)
    return _check("robot_ping", f"G1 PC2 可达 ({config.ROBOT_PC2_IP})", rc == 0,
                  "ping 通" if rc == 0 else "ping 不通",
                  "检查网线/机器人开机状态", warn_only=True)


def check_tools() -> dict:
    just, uv = _which("just"), _which("uv")
    ok = bool(just)
    return _check("tools", "构建工具 (just/uv)", ok,
                  f"just={just or '缺'} uv={uv or '缺'}",
                  "curl -LsSf https://astral.sh/uv/install.sh | sh; cargo install just")


def check_port_free(port: int) -> dict:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        in_use = s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()
    return _check(f"port_{port}", f"端口 {port}", not in_use,
                  "被占用（若遥操作已在跑则正常）" if in_use else "空闲",
                  f"ss -tlnp | grep {port} 查看占用进程", warn_only=True)


# ── scopes ───────────────────────────────────────────────────────────────────

SCOPES = {
    "env": [check_repo, check_git_lfs, check_disk, check_cuda, check_models,
            check_deploy_binary, check_venv, check_xrt_sdk,
            check_pc_service_installed, check_network, check_tools],
    "pico": [check_pc_service_installed, check_pc_service_running, check_network],
    "sim": [check_venv, check_deploy_binary, check_models,
            lambda: check_port_free(config.ZMQ_PUB_PORT),
            lambda: check_port_free(config.ZMQ_FEEDBACK_PORT)],
    "real": [check_deploy_binary, check_models, check_network, check_robot_ping],
}


def run_all_checks(scope: str) -> list[dict]:
    fns = SCOPES.get(scope, SCOPES["env"])
    return [fn() for fn in fns]
