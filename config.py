"""Single source of truth: paths, ports, stdout marker regexes.

All facts here were verified against the live checkout on 2026-06-08:
  - deploy binary:   gear_sonic_deploy/target/release/g1_deploy_onnx_ref
  - "Init Done":     g1_deploy_onnx_ref.cpp:2754  (capital D)
  - CONTROL switch:  g1_deploy_onnx_ref.cpp:3831
  - streamer prints: pico_manager_thread_server.py:1839 / 2038 / 1692
"""
from __future__ import annotations

import re
from pathlib import Path

CONSOLE_ROOT = Path(__file__).resolve().parent

REPO_ROOT = Path(
    "/home/ubuntu/gear_sonic_elbow_crawling_isaacsim/GR00T-WholeBodyControl"
)
VENV = REPO_ROOT / ".venv_teleop"
VENV_PY = VENV / "bin" / "python"
DEPLOY_DIR = REPO_ROOT / "gear_sonic_deploy"
DEPLOY_BIN = DEPLOY_DIR / "target" / "release" / "g1_deploy_onnx_ref"
SERVICE_SH = Path("/opt/apps/roboticsservice/runService.sh")
INSTALL_PICO_SH = REPO_ROOT / "install_scripts" / "install_pico.sh"
STREAMER_PY = REPO_ROOT / "gear_sonic" / "scripts" / "pico_manager_thread_server.py"
SIM_LOOP_PY = REPO_ROOT / "gear_sonic" / "scripts" / "run_sim_loop.py"
# Revo2 hand overlay: same sim_loop but monkeypatches the wbc yaml to load the
# BrainCo Revo2 combined MJCF (see overlay/). Selected via hand_mode = "revo2".
SIM_LOOP_REVO2_PY = CONSOLE_ROOT / "overlay" / "run_sim_loop_revo2.py"
PROBE_PY = CONSOLE_ROOT / "helpers" / "pico_probe.py"
# Real-G1 high-level mode switch (release for lowcmd takeover / re-select).
G1_MODE_PY = CONSOLE_ROOT / "helpers" / "g1_mode.py"
# Verify the robot's motors actually execute rt/lowcmd (controller debug mode done)
VERIFY_MOTORS_PY = CONSOLE_ROOT / "helpers" / "verify_motor_handover.py"
# Hand self-test: drive both BrainCo hands through each motor group, check encoder
HAND_SELFTEST_PY = CONSOLE_ROOT / "helpers" / "hand_selftest.py"
# Quick health probe: is the PC2 hand server publishing rt/brainco state?
BRAINCO_PROBE_PY = CONSOLE_ROOT / "helpers" / "brainco_probe.py"
# Real BrainCo hand bridge: streamer grasp -> rt/brainco/{l,r}/cmd (deploy only
# drives the body; the real hands live on rt/brainco). Needs PC2 brainco server.
BRAINCO_BRIDGE_PY = CONSOLE_ROOT / "helpers" / "brainco_hand_bridge.py"
# GUI manual drive: replaces the streamer on 5556, drives the deploy's planner
# (gait + move + facing) from console buttons — no PICO. Mutually exclusive with
# pico_streamer (both bind ZMQ 5556).
GUI_DRIVE_PY = CONSOLE_ROOT / "helpers" / "gui_drive.py"

# Teleop episode recorder (imitation-learning / StageAct data collection): records
# synced (obs, action, stage) frames while you PICO-teleop a task. Subscribes to
# the streamer pose (5556), PC2 head camera colour/depth, and DDS proprio/hands.
RECORDER_PY = CONSOLE_ROOT / "helpers" / "episode_recorder.py"
RECORDINGS_DIR = CONSOLE_ROOT / "recordings"

# Episode playback: replays a recorded episode's teleop command on 5556 back
# through the running SONIC deploy (PICO-output-level). Binds ZMQ 5556 -> mutually
# exclusive with pico_streamer / gui_drive. Drives the REAL robot -> safety-gated.
PLAYBACK_PY = CONSOLE_ROOT / "helpers" / "playback_pico.py"

# ACT training (LeRobot): dedicated `act_train` conda env (torch cu128 + lerobot
# v0.4.4). build_act_dataset.py turns CURATED recordings -> a LeRobot dataset
# (in HF_LEROBOT_HOME), then lerobot trains an ACT policy on it.
ACT_TRAIN_VENV_PY = Path.home() / "miniconda3" / "envs" / "act_train" / "bin" / "python"
BUILD_DATASET_PY = CONSOLE_ROOT / "helpers" / "build_act_dataset.py"
TRAINING_DIR = CONSOLE_ROOT / "training"   # ACT checkpoints / outputs

STATE_FILE = CONSOLE_ROOT / "state" / "state.json"
LOGS_DIR = CONSOLE_ROOT / "logs"
STATIC_DIR = CONSOLE_ROOT / "static"

GUI_PORT = 8090  # 8088 = old pico_sonic_gui, 18080 = A/B sim-real platform
ZMQ_PUB_PORT = 5556
ZMQ_FEEDBACK_PORT = 5557

ROBOT_SUBNET_PREFIX = "192.168.123."
ROBOT_PC2_IP = "192.168.123.164"

DEB_URL_2404 = (
    "https://github.com/XR-Robotics/XRoboToolkit-PC-Service/releases/download/"
    "v1.0.0/XRoboToolkit_PC_Service_1.0.0_ubuntu_24.04_amd64.deb"
)
APK_URL = (
    "https://github.com/XR-Robotics/XRoboToolkit-Unity-Client/releases/download/"
    "v1.1.1/XRoboToolkit-PICO-1.1.1.apk"
)

# Files that must exist and not be git-lfs pointers for deployment to work.
DEPLOY_MODEL_FILES = [
    DEPLOY_DIR / "policy" / "release" / "model_encoder.onnx",
    DEPLOY_DIR / "policy" / "release" / "model_decoder.onnx",
    DEPLOY_DIR / "policy" / "release" / "observation_config.yaml",
    DEPLOY_DIR / "planner" / "target_vel" / "V2" / "planner_sonic.onnx",
]
MOTION_DATA_DIR = DEPLOY_DIR / "reference" / "example"

# ── stdout markers (verified) ────────────────────────────────────────────────
MARKERS = {
    # deploy (C++ via deploy.sh)
    "deploy_ready": re.compile(r"Init Done"),
    "deploy_control": re.compile(r"transitioning to CONTROL state"),
    "deploy_estop_ack": re.compile(r"EMERGENCY STOP"),
    "deploy_waiting_lowstate": re.compile(r"LowState is not available"),
    # pico streamer
    "streamer_ready": re.compile(r"\[Manager\] ZMQ socket bound to port (\d+)"),
    "streamer_waiting_body": re.compile(r"[Ww]aiting for body (tracking )?data"),
    "stream_mode": re.compile(r"\[Manager\] StreamMode switch: (\w+) -> (\w+)"),
    "loco_mode": re.compile(r"\[PlannerLoop\] Mode -> (\d+): (\w+)"),
}

LOCOMOTION_MODE_NAMES = {
    0: "Idle", 1: "Slow Walk", 2: "Walk", 3: "Run", 4: "Squat",
    5: "Kneel (two legs)", 6: "Kneel", 7: "Lying face-down", 8: "Crawling",
    9: "Boxing Idle", 10: "Boxing Walk", 11: "Boxing Punch L", 12: "Boxing Punch R",
    13: "Boxing Hook L", 14: "Boxing Hook R", 15: "Boxing Combo A",
    16: "Boxing Combo B", 17: "Forward Jump", 18: "Stealth Walk", 19: "Injured Walk",
}

SAFETY_TOKEN_TTL_S = 120
SAFETY_TYPED_PHRASES = ("确认姿态", "CONFIRM")

# ── deploy build/run environment ─────────────────────────────────────────────
# The repo's prebuilt binary was compiled with HAS_ROS2=1 (auto-detected ROS
# jazzy), which dragged in jazzy's libddsc and crashed at startup
# (free(): invalid pointer) due to an ABI clash with unitree_sdk2's libddsc.
# We rebuild with HAS_ROS2=0 so the binary links ONLY unitree's libddsc.
# These env vars reconstruct the original interactive build environment
# (paths recovered from the working binary's ldd + filesystem search).
# deploy.sh "always builds" (just build) and sources setup_env.sh, which
# honors HAS_ROS2=0 (skips sourcing ROS jazzy entirely).
_HOME = Path.home()
DEPLOY_ENV = {
    "HAS_ROS2": "0",
    # tensorrt-10.13.3 supports the RTX 5080 (Blackwell sm_120); 10.2.0 does
    # not (Unsupported SM: 0xc00) and its _win cask lib breaks engine builds.
    "TensorRT_ROOT": str(_HOME / ".local/opt/tensorrt-10.13.3"),
    "onnxruntime_ROOT": str(_HOME / ".local/opt/onnxruntime-1.22.0"),
    "CUDA_HOME": str(_HOME / ".local/opt/cuda-runtime-12.6"),
    "CUDAToolkit_ROOT": str(_HOME / ".local/opt/cuda-runtime-12.6"),
    "MSGPACK_ROOT": str(_HOME / ".local/opt/msgpack-cxx/usr"),
    "PATH": f"{_HOME}/miniconda3/bin:{_HOME}/.local/bin:" + __import__("os").environ.get("PATH", ""),
}
# HAS_ROS2=0 build has no ROS2 output channel; teleop only needs the zmq one.
DEPLOY_OUTPUT_TYPE = "zmq"
