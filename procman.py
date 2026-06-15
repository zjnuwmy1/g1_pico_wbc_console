"""Process manager for the teleop chain.

Stop-policy classes (user hard constraint: NEVER SIGKILL a real-robot deploy —
history of arm_sdk lockups on this robot):

  profile "sim"     SIGINT(pgid) -> 8s -> SIGTERM(pgid) -> 8s -> SIGKILL(pgid)
  profile "py"      SIGINT(proc only) -> 8s -> SIGTERM(proc only)
                    (proc-only so a spawned XRoboToolkit PC service survives)
  profile "deploy"  stdin 'o' (official E-stop, clean self-exit) -> 5s
                    -> SIGINT(pgid) -> 10s -> SIGTERM(pgid) -> NEVER SIGKILL;
                    if still alive: status "needs_manual" with PID surfaced.
"""
from __future__ import annotations

import datetime
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import config


class RingLog:
    """Sequence-numbered ring buffer of log lines (adapted from pico_sonic_gui)."""

    def __init__(self, max_lines: int = 2000):
        self._lines: deque[tuple[int, str]] = deque(maxlen=max_lines)
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, line: str) -> int:
        with self._lock:
            self._seq += 1
            self._lines.append((self._seq, line))
            return self._seq

    def since(self, seq: int, limit: int = 400) -> tuple[int, list[str]]:
        with self._lock:
            out = [(s, l) for s, l in self._lines if s > seq][:limit]
            last = out[-1][0] if out else max(seq, self._seq if not self._lines else seq)
            if not out:
                last = seq
            return last, [l for _, l in out]


@dataclass
class ProcSpec:
    name: str
    cmd: list[str]
    cwd: Path
    profile: str  # "sim" | "py" | "deploy"
    ready_regex: re.Pattern | None = None
    stdin_on_start: bytes | None = None
    env_extra: dict[str, str] = field(default_factory=dict)
    # name -> regex; matches recorded into proc.parsed
    parse_regexes: dict[str, re.Pattern] = field(default_factory=dict)


class Proc:
    def __init__(self, spec: ProcSpec, logfile: Path):
        self.spec = spec
        self.log = RingLog()
        self.logfile = logfile
        self.popen: subprocess.Popen | None = None
        self.status = "idle"  # idle|starting|ready|stopping|exited|failed|needs_manual
        self.returncode: int | None = None
        self.parsed: dict[str, object] = {}
        self.started_at: float | None = None
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> dict:
        with self._lock:
            if self.popen is not None and self.popen.poll() is None:
                return {"ok": False, "error": f"{self.spec.name} already running"}
            env = os.environ.copy()
            env.update(self.spec.env_extra)
            # Live logs: Python child procs block-buffer stdout when piped, so
            # the GUI log lags far behind reality. Force line-buffered/unbuffered.
            env["PYTHONUNBUFFERED"] = "1"
            # MuJoCo viewer / PyVista windows need X; fall back to the local
            # desktop session when the console was launched without DISPLAY.
            env.setdefault("DISPLAY", ":1")
            env.setdefault("XAUTHORITY", "/run/user/1000/gdm/Xauthority")
            self.logfile.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.popen = subprocess.Popen(
                    self.spec.cmd,
                    cwd=str(self.spec.cwd),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as e:
                self.status = "failed"
                self.log.append(f"[console] spawn failed: {e}")
                return {"ok": False, "error": str(e)}
            self.status = "starting" if self.spec.ready_regex else "ready"
            self.returncode = None
            self.parsed = {}
            self.started_at = time.time()
            threading.Thread(target=self._reader, daemon=True).start()
            if self.spec.stdin_on_start:
                self.write_stdin(self.spec.stdin_on_start)
            self.log.append(f"[console] started pid={self.popen.pid}: {' '.join(self.spec.cmd)}")
            return {"ok": True, "pid": self.popen.pid}

    def _reader(self) -> None:
        p = self.popen
        assert p is not None and p.stdout is not None
        with open(self.logfile, "ab") as f:
            for raw in iter(p.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                self.log.append(line)
                f.write(raw)
                f.flush()
                self._match(line)
        rc = p.wait()
        self.returncode = rc
        if self.status not in ("stopping", "needs_manual"):
            self.status = "exited" if rc == 0 else "failed"
        else:
            self.status = "exited"
        self.log.append(f"[console] process exited rc={rc}")

    def _match(self, line: str) -> None:
        spec = self.spec
        if spec.ready_regex and self.status == "starting" and spec.ready_regex.search(line):
            self.status = "ready"
        for key, rx in spec.parse_regexes.items():
            m = rx.search(line)
            if m:
                self.parsed[key] = m.groups() if m.groups() else True
                self.parsed[f"{key}_ts"] = time.time()

    def write_stdin(self, data: bytes) -> bool:
        p = self.popen
        if p is None or p.poll() is not None or p.stdin is None:
            return False
        try:
            p.stdin.write(data)
            p.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def alive(self) -> bool:
        return self.popen is not None and self.popen.poll() is None

    # ── stopping ─────────────────────────────────────────────────────────────
    def stop(self) -> dict:
        if not self.alive():
            return {"ok": True, "note": "not running"}
        self.status = "stopping"
        threading.Thread(target=self._stop_ladder, daemon=True).start()
        return {"ok": True}

    def _signal(self, sig: int, group: bool) -> None:
        p = self.popen
        if p is None or p.poll() is not None:
            return
        try:
            if group:
                os.killpg(os.getpgid(p.pid), sig)
            else:
                p.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _wait_dead(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive():
                return True
            time.sleep(0.2)
        return not self.alive()

    def _group_alive(self) -> bool:
        p = self.popen
        if p is None:
            return False
        try:
            os.killpg(p.pid, 0)  # pgid == leader pid (start_new_session)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _reap_group(self, *sigs: int, label: str = "") -> None:
        """Main proc is dead but group children may linger (e.g. bash bg jobs
        ignore SIGINT). Walk the remaining signals until the group is gone."""
        for sig in sigs:
            if not self._group_alive():
                return
            self.log.append(f"[console] reaping leftover group children with {label or sig}")
            try:
                os.killpg(self.popen.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(1.0)

    def _stop_ladder(self) -> None:
        prof = self.spec.profile
        if prof == "deploy":
            # Official E-stop char first: clean self-exit, motors released properly.
            self.log.append("[console] graceful stop: sending 'o' (official E-stop)")
            self.write_stdin(b"o")
            if self._wait_dead(5):
                self._reap_group(signal.SIGTERM, label="SIGTERM")  # never SIGKILL
                return
            self.log.append("[console] still alive, SIGINT(pgid)")
            self._signal(signal.SIGINT, group=True)
            if self._wait_dead(10):
                self._reap_group(signal.SIGTERM, label="SIGTERM")
                return
            self.log.append("[console] still alive, SIGTERM(pgid)")
            self._signal(signal.SIGTERM, group=True)
            if self._wait_dead(15):
                return
            # NEVER SIGKILL a deploy process (arm_sdk lockup history).
            self.status = "needs_manual"
            pid = self.popen.pid if self.popen else "?"
            self.log.append(
                f"[console] STILL ALIVE after SIGTERM — manual intervention required, pid={pid}. "
                "DO NOT kill -9; check robot state first."
            )
        elif prof == "sim":
            self._signal(signal.SIGINT, group=True)
            if self._wait_dead(8):
                self._reap_group(signal.SIGTERM, signal.SIGKILL, label="SIGTERM→SIGKILL")
                return
            self._signal(signal.SIGTERM, group=True)
            if self._wait_dead(8):
                self._reap_group(signal.SIGKILL, label="SIGKILL")
                return
            self.log.append("[console] escalating to SIGKILL(pgid) (sim only)")
            self._signal(signal.SIGKILL, group=True)
            self._wait_dead(5)
        else:  # "py" — signal the process only so child PC service survives
            self._signal(signal.SIGINT, group=False)
            if self._wait_dead(8):
                return
            self._signal(signal.SIGTERM, group=False)
            self._wait_dead(8)

    def snapshot(self) -> dict:
        status = self.status
        # Reader thread can stay blocked on the pipe if an orphaned child holds
        # it open; derive the effective status from the actual process state.
        if status == "stopping" and not self.alive():
            status = "exited"
        return {
            "name": self.spec.name,
            "status": status,
            "alive": self.alive(),
            "pid": self.popen.pid if self.popen else None,
            "returncode": self.returncode,
            "parsed": {k: v for k, v in self.parsed.items() if not k.endswith("_ts")},
            "started_at": self.started_at,
        }


def _streamer_cmd(vis: bool) -> list[str]:
    cmd = [str(config.VENV_PY), str(config.STREAMER_PY), "--manager"]
    if vis:
        cmd += ["--vis_vr3pt", "--vis_smpl"]
    return cmd


class ProcessManager:
    def __init__(self) -> None:
        self.run_dir = config.LOGS_DIR / datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self._procs: dict[str, Proc] = {}
        self._lock = threading.Lock()
        # Hand model for the sim: "dex3" (upstream) or "revo2" (BrainCo overlay).
        self.hand_mode: str = "dex3"

    # ── specs ────────────────────────────────────────────────────────────────
    def _spec(self, name: str, vis: bool = False) -> ProcSpec:
        M = config.MARKERS
        deploy_parse = {
            "ready_marker": M["deploy_ready"],
            "control": M["deploy_control"],
            "estop_ack": M["deploy_estop_ack"],
            "waiting_lowstate": M["deploy_waiting_lowstate"],
        }
        if name == "sim_loop":
            script = (config.SIM_LOOP_REVO2_PY if self.hand_mode == "revo2"
                      else config.SIM_LOOP_PY)
            return ProcSpec(
                "sim_loop", [str(config.VENV_PY), str(script)],
                cwd=config.REPO_ROOT, profile="sim",
            )
        if name == "deploy_sim":
            return ProcSpec(
                "deploy_sim",
                ["bash", "deploy.sh", "--input-type", "zmq_manager",
                 "--output-type", config.DEPLOY_OUTPUT_TYPE, "sim"],
                cwd=config.DEPLOY_DIR, profile="deploy",
                ready_regex=M["deploy_ready"], stdin_on_start=b"y\n",
                env_extra=config.DEPLOY_ENV, parse_regexes=deploy_parse,
            )
        if name == "deploy_real":
            return ProcSpec(
                "deploy_real",
                ["bash", "deploy.sh", "--input-type", "zmq_manager",
                 "--output-type", config.DEPLOY_OUTPUT_TYPE, "real"],
                cwd=config.DEPLOY_DIR, profile="deploy",
                ready_regex=M["deploy_ready"], stdin_on_start=b"y\n",
                env_extra=config.DEPLOY_ENV, parse_regexes=deploy_parse,
            )
        if name == "pico_streamer":
            return ProcSpec(
                "pico_streamer", _streamer_cmd(vis),
                cwd=config.REPO_ROOT, profile="py",
                ready_regex=M["streamer_ready"],
                parse_regexes={
                    "stream_mode": M["stream_mode"],
                    "loco_mode": M["loco_mode"],
                    "waiting_body": M["streamer_waiting_body"],
                },
            )
        if name == "brainco_hand":
            import checks as _checks
            robot = _checks.find_real_robot_interface()
            iface = robot[0] if robot else "enp132s0"
            return ProcSpec(
                "brainco_hand",
                [str(config.VENV_PY), str(config.BRAINCO_BRIDGE_PY), iface],
                cwd=config.REPO_ROOT, profile="py",
            )
        if name == "gui_drive":
            return ProcSpec(
                "gui_drive", [str(config.VENV_PY), str(config.GUI_DRIVE_PY)],
                cwd=config.REPO_ROOT, profile="py",
            )
        if name == "install_pico":
            return ProcSpec(
                "install_pico", ["bash", str(config.INSTALL_PICO_SH)],
                cwd=config.REPO_ROOT, profile="py",
            )
        if name == "pc_service":
            return ProcSpec(
                "pc_service", ["bash", str(config.SERVICE_SH)],
                cwd=Path.home(), profile="py",
            )
        raise KeyError(name)

    KNOWN = ("sim_loop", "deploy_sim", "deploy_real", "pico_streamer", "brainco_hand", "gui_drive", "install_pico", "pc_service")

    # ── API ──────────────────────────────────────────────────────────────────
    def get(self, name: str) -> Proc | None:
        return self._procs.get(name)

    def start(self, name: str, vis: bool = False) -> dict:
        if name not in self.KNOWN:
            return {"ok": False, "error": f"unknown process {name}"}
        if name == "deploy_real":
            for other in ("sim_loop", "deploy_sim"):
                p = self._procs.get(other)
                if p and p.alive():
                    return {"ok": False,
                            "error": f"先停止 {other} — sim 和真机进程不能同时运行（官方文档要求）"}
        if name in ("deploy_sim",) :
            p = self._procs.get("deploy_real")
            if p and p.alive():
                return {"ok": False, "error": "deploy_real 还在运行，先优雅停止它"}
        # gui_drive and pico_streamer both bind ZMQ 5556 -> mutually exclusive.
        if name in ("gui_drive", "pico_streamer"):
            other = "pico_streamer" if name == "gui_drive" else "gui_drive"
            p = self._procs.get(other)
            if p and p.alive():
                return {"ok": False,
                        "error": f"先停止 {other} — 它和 {name} 都占用 ZMQ 5556，不能同时跑"}
        with self._lock:
            existing = self._procs.get(name)
            if existing and existing.alive():
                return {"ok": False, "error": f"{name} already running"}
            proc = Proc(self._spec(name, vis=vis), self.run_dir / f"{name}.log")
            self._procs[name] = proc
        return proc.start()

    def stop(self, name: str) -> dict:
        p = self._procs.get(name)
        if p is None:
            return {"ok": True, "note": "never started"}
        return p.stop()

    def write_stdin(self, name: str, data: bytes) -> dict:
        p = self._procs.get(name)
        if p is None or not p.alive():
            return {"ok": False, "error": f"{name} not running"}
        return {"ok": p.write_stdin(data)}

    def estop(self) -> dict:
        """Global E-stop: deploy stdin 'o' FIRST (stops the policy), then streamer."""
        actions = []
        for name in ("deploy_real", "deploy_sim"):
            p = self._procs.get(name)
            if p and p.alive():
                ok = p.write_stdin(b"o")
                actions.append(f"{name}: sent 'o' ({'ok' if ok else 'FAILED'})")
                p.status = "stopping"
                threading.Thread(target=p._stop_ladder, daemon=True).start()
        s = self._procs.get("pico_streamer")
        if s and s.alive():
            s.stop()
            actions.append("pico_streamer: SIGINT")
        if not actions:
            actions.append("nothing running")
        return {"ok": True, "actions": actions}

    def snapshot(self) -> dict:
        return {n: p.snapshot() for n, p in self._procs.items()}

    def set_hand_mode(self, mode: str) -> dict:
        if mode not in ("dex3", "revo2"):
            return {"ok": False, "error": f"unknown hand_mode {mode!r}"}
        # The sim_loop script is chosen at launch, so block switching while it runs.
        if self.any_alive(("sim_loop", "deploy_sim", "deploy_real")):
            return {"ok": False, "error": "先停掉 sim / deploy 再切换手型"}
        self.hand_mode = mode
        return {"ok": True, "hand_mode": mode}

    def logs_since(self, name: str, seq: int) -> dict:
        p = self._procs.get(name)
        if p is None:
            return {"seq": seq, "lines": []}
        last, lines = p.log.since(seq)
        return {"seq": last, "lines": lines}

    def any_alive(self, names: tuple[str, ...]) -> bool:
        return any((p := self._procs.get(n)) and p.alive() for n in names)

    def shutdown_all(self) -> None:
        """Graceful shutdown on server exit: deploys first, then the rest."""
        for name in ("deploy_real", "deploy_sim"):
            p = self._procs.get(name)
            if p and p.alive():
                p.stop()
        # give deploys a head start before tearing down sim/streamer
        time.sleep(1.0)
        for name in ("pico_streamer", "sim_loop", "install_pico"):
            p = self._procs.get(name)
            if p and p.alive():
                p.stop()
        deadline = time.time() + 20
        while time.time() < deadline:
            if not any(p.alive() for p in self._procs.values()):
                break
            time.sleep(0.5)
