#!/usr/bin/env python3
"""Scan trained ACT models for the GUI results view (stdlib-only; imported by
server.py which runs the console python, NOT the act_train env).

Each model lives in TRAINING_DIR/<repo_safe>/ (output_dir of lerobot_train):
  checkpoints/<step>/pretrained_model/{config.json,train_config.json,model.safetensors}
  checkpoints/last -> newest
  wbc_meta.json   (written by the training queue: sessions/images/touch/log_path/...)

We derive everything the card needs:
  * config (rgb|rgbd, touch, action/state dims, target steps)  <- checkpoint config.json
  * episode / frame counts / fps                               <- HF dataset meta/info.json
  * loss curve + final loss + current step + status            <- per-model training log
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

HF_LEROBOT_HOME = Path.home() / ".cache" / "huggingface" / "lerobot"

_LOSS_RE = re.compile(r"step:([0-9.]+[KM]?)\b.*?\bloss:([0-9.]+)")
_EPCH_RE = re.compile(r"\bepch:([0-9.]+)")


def _num(s: str) -> float:
    """'100K'->100000, '1.5K'->1500, '2M'->2e6, '200'->200."""
    s = s.strip()
    mult = 1.0
    if s and s[-1] in "KMkm":
        mult = 1000.0 if s[-1] in "Kk" else 1_000_000.0
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def parse_loss(log_path: Path, max_points: int = 200) -> list[dict]:
    """Extract [{step,loss,epoch}] from a lerobot training stdout log. tqdm
    progress-bar lines are ignored (they have no 'loss:'). Downsampled to
    max_points (keeps the last point)."""
    if not log_path or not Path(log_path).is_file():
        return []
    pts: list[dict] = []
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                if "loss:" not in line or "step:" not in line:
                    continue
                m = _LOSS_RE.search(line)
                if not m:
                    continue
                e = _EPCH_RE.search(line)
                pts.append({"step": int(_num(m.group(1))),
                            "loss": float(m.group(2)),
                            "epoch": float(e.group(1)) if e else None})
    except OSError:
        return []
    # de-dup consecutive identical steps (lerobot logs a step several times)
    dedup: list[dict] = []
    for p in pts:
        if dedup and dedup[-1]["step"] == p["step"]:
            dedup[-1] = p
        else:
            dedup.append(p)
    if len(dedup) > max_points:
        k = len(dedup) / max_points
        idx = sorted({int(i * k) for i in range(max_points)} | {len(dedup) - 1})
        dedup = [dedup[i] for i in idx if i < len(dedup)]
    return dedup


def _read_json(p: Path) -> dict:
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}


def _checkpoints(outdir: Path) -> list[str]:
    ck = outdir / "checkpoints"
    if not ck.is_dir():
        return []
    nums = sorted((d.name for d in ck.iterdir()
                   if d.is_dir() and d.name.isdigit()), key=int)
    if (ck / "last").exists():
        nums.append("last")
    return nums


def _dataset_meta(repo_id: str) -> dict:
    info = _read_json(HF_LEROBOT_HOME / repo_id / "meta" / "info.json")
    return {"episodes": info.get("total_episodes"),
            "frames": info.get("total_frames"),
            "fps": info.get("fps")}


def model_info(outdir: Path) -> dict | None:
    """Build one model card's data, or None if the dir isn't a model."""
    outdir = Path(outdir)
    meta = _read_json(outdir / "wbc_meta.json")
    ckpts = _checkpoints(outdir)
    if not ckpts and not meta:
        return None

    # config from the newest checkpoint's pretrained_model/
    cfg = tcfg = {}
    if ckpts:
        pm = outdir / "checkpoints" / ckpts[-1] / "pretrained_model"
        cfg = _read_json(pm / "config.json")
        tcfg = _read_json(pm / "train_config.json")

    inp = cfg.get("input_features", {}) or {}
    out = cfg.get("output_features", {}) or {}
    img_keys = [k for k in inp if k.startswith("observation.images")]
    rgbd = any(k.endswith("_depth") for k in img_keys)
    state_dim = (inp.get("observation.state", {}) or {}).get("shape", [0])
    state_dim = state_dim[0] if state_dim else 0
    action_dim = (out.get("action", {}) or {}).get("shape", [0])
    action_dim = action_dim[0] if action_dim else 0

    repo_id = meta.get("repo_id") or (tcfg.get("dataset", {}) or {}).get("repo_id") \
        or outdir.name.replace("_", "/", 1)
    steps_target = meta.get("steps") or tcfg.get("steps")

    # loss curve: pick the NEWEST matching log among the meta pointer and any
    # logs/*/act_train__<dir>.log, so a live resume/recovery run's curve wins
    # over a stale crashed-run log the queue may have recorded in meta.
    cands = []
    mlp = meta.get("log_path")
    if mlp and Path(mlp).is_file():
        cands.append(Path(mlp))
    cands += [p for p in (outdir.parent.parent / "logs").glob(f"*/act_train__{outdir.name}.log")
              if p.is_file()]
    log_path = str(max(cands, key=lambda p: p.stat().st_mtime)) if cands else None
    loss = parse_loss(Path(log_path)) if log_path else []
    final_loss = loss[-1]["loss"] if loss else None
    last_step = loss[-1]["step"] if loss else None

    ds = _dataset_meta(repo_id)

    status = meta.get("status")
    if not status:
        status = "done" if "last" in ckpts else ("training" if loss else "unknown")

    # touch: state = 29(joint)+6+6 = 41 without touch; >41 means tactile appended
    touch = meta.get("touch")
    if touch is None:
        touch = 1 if state_dim and state_dim != 41 else 0

    return {
        "repo_id": repo_id,
        "dir": outdir.name,
        "images": meta.get("images") or ("rgbd" if rgbd else "rgb"),
        "touch": int(bool(touch)),
        "state_dim": state_dim,
        "action_dim": action_dim,
        "steps_target": steps_target,
        "last_step": last_step,
        "final_loss": final_loss,
        "episodes": ds["episodes"],
        "frames": ds["frames"],
        "fps": ds["fps"],
        "sessions": meta.get("sessions"),
        "checkpoints": ckpts,
        "loss": loss,
        "status": status,
        "started_at": meta.get("started_at"),
    }


def list_models(training_dir: Path) -> list[dict]:
    training_dir = Path(training_dir)
    if not training_dir.is_dir():
        return []
    out = []
    for d in training_dir.iterdir():
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        mi = model_info(d)
        if mi:
            out.append(mi)
    out.sort(key=lambda m: (m.get("started_at") or 0), reverse=True)
    return out


def live_card(job: dict, loss: list | None = None) -> dict:
    """Synthesize a model card for a job that's TRAINING but whose output dir
    doesn't exist yet (lerobot creates it only at the first checkpoint, ~step
    20k). Curve comes straight from the live training log; episode/frame counts
    from the (already-built) dataset."""
    repo = job["repo_id"]
    loss = loss or []
    ds = _dataset_meta(repo)
    return {
        "repo_id": repo, "dir": repo.replace("/", "_"),
        "images": job.get("images", "rgb"), "touch": int(bool(job.get("touch"))),
        "state_dim": 0, "action_dim": 0, "steps_target": job.get("steps"),
        "last_step": loss[-1]["step"] if loss else None,
        "final_loss": loss[-1]["loss"] if loss else None,
        "episodes": ds["episodes"], "frames": ds["frames"], "fps": ds["fps"],
        "sessions": job.get("sessions"), "checkpoints": [], "loss": loss,
        "status": "training", "started_at": job.get("enqueued_at"),
    }


def write_meta(outdir: Path, **fields) -> None:
    """Merge fields into outdir/wbc_meta.json (created once lerobot makes outdir)."""
    outdir = Path(outdir)
    if not outdir.is_dir():
        return
    p = outdir / "wbc_meta.json"
    cur = _read_json(p)
    cur.update({k: v for k, v in fields.items() if v is not None})
    try:
        p.write_text(json.dumps(cur, indent=2))
    except OSError:
        pass


if __name__ == "__main__":
    import sys
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "training"
    for m in list_models(base):
        print(f"{m['repo_id']:30s} {m['images']:5s} touch={m['touch']} "
              f"ep={m['episodes']} frames={m['frames']} "
              f"step={m['last_step']}/{m['steps_target']} loss={m['final_loss']} "
              f"ckpts={len(m['checkpoints'])} {m['status']}")
