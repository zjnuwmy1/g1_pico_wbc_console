#!/usr/bin/env python3
"""Console-owned launcher: run the gear_sonic MuJoCo sim_loop with the BrainCo
Revo2 hand OVERLAY, without modifying any upstream file.

How it stays off the upstream:
  * load_wbc_yaml() hardcodes the wbc yaml path inside the gear_sonic package,
    so we monkeypatch it to load our overlay yaml instead. The overlay yaml sets
    ROBOT_SCENE -> the Revo2 combined MJCF and NUM_HAND_MOTORS/JOINTS -> 6.
  * The Revo2 MJCF renames its 6 actuated finger joints to contain "_hand_" so
    base_sim's substring joint discovery finds exactly 6/hand (no base_sim patch).

Run exactly like the console runs run_sim_loop.py (no args, cwd = repo root).
"""
import os
import yaml

OVERLAY_YAML = os.path.expanduser(
    "~/Desktop/g1_pico_wbc_console/overlay/g1_revo2/wbc_g1_revo2.yaml"
)

from gear_sonic.utils.mujoco_sim import configs

_override = configs.override_wbc_config


def _patched_load_wbc_yaml(self):
    with open(OVERLAY_YAML) as f:
        wbc = yaml.load(f, Loader=yaml.FullLoader)
    # Trim Dex3-sized (7/hand) effort array to Revo2 (6/hand). CRITICAL: the list
    # is in JOINT-TREE order [legs+waist+left_arm(22) | LEFT HAND 7 | right_arm 7 |
    # RIGHT HAND 7], NOT grouped body-then-hands. So drop the 7th joint of each
    # hand: index 28 (left hand) and 42 (right hand). Getting this wrong shifts
    # the right-arm efforts (right_shoulder_pitch ends up with a ~0.7 Nm finger
    # limit) -> the right arm is badly underpowered and can't track.
    eff = wbc.get("motor_effort_limit_list")
    if isinstance(eff, list) and len(eff) == 43:
        wbc["motor_effort_limit_list"] = eff[:28] + eff[29:42]
    print(f"[revo2-overlay] loaded {OVERLAY_YAML} "
          f"(ROBOT_SCENE={wbc.get('ROBOT_SCENE')}, "
          f"NUM_HAND_JOINTS={wbc.get('NUM_HAND_JOINTS')}, "
          f"effort_len={len(wbc.get('motor_effort_limit_list', []))})")
    return _override(wbc, self)


configs.BaseConfig.load_wbc_yaml = _patched_load_wbc_yaml


# ── Stage-2 hand mapping (symmetric streamer signal) ─────────────────────────
# The C++ deploy republishes the hand command to rt/dex3/cmd ASYMMETRICALLY
# (left ends up ~6x weaker than right) even though the streamer feeds it
# symmetric, full hand joints. So bypass the deploy's hand channel entirely:
# subscribe to the streamer's ZMQ PUB (5556, "pose" topic) and read its
# left_hand_joints / right_hand_joints (7-DOF Dex3 gripper-IK output, symmetric),
# collapse each into a closedness in [0,1], and curl the Revo2 fingers.
# Set WBC_REVO2_RAWHAND=1 to fall back to the raw (asymmetric) deploy command.
import os as _os
import json as _json
import struct as _struct
import threading as _threading
if _os.environ.get("WBC_REVO2_RAWHAND") != "1":
    import numpy as _np
    import zmq as _zmq
    from gear_sonic.utils.mujoco_sim import base_sim as _bs

    _HDR = 1280
    _CLOSE_NORM = 0.9  # mean(|streamer 7-DOF joints|) at full close ~0.91
    _STALE_S = 1.0     # no pose msg for this long -> treat input as lost (open)
    _stream_hands = {"L": None, "R": None}
    _stream_rx = {"t": 0.0}
    _SZ = {"f32": 4, "f64": 8, "i32": 4, "i64": 8, "bool": 1}

    def _parse_pose(msg):
        if msg[:4] != b"pose":
            return
        body = msg[4:]
        try:
            h = _json.loads(body[:_HDR].rstrip(b"\x00"))
        except Exception:
            return
        off = 0
        pay = body[_HDR:]
        found = False
        for f in h.get("fields", []):
            n = 1
            for d in (f.get("shape") or [1]):
                n *= int(d)
            sz = _SZ.get(f["dtype"], 4)
            if f["name"] in ("left_hand_joints", "right_hand_joints") and off + sz * n <= len(pay):
                vals = list(_struct.unpack("<" + "f" * n, pay[off:off + sz * n]))
                _stream_hands["L" if f["name"][0] == "l" else "R"] = vals
                found = True
            off += sz * n
        if found:
            import time as _time
            _stream_rx["t"] = _time.monotonic()

    def _zmq_reader():
        ctx = _zmq.Context()
        s = ctx.socket(_zmq.SUB)
        s.connect("tcp://localhost:5556")
        s.setsockopt(_zmq.SUBSCRIBE, b"pose")
        s.setsockopt(_zmq.RCVTIMEO, 1000)
        while True:
            try:
                _parse_pose(s.recv())
            except Exception:
                pass

    _threading.Thread(target=_zmq_reader, daemon=True).start()

    def _closedness(side):
        import time as _time
        j = _stream_hands.get(side)
        # stale input (streamer stopped / PLANNER mode) -> target open, the PD
        # eases the fingers there instead of freezing at the last grasp.
        if not j or (_time.monotonic() - _stream_rx["t"]) > _STALE_S:
            return 0.0
        return float(min(1.0, max(0.0, (sum(abs(x) for x in j) / len(j)) / _CLOSE_NORM)))

    def _stream_hand_torques(self):
        KP, KD = 3.0, 0.12
        tq = _np.zeros(self.num_hand_dof * 2)
        cl = {"L": _closedness("L"), "R": _closedness("R")}
        for hand_index, side, off in (
            (self.left_hand_index, "L", 0),
            (self.right_hand_index, "R", self.num_hand_dof),
        ):
            c = cl[side]  # symmetric streamer signal; hold open (0) until first msg
            for k, ji in enumerate(hand_index):
                lo, hi = self.mj_model.jnt_range[ji]
                target = lo + c * (hi - lo)
                q = self.mj_data.qpos[ji + self.qpos_offset - 1]
                dq = self.mj_data.qvel[ji + self.qvel_offset - 1]
                tq[off + k] = KP * (target - q) - KD * dq
        n = getattr(self, "_revo2_dbg", 0) + 1
        self._revo2_dbg = n
        if n % 100 == 0:
            print(f"[revo2-hand] (stream) closedness L={cl['L']:.2f} R={cl['R']:.2f} | "
                  f"L={_stream_hands['L']} R={_stream_hands['R']}", flush=True)
        return tq

    _bs.DefaultEnv.compute_hand_torques = _stream_hand_torques
    print("[revo2-overlay] hand STREAM mapping active (symmetric streamer signal -> Revo2 curl)")

if __name__ == "__main__":
    from gear_sonic.scripts import run_sim_loop
    run_sim_loop.main(run_sim_loop.ArgsConfig())
