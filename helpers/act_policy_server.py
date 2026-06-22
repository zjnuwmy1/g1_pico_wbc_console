#!/usr/bin/env python3
"""ACT policy inference server — runs in the act_train conda env (torch + lerobot
+ CUDA). A thin stdlib-socket wrapper so the DDS/teleop side (act_infer.py, which
lives in the teleop venv and owns unitree_sdk2py + the camera) can get actions
WITHOUT importing torch. Keeping torch out of the DDS process also keeps the
fragile cyclonedds setup untouched.

Wire format (127.0.0.1:PORT, length-prefixed frames):
  frame   = [4B body_len][body]
  body    = [4B hdr_len][hdr json][raw payload]
  request hdr {cmd:"predict", state_len:N, images:[{key,shape:[H,W,3]}]}
          payload = state(float32*N) ++ each image(uint8 H*W*3)
          hdr {cmd:"reset"}  -> start of a new episode (clears ACT action queue)
          hdr {cmd:"ping"}   -> {ok, images:[...]} so the client learns rgb/rgbd
  reply   predict -> hdr {ok, action_len:102}, payload = action float32*102

The model was validated to need torch float CHW [0,1] tensors (uint8 overflows
the normalizer; numpy stays on cpu) — we build exactly that here.

Usage: act_policy_server.py <checkpoint_dir> [--port 5560] [--device cuda]
       <checkpoint_dir> may be the run dir, a checkpoints/<step> dir, or the
       pretrained_model dir itself — we resolve down to pretrained_model.
"""
import argparse
import json
import socket
import struct
import sys
from pathlib import Path

import numpy as np
import torch
from lerobot.policies.act.modeling_act import ACTPolicy
from lerobot.policies.factory import make_pre_post_processors


def _recv_n(sock, n):
    buf = bytearray()
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            return None
        buf += c
    return bytes(buf)


def _recv_frame(sock):
    h = _recv_n(sock, 4)
    if not h:
        return None
    (ln,) = struct.unpack(">I", h)
    body = _recv_n(sock, ln)
    if body is None:
        return None
    (hl,) = struct.unpack(">I", body[:4])
    hdr = json.loads(body[4:4 + hl].decode())
    return hdr, body[4 + hl:]


def _send_frame(sock, hdr, payload=b""):
    hb = json.dumps(hdr).encode()
    body = struct.pack(">I", len(hb)) + hb + payload
    sock.sendall(struct.pack(">I", len(body)) + body)


def _resolve_ckpt(p: Path) -> Path:
    p = Path(p)
    if (p / "config.json").is_file():
        return p
    if (p / "pretrained_model" / "config.json").is_file():
        return p / "pretrained_model"
    last = p / "checkpoints" / "last" / "pretrained_model"
    if (last / "config.json").is_file():
        return last
    raise SystemExit(f"no pretrained_model/config.json under {p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("checkpoint")
    ap.add_argument("--port", type=int, default=5560)
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()
    dev = "cuda" if (args.device != "cpu" and torch.cuda.is_available()) else "cpu"

    ck = _resolve_ckpt(args.checkpoint)
    print(f"[policy] loading {ck} on {dev}", flush=True)
    policy = ACTPolicy.from_pretrained(str(ck))
    policy.to(dev)
    policy.eval()
    pre, post = make_pre_post_processors(
        policy_cfg=policy.config, pretrained_path=str(ck),
        preprocessor_overrides={"device_processor": {"device": dev}})
    img_keys = [k for k in policy.config.input_features if k.startswith("observation.images")]
    state_dim = policy.config.input_features["observation.state"].shape[0]
    print(f"[policy] READY images={img_keys} state_dim={state_dim} "
          f"action={policy.config.output_features['action'].shape[0]}", flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(1)
    print(f"[policy] listening 127.0.0.1:{args.port}", flush=True)

    while True:
        conn, _ = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print("[policy] client connected", flush=True)
        try:
            while True:
                fr = _recv_frame(conn)
                if fr is None:
                    break
                hdr, payload = fr
                cmd = hdr.get("cmd")
                if cmd == "reset":
                    policy.reset()
                    _send_frame(conn, {"ok": True})
                    continue
                if cmd == "ping":
                    _send_frame(conn, {"ok": True, "images": img_keys, "state_dim": state_dim,
                                       "action_dim": int(policy.config.output_features["action"].shape[0])})
                    continue
                if cmd != "predict":
                    _send_frame(conn, {"ok": False, "error": "bad cmd"})
                    continue
                off = 0
                sl = int(hdr["state_len"])
                state = np.frombuffer(payload, np.float32, sl, off).copy()
                off += sl * 4
                obs = {"observation.state": torch.from_numpy(state)}
                for im in hdr["images"]:
                    H, W, C = im["shape"]
                    n = H * W * C
                    arr = np.frombuffer(payload, np.uint8, n, off).reshape(H, W, C)
                    off += n
                    t = torch.from_numpy(arr.astype(np.float32) / 255.0).permute(2, 0, 1)
                    obs[im["key"]] = t
                with torch.inference_mode():
                    a = post(policy.select_action(pre(obs)))
                act = a.cpu().numpy().astype(np.float32).ravel()
                _send_frame(conn, {"ok": True, "action_len": int(act.size)}, act.tobytes())
        except (ConnectionError, OSError) as e:
            print(f"[policy] client error: {e}", flush=True)
        finally:
            conn.close()
            print("[policy] client disconnected", flush=True)


if __name__ == "__main__":
    main()
