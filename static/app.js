/* G1 PICO WBC Console frontend — 1s polling, no deps. */
"use strict";

let STATE = null;
let safetyToken = null;

// 遥操作模式中文名（值来自 streamer stdout 的 enum 名，保留英文括注以便对照状态机图）
const STREAM_MODE_ZH = {
  OFF: "关闭",
  POSE: "全身跟随",
  PLANNER: "行走规划",
  PLANNER_FROZEN_UPPER: "上身冻结+行走",
  POSE_PAUSE: "暂停",
  VR_3PT: "三点跟随(头+双手)",
  PLANNER_VR_3PT: "三点跟随(头+双手)",
};
// 步态中文名（按 LocomotionMode 编号 0-19）
const LOCO_MODE_ZH = {
  0: "待机", 1: "慢走", 2: "行走", 3: "奔跑", 4: "下蹲",
  5: "双腿跪", 6: "单腿跪", 7: "俯卧", 8: "爬行",
  9: "拳击待机", 10: "拳击行走", 11: "左直拳", 12: "右直拳",
  13: "左勾拳", 14: "右勾拳", 15: "组合拳A", 16: "组合拳B",
  17: "前跳", 18: "潜行", 19: "受伤行走",
};
function streamModeLabel(m) {
  if (!m) return "–";
  const zh = STREAM_MODE_ZH[m];
  return zh ? `${zh} (${m})` : m;
}
function locoModeLabel(n, name) {
  if (n === null || n === undefined) return "–";
  const zh = LOCO_MODE_ZH[n];
  return zh ? `${n} ${zh}` : `${n} ${name || ""}`;
}
const logSeqs = {};            // proc name -> last seq
const LOG_PROCS = ["install_pico", "pc_service", "sim_loop", "deploy_sim", "deploy_real", "pico_streamer", "brainco_hand", "gui_drive", "recorder", "convert_dataset", "act_train", "act_infer"];
// pico_streamer log is mirrored into two panes (step 4 + step 5)
const LOG_PANES = {
  install_pico: ["log-install_pico"], pc_service: ["log-pc_service"],
  sim_loop: ["log-sim_loop"], deploy_sim: ["log-deploy_sim"],
  deploy_real: ["log-deploy_real"],
  pico_streamer: ["log-pico_streamer", "log-pico_streamer-5"],
  brainco_hand: ["log-brainco_hand"],
  gui_drive: ["log-gui_drive"],
  recorder: ["log-recorder"],
  convert_dataset: ["log-convert_dataset"],
  act_train: ["log-act_train"],
  act_infer: ["log-act_infer"],
};

const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opts = body !== undefined
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  // Never throw: a network blip used to become a silent unhandled rejection that
  // killed the caller (incl. E-STOP). Always resolve to an object with .ok.
  try {
    const r = await fetch(path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok && j.ok === undefined) j.ok = false;
    if (!r.ok && !j.error) j.error = "HTTP " + r.status;
    return j;
  } catch (e) {
    return { ok: false, _neterr: true, error: "网络/服务未响应（控制台可能已断开）：" + (e.message || e) };
  }
}

/* ── stepper navigation ── */
function showStep(n) {
  document.querySelectorAll(".step-panel").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll(".step-tab").forEach((t) => t.classList.remove("active"));
  $(`panel-${n}`).classList.add("active");
  document.querySelector(`.step-tab[data-step="${n}"]`).classList.add("active");
  localStorage.setItem("wbc_step", n);
  // auto-load the data viewer the first time you open it
  if (String(n) === "8" && $("ep-sel") && !$("ep-sel").options.length) loadRecordings();
  if (String(n) === "7") loadFrozenRelease();
  if (String(n) === "9") { pbLoad(); pbRefresh(); }
  if (String(n) === "10") { trainLoadSessions(); trainOverview(); }
}
document.querySelectorAll(".step-tab").forEach((t) => {
  t.tabIndex = 0;  // keyboard-reachable
  t.addEventListener("click", () => showStep(t.dataset.step));
  t.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showStep(t.dataset.step); }
  });
});

/* ── actions ── */
async function runChecks(scope) {
  const res = await api("/api/checks/run", { scope });
  if (scope === "env") renderChecks(res.checks);
}

async function startProc(name) {
  const body = {};
  if (name === "pico_streamer") body.vis = $("vis-toggle").checked;
  if (name === "deploy_real") {
    if (!safetyToken) { uiToast("请先完成步骤5 的安全确认门（勾选清单 + 输入「确认姿态」）", "warn"); return; }
    body.safety_token = safetyToken;
  }
  const res = await api(`/api/procs/${name}/start`, body);
  if (!res.ok) uiToast("启动 " + name + " 失败：" + (res.error || "见日志"), "warn");
  // only consume the safety token if deploy actually started (don't waste the gate)
  if (name === "deploy_real" && res.ok) { safetyToken = null; updateSafetyUI(false); }
  poll();
}

async function stopProc(name) { await api(`/api/procs/${name}/stop`, {}); poll(); }

const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const procAlive = (n) => !!(STATE && STATE.procs && STATE.procs[n] && STATE.procs[n].alive);

/* ── one-click bring-up: deploy → streamer → hand bridge → recorder (+goal) ──
   Collapses steps ②③④ + 准备录制 into one button. The dangerous prep
   (confirm posture → release high-level mode) stays explicit above this. */
async function startTeleopRecordStack(btn) {
  const setS = (m) => { const s = $("stack-status"); if (s) s.textContent = m; };
  if (btn) btn.disabled = true;
  try {
    if (!procAlive("deploy_real")) {
      if (!safetyToken) { setS("✗ 先完成上面的安全确认门（确认姿态）再点"); return; }
      setS("启动 deploy(real)…（首次建/载模型可能 ~30s）");
      const r = await api("/api/procs/deploy_real/start", { safety_token: safetyToken });
      safetyToken = null; updateSafetyUI(false);
      if (!r.ok) { setS("✗ deploy 启动失败：" + (r.error || "")); return; }
      await _sleep(3000);
    }
    if (!procAlive("pico_streamer")) {
      setS("启动 PICO streamer…");
      await api("/api/procs/pico_streamer/start", { vis: !!($("vis-toggle") && $("vis-toggle").checked) });
      await _sleep(1500);
    }
    // ensure the PC2 hand server is actually publishing state (auto-restart if its
    // DDS went bad) BEFORE starting the bridge — folds the hand debug into startup
    setS("检查手服务端健康…");
    const he = await api("/api/brainco/ensure", {});
    if (he.restarted) setS(he.healthy ? "手服务端不健康 → 已自动重启 ✓" : "⚠ 手服务端重启后仍无数据，查 PC2/通电");
    await _sleep(500);
    if (!procAlive("brainco_hand")) { setS("启动手桥…"); await api("/api/procs/brainco_hand/start", {}); await _sleep(1200); }
    if (!procAlive("recorder")) { setS("启动录制器…"); await api("/api/procs/recorder/start", {}); await _sleep(1500); }
    const goal = (($("stack-goal") && $("stack-goal").value) || "").trim() || "拿瓶子";
    localStorage.setItem("wbc_goal", goal);
    await api("/api/record", { cmd: "goal", goal });
    setS("✓ 全链路已拉起 — 戴 PICO，去步骤7 看就绪灯，PICO动作转绿后 左Grip+A 录制");
  } finally { if (btn) btn.disabled = false; poll(); }
}

/* ── manual drive (no PICO): relay intent to gui_drive via /api/drive ── */
const _drive = { fwd: 0, strafe: 0, turn: 0 };
function driveSend(extra) {
  const gait = parseInt($("drive-gait").value, 10);
  const sp = parseFloat($("drive-speed").value);
  $("drive-speed-val").textContent = sp < 0 ? "默认" : sp.toFixed(1);
  const body = Object.assign({ gait, speed: sp, fwd: _drive.fwd, strafe: _drive.strafe, turn: _drive.turn }, extra || {});
  const r = $("drive-readout");
  if (r) r.textContent = (_drive.fwd || _drive.strafe || _drive.turn)
    ? `前${_drive.fwd} 移${_drive.strafe} 转${_drive.turn}` : "停";
  api("/api/drive", body);
}
async function drivePolicy(p) {
  if (p === "start" && !(await uiConfirm("启动策略 = 机器人开始被控制（Init Done 后会动）。确认场地清空、急停在手？"))) return;
  driveSend({ policy: p });
}
function driveHold(axis, v) { _drive[axis] = v; driveSend(); }
function driveRelease(axis) { if (_drive[axis] !== 0) { _drive[axis] = 0; driveSend(); } }
function driveAllStop() { _drive.fwd = _drive.strafe = _drive.turn = 0; driveSend(); }
const _driveKeys = { w: ["fwd", 1], s: ["fwd", -1], a: ["strafe", 1], d: ["strafe", -1], q: ["turn", 1], e: ["turn", -1] };
document.addEventListener("keydown", (ev) => {
  if (document.getElementById("panel-6")?.classList.contains("active") &&
      _driveKeys[ev.key] && !ev.repeat) {
    const [ax, v] = _driveKeys[ev.key]; _drive[ax] = v; driveSend();
  }
});
document.addEventListener("keyup", (ev) => {
  if (_driveKeys[ev.key]) { const [ax] = _driveKeys[ev.key]; if (_drive[ax] !== 0) { _drive[ax] = 0; driveSend(); } }
});
// deadman failsafe: never leave the robot moving if focus/pointer is lost
const _driveDead = () => { if (_drive.fwd || _drive.strafe || _drive.turn) driveAllStop(); };
window.addEventListener("blur", _driveDead);
document.addEventListener("visibilitychange", () => { if (document.hidden) _driveDead(); });
document.addEventListener("pointerup", _driveDead);
document.addEventListener("pointercancel", _driveDead);
document.addEventListener("touchcancel", _driveDead);

/* ── episode recorder (StageAct data collection): relay to /api/record ── */
let _recOn = false;
async function recCmd(cmd, extra) {
  if (cmd === "discard" && !(await uiConfirm("丢弃当前 episode？此条数据不保存。"))) return;
  await api("/api/record", Object.assign({ cmd }, extra || {}));
}
function recToggle() { recCmd(_recOn ? "stop" : "start"); }   // one button / spacebar / grip+A
function recAuto(on) { api("/api/record", { cmd: "auto", on: !!on }); }
async function recPrepare() {
  // one-click: start the recorder backend (if not already up) + set the goal
  if (!(STATE && STATE.procs && STATE.procs.recorder && STATE.procs.recorder.alive)) {
    await api("/api/procs/recorder/start", {});
    await new Promise((r) => setTimeout(r, 1500));   // let it bind sockets / DDS
  }
  const goal = (($("rec-goal") && $("rec-goal").value) || "").trim() || "拿瓶子";
  localStorage.setItem("wbc_goal", goal);
  await api("/api/record", { cmd: "goal", goal });
  poll();
}
function recStage(stage, label) {
  api("/api/record", { cmd: "stage", stage, label });
  if ($("rec-stage")) $("rec-stage").textContent = String(stage);
}
async function restartHand(btn) {
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "重启中…(~10s)"; }
  const r = await api("/api/brainco/restart", {});
  if (btn) { btn.disabled = false; btn.textContent = old; }
  // success shows up as the 左/右手q + 触觉 chips turning green within a few seconds
  if (!r.ok) uiToast("重启手服务端失败：" + (r.error || r.out || "见日志"), "warn");
  else uiToast("PC2 手服务端已重启", "ok");
}

/* ── dexterous-hand: upper-body-LOCK release delay slider (step 7) ── */
function frzLabel(v) {
  v = parseFloat(v);
  const el = $("frz-val");
  if (el) { el.textContent = v === 0 ? "不松手" : v.toFixed(1) + " 秒"; el.className = "pill " + (v === 0 ? "err" : "ok"); }
}
async function loadFrozenRelease() {
  const r = await fetch("/api/hand/frozen_release").then((x) => x.json()).catch(() => null);
  if (r && r.ok) { const s = $("frz-slider"); if (s) s.value = r.seconds; frzLabel(r.seconds); }
}
async function setFrozenRelease(v) {
  const r = await api("/api/hand/frozen_release", { seconds: parseFloat(v) });
  if (r.ok) { frzLabel(r.seconds); uiToast(r.seconds === 0 ? "上半身锁定：手一直保持抓握" : `上半身锁定 ${r.seconds.toFixed(1)} 秒后松手`); }
  else uiToast("设置失败：" + (r.error || ""), "danger");
}

/* ── step 9: playback a recorded episode to the real robot ── */
function pbSpeedLabel(v) { const e = $("pb-speed-val"); if (e) e.textContent = parseFloat(v).toFixed(2) + "x"; }
function pbSpeedLive(v) { pbSpeedLabel(v); api("/api/playback/speed", { speed: parseFloat(v) }); }  // live mid-playback
async function pbLoad() {
  const sel = $("pb-sel"); if (!sel) return;
  const r = await fetch("/api/recordings").then((x) => x.json()).catch(() => null);
  if (!r || !r.sessions || !r.sessions.length) { sel.innerHTML = "<option>（暂无录制）</option>"; return; }
  let h = "";
  for (const s of r.sessions) for (const e of s.episodes)
    h += `<option value="${e.path}">${s.session}/${e.name} · ${e.frames}帧 · ${e.seconds || 0}s</option>`;
  sel.innerHTML = h;
}
async function pbRefresh() {
  const st = await fetch("/api/state").then((x) => x.json()).catch(() => null);
  const ul = $("pb-precond-list"), pill = $("pb-precond");
  if (!st || !ul) return;
  const p = st.procs || {}, t = st.teleop || {};
  const ready = !!t.deploy_ready || !!t.deploy_control;
  const z = (n) => p[n] && p[n].alive;
  const occ = z("pico_streamer") || z("gui_drive");
  const items = [
    [ready, "SONIC deploy 已就绪（到 Init Done）—— 回放会自己进 CONTROL", "去步骤5起 deploy 到 Init Done（释放高层+调试模式）"],
    [!occ, "5556 已空闲（PICO数据流 / 手动驾驶 已停）", "先停 pico_streamer / gui_drive"],
  ];
  ul.innerHTML = items.map(([ok, label, fix]) =>
    `<li><span style="color:${ok ? "var(--ok)" : "var(--fail)"}">${ok ? "✓" : "✗"}</span> ${label}${ok ? "" : ` <span class="hint">— ${fix}</span>`}</li>`).join("");
  const allOk = items.every((x) => x[0]);
  if (pill) { pill.textContent = allOk ? "就绪" : "未就绪"; pill.className = "pill " + (allOk ? "ok" : "err"); }
}
async function pbEngage() {
  const mode = (document.querySelector('input[name="pb-mode"]:checked') || {}).value;
  if (mode !== "pico") { uiToast("电机层面稍后实现，先用 PICO 层面", "warn"); return; }
  const ep = $("pb-sel").value;
  if (!ep || ep[0] !== "/") { uiToast("先选一条 episode", "warn"); return; }
  const checklist = {};
  document.querySelectorAll(".pb-ck").forEach((c) => { checklist[c.dataset.key] = c.checked; });
  const typed = ($("pb-typed").value || "").trim();
  if (!(await uiConfirm("① 起身站稳：\n这会【启动策略进 CONTROL】，机器人缓动到录制起始姿势并站稳。\n场地清空、急停在手？"))) return;
  const tok = await api("/api/safety/confirm", { checklist, typed });
  if (!tok.ok) { uiToast("安全确认未通过：" + (tok.error || ""), "danger"); return; }
  const speed = parseFloat($("pb-speed").value);
  const r = await api("/api/playback/start", { episode: ep, speed, safety_token: tok.safety_token });
  if (!r.ok) { uiToast("启动失败：" + (r.error || ""), "danger"); return; }
  uiToast("起身中：正在让 deploy 进 CONTROL…", "ok");
  // OBJECTIVELY confirm the deploy actually engaged (else poses are ignored —
  // robot just stands in PD-hold and "nothing happens" on play).
  for (let i = 0; i < 20; i++) {
    await new Promise((res) => setTimeout(res, 500));
    const st = await fetch("/api/state").then((x) => x.json()).catch(() => null);
    if (st && st.teleop && st.teleop.deploy_control) {
      uiToast("✅ deploy 已进 CONTROL，机器人已接管 —— 目视站稳后点 ②", "ok");
      return;
    }
    if (st && st.procs && (st.procs.playback || {}).alive === false) {
      uiToast("⚠ 回放进程已退出，看 playback 日志", "danger"); return;
    }
  }
  uiToast("⚠ deploy 10s 内没进 CONTROL！检查：① 机器人遥控器已 L2+R2 调试模式？② 看 playback 日志 start 是否发出。点 ■ 停止后重来", "danger");
}
async function pbPlay() {
  const st = await fetch("/api/state").then((x) => x.json()).catch(() => null);
  if (!st || !(st.teleop || {}).deploy_control) {
    uiToast("deploy 还没进 CONTROL —— 机器人没接管，现在回放无效。先点①等它接管（看到「✅已进CONTROL」）", "danger");
    return;
  }
  if (!(await uiConfirm("② 开始回放：机器人将按录制轨迹前进。\n已目视确认站稳、急停在手？"))) return;
  const r = await api("/api/playback/play", {});
  uiToast(r.ok ? "开始前进回放" : "失败：" + (r.error || "回放进程没在站立等待？"), r.ok ? "ok" : "danger");
}
async function pbStop() {
  const r = await api("/api/procs/playback/stop", {});
  uiToast(r.ok ? "回放已停止（机器人保持最后指令，必要时急停）" : "停止失败：" + (r.error || ""), r.ok ? "ok" : "danger");
}

/* ── step 10: ACT training (convert curated recordings -> LeRobot -> train) ── */
async function trainStatus() {
  const r = await fetch("/api/train/status").then((x) => x.json()).catch(() => null);
  const p = $("train-env-pill");
  if (!p) return;
  if (r && r.env_ready) { p.textContent = "环境就绪"; p.className = "pill ok"; }
  else { p.textContent = "环境装中…(torch/lerobot)"; p.className = "pill err"; }
}
async function trainLoadSessions() {
  const host = $("train-sessions");
  const r = await fetch("/api/recordings").then((x) => x.json()).catch(() => null);
  if (!r || !r.sessions || !r.sessions.length) { if (host) host.innerHTML = "<span class='hint'>暂无录制</span>"; return; }
  let h = "";
  for (const s of r.sessions) {
    const keep = s.episodes.filter((e) => e.curation && e.curation.keep === true).length;
    h += `<label style="display:inline-block;margin:3px 14px 3px 0">
      <input type="checkbox" class="train-sess" value="${s.session}" ${keep ? "" : "disabled"}>
      ${s.session} <span class="hint">(✓要 ${keep}/${s.count})</span></label>`;
  }
  if (host) host.innerHTML = h;
  trainStatus();
}
function trainSelAll(c) { document.querySelectorAll(".train-sess:not(:disabled)").forEach((x) => { x.checked = c; }); }
function _trainSel() { return [...document.querySelectorAll(".train-sess:checked")].map((x) => x.value); }
async function trainEnqueue() {
  const sessions = _trainSel();
  if (!sessions.length) { uiToast("先勾选要训练的会话(至少一个有 ✓要 的)", "warn"); return; }
  const repo = ($("train-repo").value || "").trim();
  if (!repo) { uiToast("先填模型名", "warn"); return; }
  const images = $("train-images").value, touch = $("train-touch").checked;
  const steps = parseInt($("train-steps").value) || 100000;
  const r = await api("/api/train/enqueue", { sessions, repo_id: repo, images, touch, steps });
  if (r.ok) {
    uiToast(`已入队：${r.job.repo_id}(${images}${touch ? "+touch" : ""}, ${sessions.length}会话, ${steps}步)`, "ok");
    // auto-bump a trailing number so the next enqueue won't name-collide
    $("train-repo").value = repo.replace(/(\d+)(?!.*\d)/, (m) => parseInt(m) + 1);
    trainOverview();
  } else uiToast("入队失败：" + (r.error || ""), "danger");
}

// inline SVG loss sparkline (log-y so early drop + late detail both read)
function _spark(loss) {
  if (!loss || loss.length < 2) return `<div class="spark-empty">暂无 loss（训练启动后出现）</div>`;
  const W = 300, H = 54, pad = 3;
  const steps = loss.map((p) => p.step);
  const x0 = Math.min(...steps), x1 = Math.max(...steps);
  const ly = loss.map((p) => Math.log10(Math.max(p.loss, 1e-4)));
  const y0 = Math.min(...ly), y1 = Math.max(...ly);
  const sx = (s) => pad + (W - 2 * pad) * (x1 > x0 ? (s - x0) / (x1 - x0) : 0);
  const sy = (v) => pad + (H - 2 * pad) * (y1 > y0 ? (Math.log10(Math.max(v, 1e-4)) - y1) / (y0 - y1) : 0.5);
  let d = "";
  loss.forEach((p, i) => { d += (i ? "L" : "M") + sx(p.step).toFixed(1) + "," + sy(p.loss).toFixed(1) + " "; });
  const last = loss[loss.length - 1];
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="var(--acc)" stroke-width="1.5"/>
    <circle cx="${sx(last.step).toFixed(1)}" cy="${sy(last.loss).toFixed(1)}" r="2.2" fill="var(--ok)"/></svg>`;
}
function _fmt(n) { return n == null ? "—" : (n >= 1000 ? (n / 1000).toFixed(n % 1000 ? 1 : 0) + "K" : "" + n); }

function renderModels(models) {
  const host = $("train-models");
  if (!host) return;
  if (!models || !models.length) { host.innerHTML = `<span class="hint">还没有训练好的模型。上面新建一个训练。</span>`; return; }
  host.innerHTML = models.map((m) => {
    const imgChip = m.images === "rgbd" ? `<span class="chip rgbd">RGBD</span>` : `<span class="chip rgb">RGB</span>`;
    const touchChip = m.touch ? `<span class="chip touch">+触觉</span>` : "";
    const prog = m.steps_target ? Math.round(100 * (m.last_step || 0) / m.steps_target) : 0;
    const statusChip = m.status === "training" ? `<span class="chip warn">训练中 ${prog}%</span>`
      : (m.status === "failed" ? `<span class="chip warn">失败</span>` : `<span class="chip touch">✓ 完成</span>`);
    const ckOpts = (m.checkpoints || []).slice().reverse()
      .map((c) => `<option value="${c}">${c === "last" ? "最新(last)" : "step " + c}</option>`).join("");
    const deployable = (m.checkpoints || []).length > 0 && m.status !== "training";
    return `<div class="model-card ${m.status}">
      <h4>${m.repo_id}</h4>
      <div class="chips">${imgChip}${touchChip}${statusChip}
        <span class="chip">${_fmt(m.episodes)} 段</span><span class="chip">${_fmt(m.frames)} 帧</span></div>
      ${_spark(m.loss)}
      <div class="model-stat">loss <b>${m.final_loss != null ? m.final_loss.toFixed(3) : "—"}</b>
        · step <b>${_fmt(m.last_step)}</b>/${_fmt(m.steps_target)}
        · 动作 <b>${m.action_dim}</b>d · 状态 <b>${m.state_dim}</b>d</div>
      <div class="model-actions">
        <select id="ck-${m.dir}" ${deployable ? "" : "disabled"}>${ckOpts || "<option>无</option>"}</select>
        <button class="btn primary" ${deployable ? "" : "disabled"} onclick="deployModel('${m.repo_id}','${m.dir}')">▶ 部署</button>
        <button class="btn" onclick="deleteModel('${m.dir}','${m.repo_id}')">🗑</button>
      </div></div>`;
  }).join("");
}

function renderQueue(queue) {
  const host = $("train-queue");
  if (!host) return;
  const active = (queue || []).filter((j) => j.status !== "done");
  if (!active.length) { host.innerHTML = `<span class="hint">队列空。</span>`; return; }
  const L = { queued: "排队", converting: "转换中", training: "训练中", failed: "失败" };
  host.innerHTML = active.map((j) => {
    const cfg = `${j.images}${j.touch ? "+touch" : ""}, ${Array.isArray(j.sessions) ? j.sessions.length + "会话" : j.sessions}, ${_fmt(j.steps)}步`;
    const err = j.error ? ` <span class="hint" style="color:var(--fail)">${j.error}</span>` : "";
    return `<div class="qrow"><span class="qstat ${j.status}">${L[j.status] || j.status}</span>
      <b>${j.repo_id}</b> <span class="hint">${cfg}</span>${err}</div>`;
  }).join("");
}

async function trainOverview() {
  const r = await fetch("/api/train/overview").then((x) => x.json()).catch(() => null);
  if (!r || !r.ok) return;
  const p = $("train-env-pill");
  if (p) { if (r.env_ready) { p.textContent = "环境就绪"; p.className = "pill ok"; } else { p.textContent = "环境装中…"; p.className = "pill err"; } }
  renderQueue(r.queue);
  renderModels(r.models);
}
function _trainPoll() { if ($("panel-10") && $("panel-10").classList.contains("active")) trainOverview(); }

async function deleteModel(dir, repo) {
  if (!(await uiConfirm(`移除模型 ${repo}？\n移到回收站 training/_trash/（可恢复；数据集和录制不动）。`))) return;
  const r = await api("/api/train/delete_model", { dir });
  uiToast(r.ok ? (r.note || "已移除") : "失败：" + (r.error || ""), r.ok ? "ok" : "danger");
  trainOverview();
}

// ▶ deploy a trained model to the real robot (ACT inference -> SONIC), same
// stand-first / confirm-CONTROL flow as playback.
async function deployModel(repo, dir) {
  const sel = $("ck-" + dir);
  const ck = sel ? sel.value : "last";
  const checklist = {};
  document.querySelectorAll(".dep-ck").forEach((c) => { checklist[c.dataset.key] = c.checked; });
  const typed = ($("deploy-typed").value || "").trim();
  if (!(await uiConfirm(`部署 ${repo}（${ck}）？\n这会【启动策略进 CONTROL】，由模型实时驱动机器人 —— 先 HOLD 站稳，再点「② 执行」。\n场地清空、急停在手？`))) return;
  const tok = await api("/api/safety/confirm", { checklist, typed });
  if (!tok.ok) { uiToast("安全确认未通过：" + (tok.error || ""), "danger"); return; }
  const r = await api("/api/infer/start", { dir, checkpoint: ck, safety_token: tok.safety_token });
  if (!r.ok) { uiToast("启动失败：" + (r.error || ""), "danger"); return; }
  uiToast("起身中：让 deploy 进 CONTROL（策略服务器加载约几秒）…", "ok");
  for (let i = 0; i < 40; i++) {
    await new Promise((res) => setTimeout(res, 500));
    const st = await fetch("/api/state").then((x) => x.json()).catch(() => null);
    if (st && st.teleop && st.teleop.deploy_control) {
      uiToast("✅ deploy 已进 CONTROL，机器人 HOLD 住首个预测姿势 —— 目视站稳后点「② 执行」", "ok"); return;
    }
    if (st && st.procs && (st.procs.act_infer || {}).alive === false) {
      uiToast("⚠ 推理进程已退出，看 act_infer 日志", "danger"); return;
    }
  }
  uiToast("⚠ 20s 内没进 CONTROL —— 检查：① L2+R2 调试模式？② act_infer 日志（策略服务器/相机/DDS）", "danger");
}
async function inferPlay() {
  const st = await fetch("/api/state").then((x) => x.json()).catch(() => null);
  if (!st || !(st.teleop || {}).deploy_control) {
    uiToast("deploy 还没进 CONTROL —— 先点某个模型的「▶ 部署」等它接管（看到「✅已进CONTROL」）", "danger"); return;
  }
  if (!(await uiConfirm("② 开始自主执行：机器人将由模型实时控制动作。\n已目视站稳、急停在手？"))) return;
  const r = await api("/api/infer/play", {});
  uiToast(r.ok ? "▶ 自主执行中" : "失败：" + (r.error || "推理进程没在 HOLD 等待？"), r.ok ? "ok" : "danger");
}
async function inferStop() {
  const r = await api("/api/procs/act_infer/stop", {});
  uiToast(r.ok ? "已停止（机器人保持最后指令，必要时急停 o）" : "停止失败：" + (r.error || ""), r.ok ? "ok" : "danger");
}
// live test controls -> act_infer
function inferCtl(o) { return api("/api/infer/ctl", o); }
function inferCmd(c) { inferCtl({ cmd: c }); uiToast(c === "pause" ? "⏸ 本轮保持（手指延时后松开）" : (c === "next" ? "⟲ 回初始位 → 新一轮" : c), "ok"); }
function inferSpeed(v) { const e = $("infer-speed-val"); if (e) e.textContent = parseFloat(v).toFixed(2) + "x"; inferCtl({ speed: parseFloat(v) }); }
function inferLoop(b) { inferCtl({ loop: !!b }); uiToast(b ? "🔁 循环开（单轮到点自动回初始重来）" : "循环关", "ok"); }
function inferRound(v) { inferCtl({ round_s: parseFloat(v) || 0 }); }
function inferRelease(v) { inferCtl({ release_s: parseFloat(v) || 0 }); }
function inferRefresh() { inferCtl({ cmd: "refresh" }); uiToast("🔄 刷新frame1（重新预测初始姿势，平滑过渡）", "ok"); }
function inferRampIn(v) { const e = $("infer-rampin-val"); if (e) e.textContent = parseFloat(v).toFixed(1) + "s"; inferCtl({ ramp_in_s: parseFloat(v) || 0 }); }

async function restartCamera(btn) {
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "重启中…(~6s)"; }
  const r = await api("/api/camera/restart", {});
  if (btn) { btn.disabled = false; btn.textContent = old; }
  // success: the 相机/深度 chips resume + "⚠相机冻帧" clears within a few seconds
  if (!r.ok) uiToast("重启相机失败：" + (r.error || r.out || "见日志"), "warn");
  else uiToast("PC2 相机服务已重启，几秒后画面恢复", "ok");
}

async function handSelftest(btn) {
  if (!(await uiConfirm("自检手会驱动两只手运动约 12 秒（自动停手桥跑完再重起）。确认手周围无障碍、可以动？"))) return;
  const out = $("selftest-out");
  if (out) { out.classList.remove("hidden"); out.textContent = "自检中…（手会动，约 12s）"; }
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/hand/selftest", {});
    if (!out) return;
    const heal = r.auto_recovered ? "ℹ 检测到收不到手数据 → 已自动重启 PC2 手服务端后重试\n\n" : "";
    if (!r.ok) { out.textContent = heal + "✗ " + (r.error || "失败"); return; }
    let s = heal + r.verdict + "\n\n";
    for (const side of ["L", "R"]) {
      const h = r.hands && r.hands[side];
      if (!h) continue;
      s += (side === "L" ? "左手" : "右手") + (h.present ? "" : "（无 state！）") + "：\n";
      for (const m of h.motors) {
        s += `  motor${m.motor} ${m.group}  Δ=${m.delta}  ${m.ok ? "✓ 动" : "✗ 没动"}\n`;
      }
    }
    out.textContent = s;
  } finally { if (btn) btn.disabled = false; poll(); }
}

async function verifyMotors(btn) {
  const out = $("verify-out");
  if (out) { out.classList.remove("hidden"); out.textContent = "验证中…（读 rt/lowcmd vs rt/lowstate）"; }
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/verify/motors", {});
    if (!out) return;
    if (!r.ok) { out.textContent = "✗ " + (r.error || "失败"); return; }
    let s = r.verdict + "\n\n关节        cmd_q   actual    kp    diff\n";
    for (const j of r.joints) {
      s += `${j.joint.padEnd(7)} ${String(j.cmd).padStart(7)} ${String(j.actual).padStart(8)} ${String(j.kp).padStart(5)} ${String(j.diff).padStart(7)} ${j.ok ? "✓" : "✗"}\n`;
    }
    out.textContent = s;
  } finally { if (btn) btn.disabled = false; }
}
// spacebar = start/stop an episode while step 7 is focused (desk operation)
document.addEventListener("keydown", (ev) => {
  if (ev.code === "Space" && document.getElementById("panel-7")?.classList.contains("active")
      && ev.target.tagName !== "INPUT" && ev.target.tagName !== "TEXTAREA") {
    ev.preventDefault();
    recToggle();
  }
});

/* ── data viewer (step 8): browse recorded episodes ── */
let _epData = null, _epPath = "";
function fmtBytes(b) {
  b = b || 0;
  if (b >= 1e9) return (b / 1e9).toFixed(2) + "GB";
  if (b >= 1e6) return (b / 1e6).toFixed(1) + "MB";
  if (b >= 1e3) return (b / 1e3).toFixed(0) + "KB";
  return b + "B";
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
async function loadRecordings() {
  const r = await fetch("/api/recordings").then((x) => x.json()).catch(() => null);
  const sel = $("ep-sel"), ov = $("rec-overview");
  if (!r || !r.sessions || !r.sessions.length) {
    if (sel) sel.innerHTML = "<option>（暂无录制）</option>";
    if (ov) ov.innerHTML = "<div class='hint'>recordings/ 里还没有 episode。</div>";
    $("ep-info").textContent = "recordings/ 里还没有 episode。";
    return;
  }
  // dropdown drives the frame viewer; _epList (ordered) drives "next unlabeled"
  let opt = "", total = 0, tBytes = 0, tFrames = 0, nKeep = 0, nDrop = 0;
  _epList = [];
  for (const s of r.sessions) for (const e of s.episodes) {
    total++; tBytes += e.bytes || 0; tFrames += e.frames || 0;
    const c = e.curation;
    const labeled = !!(c && (c.keep === true || c.keep === false));
    if (c && c.keep === true) nKeep++; else if (c && c.keep === false) nDrop++;
    _epList.push({ path: e.path, curated: labeled });
    opt += `<option value="${e.path}">${s.session} / ${e.name} · ${e.frames}帧</option>`;
  }
  if (sel) sel.innerHTML = opt;
  // overview: per-session table of what was recorded + curation + delete controls
  let html = `<div class="note" style="margin-bottom:8px">共 <b>${total}</b> episode / ${r.sessions.length} 会话 · ${tFrames} 帧 · 占盘 <b>${fmtBytes(tBytes)}</b>
    &nbsp;|&nbsp; 取舍(只是分类标签，不删文件)：<b style="color:var(--ok)">✓要 ${nKeep}</b> · <b style="color:var(--fail)">✗不要 ${nDrop}</b> · 未定 ${total - nKeep - nDrop}</div>`;
  for (const s of r.sessions) {
    html += `<div class="rec-sess"><div class="row" style="justify-content:space-between;align-items:center">
      <strong>📁 ${esc(s.session)}</strong>
      <span class="hint">${s.count} 条 · ${s.frames} 帧 · ${fmtBytes(s.bytes)}
      &nbsp;<button class="btn" onclick="delRecording('${s.episodes[0] ? "/recordings/" + s.session : ""}','整个会话 ${esc(s.session)}（${s.count} 条）')">🗑 删会话</button></span></div>
      <table class="rec-tbl"><thead><tr><th>ep</th><th>目标</th><th>帧</th><th>时长</th><th>大小</th><th>模态</th><th>阶段</th><th>取舍</th><th></th></tr></thead><tbody>`;
    for (const e of s.episodes) {
      const mods = [(e.cameras || []).includes("head") ? "RGB" : "", (e.cameras || []).includes("depth") ? "D" : "", e.state ? "本体" : "", e.touch ? "触" : ""].filter(Boolean).join("·");
      const c = e.curation;
      const curCell = (c && c.keep === true)
        ? `<span style="color:var(--ok)">✓${(c.start != null || c.end != null) ? ` ${c.start ?? 0}-${c.end ?? (e.frames - 1)}` : ""}</span>`
        : (c && c.keep === false) ? `<span style="color:var(--fail)">✗不要</span>` : "—";
      html += `<tr>
        <td>${e.name.replace("episode_", "#")}</td>
        <td>${esc(e.goal) || "—"}</td>
        <td>${e.frames}</td>
        <td>${e.seconds || 0}s</td>
        <td>${fmtBytes(e.bytes)}</td>
        <td>${mods || "—"}</td>
        <td>${e.stages > 1 ? e.stages + "段" : "未标"}</td>
        <td>${curCell}</td>
        <td style="white-space:nowrap"><button class="btn" onclick="openEpisodePath('${e.path}')">打开</button>
        <button class="btn" onclick="delRecording('${e.path}','${esc(s.session)}/${esc(e.name)}（${e.frames}帧）')">🗑</button></td>
      </tr>`;
    }
    html += `</tbody></table></div>`;
  }
  if (ov) ov.innerHTML = html;
  $("ep-info").textContent = `共 ${total} 条 episode（${r.sessions.length} 个会话）。`;
  if ($("cur-remaining")) $("cur-remaining").textContent = `还剩 ${_curRemaining()} 条未标 / 共 ${_epList.length}`;
  if (sel && sel.value) openEpisodePath(sel.value);
}
async function delRecording(path, label) {
  if (!path) return;
  if (!(await uiConfirm(`删除 ${label}？\n\n文件将从磁盘永久删除，不可恢复。`))) return;
  const r = await api("/api/recordings/delete", { path });
  if (r.ok) {
    if (_epPath && (_epPath === path || _epPath.startsWith(path + "/"))) { _epData = null; _epPath = ""; const im = $("ep-frame"); if (im) im.removeAttribute("src"); }
    uiToast("已删除 " + label);
    loadRecordings();
  } else uiToast("删除失败：" + (r.error || "未知"), "danger");
}
function openEpisode() { openEpisodePath($("ep-sel").value); }
async function openEpisodePath(path) {
  if (!path || path[0] !== "/") return;
  const sel = $("ep-sel"); if (sel && sel.value !== path) sel.value = path;
  _epPath = path;
  const d = await fetch(`${path}/data.json`).then((x) => x.json()).catch(() => null);
  if (!d || !d.data) { $("ep-info").textContent = "读取 data.json 失败"; return; }
  _epData = d;
  const n = d.data.length;
  const sl = $("ep-slider"); sl.max = Math.max(0, n - 1); sl.value = 0;
  const info = d.info || {};
  $("ep-info").textContent = `目标：${(d.text || {}).goal || "?"} · ${n} 帧 · 相机：${(info.cameras || []).join("/") || "无"} · 触觉：${info.tactile_keys ? "有" : "无"}`;
  // derive existing stage boundaries from the data (for re-annotation)
  _epBounds = []; let prev = 0;
  for (let i = 0; i < n; i++) {
    const st = d.data[i].stage || 0;
    if (st !== prev) { _epBounds.push({ frame: i, stage: st, label: d.data[i].stage_label || "" }); prev = st; }
  }
  renderBounds();
  if ($("anno-status")) { $("anno-status").textContent = _epBounds.length ? "已有标注" : "未标注"; $("anno-status").className = "pill"; }
  // load this episode's keep/discard + useful range (curation.json) if present
  const curRaw = await fetch(`${path}/curation.json`).then((x) => (x.ok ? x.json() : null)).catch(() => null);
  _cur = curRaw ? { keep: curRaw.keep ?? null, start: curRaw.start ?? null, end: curRaw.end ?? null }
                : { keep: null, start: null, end: null };
  renderCur();
  showFrame(0);
}

/* ── offline stage annotation (step 8) ── */
let _epBounds = [];
function renderBounds() {
  const el = $("anno-list"); if (!el) return;
  const sorted = [..._epBounds].sort((a, b) => a.frame - b.frame);
  el.textContent = sorted.length ? sorted.map((b) => `f${b.frame} → 阶段${b.stage} ${b.label}`).join("\n") : "（无标注，全 stage 0）";
}
function _annoDirty() { if ($("anno-status")) { $("anno-status").textContent = "未保存"; $("anno-status").className = "pill err"; } }
function markStage(stage, label) {
  if (!_epData) return;
  const fr = parseInt($("ep-slider").value) || 0;
  _epBounds = _epBounds.filter((b) => b.frame !== fr);
  _epBounds.push({ frame: fr, stage, label });
  renderBounds(); _annoDirty();
}
function undoStage() { _epBounds.pop(); renderBounds(); _annoDirty(); }
function clearStages() { _epBounds = []; renderBounds(); _annoDirty(); }
async function saveStages() {
  if (!_epPath) return;
  const r = await api("/api/recordings/annotate", { path: _epPath, boundaries: _epBounds });
  if (r.ok) { $("anno-status").textContent = `已保存（${r.boundaries} 边界 / ${r.frames} 帧）`; $("anno-status").className = "pill ok"; }
  else { $("anno-status").textContent = "保存失败：" + (r.error || ""); $("anno-status").className = "pill err"; }
}

/* ── data curation: keep/discard + useful frame range (step 8), auto-saved ── */
let _cur = { keep: null, start: null, end: null };
let _epList = [];   // ordered [{path, curated}] for "next unlabeled" navigation
function _curRemaining() { return _epList.filter((x) => !x.curated).length; }
function renderCur() {
  const n = _epData ? _epData.data.length : 0;
  const s = _cur.start, e = _cur.end;
  const rt = (s == null && e == null) ? `全长（${n} 帧）`
    : `${s ?? 0} – ${e ?? (n - 1)}（${(e ?? (n - 1)) - (s ?? 0) + 1} 帧）`;
  if ($("cur-range")) $("cur-range").textContent = rt;
  const kb = $("cur-keep"), db = $("cur-drop");
  if (kb) kb.className = "btn" + (_cur.keep === true ? " primary" : "");
  if (db) db.className = "btn" + (_cur.keep === false ? " danger-btn" : "");
  const st = $("cur-status");
  if (st) {
    if (_cur.keep === true) { st.textContent = "✓ 要 · " + rt; st.className = "pill ok"; }
    else if (_cur.keep === false) { st.textContent = "✗ 不要"; st.className = "pill err"; }
    else { st.textContent = "未定"; st.className = "pill"; }
  }
  if ($("cur-remaining")) $("cur-remaining").textContent = `还剩 ${_curRemaining()} 条未标 / 共 ${_epList.length}`;
}
async function saveCur() {
  if (!_epPath) return;
  // reflect the decision in the in-memory list so "next unlabeled" + the counter stay live
  const it = _epList.find((x) => x.path === _epPath);
  if (it) it.curated = (_cur.keep === true || _cur.keep === false);
  if ($("cur-remaining")) $("cur-remaining").textContent = `还剩 ${_curRemaining()} 条未标 / 共 ${_epList.length}`;
  await api("/api/recordings/curate", { path: _epPath, keep: _cur.keep, start: _cur.start, end: _cur.end });
}
function curNextUnlabeled() {
  if (!_epList.length) { uiToast("先「刷新列表」", "warn"); return; }
  const idx = _epList.findIndex((x) => x.path === _epPath);
  for (let k = 1; k <= _epList.length; k++) {     // forward from current, wrap, skip current
    const j = (idx + k) % _epList.length;
    if (j === idx) continue;
    if (!_epList[j].curated) { openEpisodePath(_epList[j].path); return; }
  }
  uiToast(`🎉 标完了！没有别的未标条目（共 ${_epList.length} 条全有取舍）`, "ok");
}
function setCurKeep(k) { if (!_epData) return; _cur.keep = k; renderCur(); saveCur(); uiToast(k ? "标记：要" : "标记：不要"); }
function setCurStart() {
  if (!_epData) return;
  _cur.start = parseInt($("ep-slider").value) || 0;
  if (_cur.keep == null) _cur.keep = true;
  if (_cur.end != null && _cur.end < _cur.start) _cur.end = _cur.start;
  renderCur(); saveCur();
}
function setCurEnd() {
  if (!_epData) return;
  _cur.end = parseInt($("ep-slider").value) || 0;
  if (_cur.keep == null) _cur.keep = true;
  if (_cur.start != null && _cur.start > _cur.end) _cur.start = _cur.end;
  renderCur(); saveCur();
}
function resetCurRange() { if (!_epData) return; _cur.start = null; _cur.end = null; renderCur(); saveCur(); }
function curJump(which) { const v = which === "start" ? _cur.start : _cur.end; if (v != null) showFrame(v); }
function showFrame(i) {
  if (!_epData) return;
  i = Math.max(0, Math.min(_epData.data.length - 1, parseInt(i) || 0));
  $("ep-slider").value = i;
  const f = _epData.data[i];
  const img = $("ep-frame");
  img.alt = `帧 ${f.idx}（stage ${f.stage}）`;
  img.onerror = () => { img.alt = `帧 ${f.idx} 图像缺失`; };
  img.src = `${_epPath}/frames/${String(f.idx).padStart(6, "0")}_head.jpg`;
  $("ep-fidx").textContent = `${i + 1} / ${_epData.data.length}`;
  const r2 = (x) => (typeof x === "number" ? x.toFixed(2) : (x ?? "-"));
  const a = f.action || {}, tac = f.tactile || {};
  const peak = (side) => { const t = tac[side]; return (t && t.normal_force) ? Math.max(...t.normal_force) : "-"; };
  let s = `帧 ${f.idx}  stage=${f.stage}${f.stage_label ? "(" + f.stage_label + ")" : ""}\n`;
  s += f.action ? `动作: L grip=${r2(a.left_grip)} trig=${r2(a.left_trigger)} | R grip=${r2(a.right_grip)} trig=${r2(a.right_trigger)}\n`
                : "动作: (该帧无 pose)\n";
  s += `触觉峰值 normal_force: 左=${peak("left")} 右=${peak("right")}`;
  if (f.state && f.state.joint_q) s += `\n本体: ${f.state.joint_q.length} 关节 q` + (f.state.left_hand_q ? ` · 左手 ${f.state.left_hand_q.length}` : "");
  $("ep-frame-info").textContent = s;
}
function frameStep(d) { if (_epData) showFrame((parseInt($("ep-slider").value) || 0) + d); }
async function exportMp4(kind = "head") {
  if (!_epPath) { uiToast("先选一条 episode", "warn"); return; }
  const btnId = kind === "rgbd" ? "ep-export-rgbd" : "ep-export";
  const btn = $(btnId), label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "导出中…"; }
  uiToast(kind === "rgbd" ? "正在编码 RGBD（RGB|彩色深度并排）…" : "正在用 ffmpeg 编码 MP4…（按帧数,几秒）");
  const r = await api("/api/recordings/export", { path: _epPath, kind });
  if (btn) { btn.disabled = false; btn.textContent = label || "⬇ 导出"; }
  if (r.ok) {
    const a = document.createElement("a");
    a.href = r.url;                                         // /recordings/<sess>/<ep>/<file>.mp4
    const suffix = kind === "rgbd" ? "_rgbd" : "";
    a.download = _epPath.split("/").slice(-2).join("_") + suffix + ".mp4";  // <sess>_<ep>[_rgbd].mp4
    document.body.appendChild(a); a.click(); a.remove();
    uiToast(`已导出 ${(r.size / 1e6).toFixed(1)}MB @ ${r.fps}fps，开始下载`);
  } else {
    uiToast("导出失败：" + (r.error || "未知"), "danger");
  }
}

/* ── in-page confirm modal ── the native confirm() can be permanently
   suppressed by the browser ("阻止此页面再创建对话框"), which makes every
   danger button that gates on it look unclickable ("点不动"). This self-drawn
   modal never gets blocked. Returns a Promise<bool>. ── */
function uiConfirm(message) {
  if (window.__t) message = window.__t(message);   // i18n: translate before showing
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center";
    const box = document.createElement("div");
    box.style.cssText = "background:#1b2433;border:1px solid #3a4a60;border-radius:10px;max-width:460px;padding:22px;color:#e6edf6;box-shadow:0 10px 40px rgba(0,0,0,.5)";
    const msg = document.createElement("div");
    msg.style.cssText = "white-space:pre-wrap;line-height:1.55;margin-bottom:18px;font-size:15px";
    msg.textContent = message;
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:12px;justify-content:flex-end";
    const cancel = document.createElement("button");
    cancel.className = "btn"; cancel.textContent = "取消";
    const ok = document.createElement("button");
    ok.className = "btn danger-btn"; ok.textContent = "确认";
    const done = (v) => { ov.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") done(false); else if (e.key === "Enter") done(true); };
    cancel.onclick = () => done(false);
    ok.onclick = () => done(true);
    document.addEventListener("keydown", onKey);
    row.append(cancel, ok); box.append(msg, row); ov.append(box);
    document.body.append(ov);
    ok.focus();
  });
}

/* ── self-drawn toast — native alert() can be suppressed by the browser (the
   same reason confirm() was abandoned), which on a safety tool means a click
   (incl. E-STOP) can give zero feedback. This in-page toast can't be blocked.
   kind: ok / warn / danger. danger is sticky until clicked; others auto-dismiss. ── */
function uiToast(message, kind = "ok") {
  if (window.__t) message = window.__t(message);   // i18n: translate before showing
  let host = $("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    host.style.cssText = "position:fixed;left:50%;bottom:74px;transform:translateX(-50%);z-index:10000;display:flex;flex-direction:column;gap:8px;align-items:center;max-width:92vw";
    document.body.append(host);
  }
  const C = { ok: ["#15351f", "#7fdc9b", "✓"], warn: ["#3a3214", "#f1d77f", "⚠"], danger: ["#3a1717", "#ff9b9b", "■"] };
  const [bg, fg, glyph] = C[kind] || C.ok;
  const t = document.createElement("div");
  t.style.cssText = `background:${bg};color:${fg};border:1px solid ${fg}55;border-radius:8px;padding:10px 16px;font-size:14px;line-height:1.5;white-space:pre-wrap;box-shadow:0 6px 24px rgba(0,0,0,.5);cursor:pointer;max-width:560px`;
  t.textContent = `${glyph} ${message}`;
  t.onclick = () => t.remove();
  host.append(t);
  if (kind !== "danger") setTimeout(() => t.remove(), 5500);
  return t;
}

/* ── real G1 high-level mode switch (release for lowcmd / re-select) ── */
async function g1Mode(action, name) {
  const out = $("g1mode-out");
  const pill = $("pill-g1mode");
  if (g1Mode._busy) return;                       // ignore double-clicks
  if (action === "release" &&
      !(await uiConfirm("⚠ 释放高层模式 = 原生运控脱离，机器人会变软、靠吊架承重！\n\n确认：① 已停 deploy ② 吊架张紧 ③ 急停在手？"))) {
    return;
  }
  g1Mode._busy = true;
  // immediate, unmistakable feedback — DDS cold-start can take ~15s
  out.textContent = "⏳ 执行中…（约 15s，正在与机器人 DDS 通信，请勿重复点击）";
  pill.textContent = "执行中…"; pill.className = "proc-pill";
  try {
    const body = { action };
    if (action === "release") body.confirm = true;
    if (name) body.name = name;
    const r = await api("/api/g1/mode", body);
    out.textContent = JSON.stringify(r, null, 2);
    const m = r.after !== undefined ? r.after : r.mode;
    if (r.ok && action === "release") { pill.textContent = "已释放 (lowcmd 就绪)"; pill.className = "proc-pill ok"; }
    else if (m) { pill.textContent = m.name || "(无)"; pill.className = "proc-pill"; }
    else if (!r.ok) { pill.textContent = "失败"; pill.className = "proc-pill err"; }
  } finally {
    g1Mode._busy = false;
  }
}

async function estop() {
  const btn = $("estop");
  // instant, unmissable acknowledgement BEFORE the network await
  if (btn) {
    btn.classList.add("estop-sent");
    const old = btn.textContent; btn.textContent = "■ 急停已发送…";
    setTimeout(() => { btn.classList.remove("estop-sent"); btn.textContent = old; }, 2000);
  }
  try {
    const res = await api("/api/estop", {});
    if (res._neterr || res.ok === false) {
      // the one click that MUST never fail silently
      uiToast("急停指令可能未送达控制台！\n立即用手柄 A+B+X+Y，或在部署终端按 O 急停。\n" + (res.error || ""), "danger");
    } else {
      const acts = res.actions || [];
      const failed = acts.some((l) => /FAIL/i.test(l));
      uiToast("急停已触发：\n" + (acts.join("\n") || "(无动作)"), failed ? "danger" : "ok");
    }
  } finally {
    poll();
  }
}

async function picoProbe() {
  $("probe-status").textContent = "探测中…";
  $("probe-status").className = "proc-pill starting";
  await api("/api/pico/probe", {});
}

async function confirmSafety() {
  const checklist = {};
  document.querySelectorAll(".safety-cb").forEach((cb) => { checklist[cb.dataset.key] = cb.checked; });
  const res = await api("/api/safety/confirm", {
    checklist, typed: $("safety-typed").value,
  });
  if (!res.ok) { uiToast(res.error || "安全确认失败", "warn"); return; }
  safetyToken = res.safety_token;
  updateSafetyUI(true, res.expires_in);
}

function updateSafetyUI(armed, ttl) {
  const st = $("safety-status");
  if (armed) {
    const t = ttl == null ? 120 : ttl;
    st.textContent = `已解锁 · 剩余 ${t}s（单次）`;
    st.className = "proc-pill " + (t < 30 ? "starting" : "ready");  // amber when about to expire
    $("btn-deploy_real").disabled = false;
  } else {
    st.textContent = "未解锁";
    st.className = "proc-pill";
    $("btn-deploy_real").disabled = true;
  }
}

async function completeStep(n) {
  await api(`/api/steps/${n}/complete`, {});
  showStep(Math.min(5, Number(n) + 1));
  poll();
}

function copyText(id) {
  navigator.clipboard.writeText($(id).textContent)
    .then(() => uiToast("已复制到剪贴板", "ok"))
    .catch(() => uiToast("复制失败，请手动选中复制", "warn"));
}

/* ── rendering ── */
function renderChecks(list) {
  const grid = $("checks-env");
  grid.innerHTML = "";
  let pcOk = false;
  for (const c of list) {
    const div = document.createElement("div");
    div.className = `check-card ${c.level}`;
    div.innerHTML =
      `<div class="label">${c.ok ? "✓" : c.level === "warn" ? "⚠" : "✗"} ${c.label}</div>` +
      `<div class="detail">${esc(c.detail)}</div>` +
      (c.fix_hint ? `<div class="fix">↳ ${esc(c.fix_hint)}</div>` : "");
    grid.appendChild(div);
    if (c.id === "pc_service") pcOk = c.ok;
  }
  $("pcservice-status").textContent = pcOk ? " ✓ PC Service 已安装" : " ✗ 尚未安装";
  $("pcservice-status").style.color = pcOk ? "var(--ok)" : "var(--fail)";
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function pillFor(name, procs, elId) {
  const el = $(elId);
  if (!el) return;
  const p = procs[name];
  if (!p) { el.textContent = "未运行"; el.className = "proc-pill"; return; }
  const map = {
    starting: ["启动中…", "starting"], ready: ["运行中 ✓", "ready"],
    stopping: ["停止中…", "starting"], exited: ["已退出", ""],
    failed: ["异常退出", "failed"], needs_manual: ["需人工处理!", "needs_manual"],
    idle: ["未运行", ""],
  };
  let [txt, cls] = map[p.status] || [p.status, ""];
  if (p.status === "starting" && p.started_at && STATE && STATE.now) {
    const secs = Math.round(STATE.now - p.started_at);
    txt = `启动中… ${secs}s`;
    if (secs > (name.startsWith("deploy") ? 50 : 20)) txt += "（较久·查日志）";
  }
  el.textContent = txt + (p.pid && p.alive ? ` (pid ${p.pid})` : "");
  el.className = "proc-pill " + cls;
}

function renderState(s) {
  STATE = s;
  const procs = s.procs || {};

  // process pills
  pillFor("install_pico", procs, "pill-install_pico");
  // pc_service: runService.sh 是启动器，fork 守护进程后自己退出。
  // 用真实守护进程状态（60061 socket）显示，而不是启动器进程的生命周期。
  const pcEl = $("pill-pc_service");
  if (pcEl) {
    if (s.pc_service_alive) {
      pcEl.textContent = "运行中 ✓"; pcEl.className = "proc-pill ready";
    } else {
      pillFor("pc_service", procs, "pill-pc_service");
    }
  }
  pillFor("sim_loop", procs, "pill-sim_loop");
  pillFor("deploy_sim", procs, "pill-deploy_sim");
  pillFor("deploy_real", procs, "pill-deploy_real");
  pillFor("pico_streamer", procs, "pill-pico_streamer");
  pillFor("pico_streamer", procs, "pill-pico_streamer-5");
  pillFor("brainco_hand", procs, "pill-brainco_hand");
  pillFor("gui_drive", procs, "pill-gui_drive");
  pillFor("recorder", procs, "pill-recorder");
  pillFor("convert_dataset", procs, "pill-convert_dataset");
  pillFor("act_train", procs, "pill-act_train");
  pillFor("act_infer", procs, "pill-act_infer");
  // streamer 退出时 xrt SDK 析构必崩(rc=-6/SIGABRT)，且 A+B+X+Y 急停也会主动退出——
  // 都属正常，不显示成「异常退出」。
  const ps = procs["pico_streamer"];
  if (ps && !ps.alive && (ps.status === "failed" || ps.status === "exited")) {
    for (const id of ["pill-pico_streamer", "pill-pico_streamer-5"]) {
      const el = $(id);
      if (el) { el.textContent = "已停止"; el.className = "proc-pill"; }
    }
  }

  // step 3: 列出工作站所有可用局域网 IP，标注 WiFi / 有线，头显填哪个都行
  const allIps = (s.ips && s.ips.all) || [];
  const robotPrefix = (s.ips && s.ips.robot_prefix) || "192.168.123.";
  const ipEl = $("wifi-ip"), hintEl = $("wifi-ip-hint");
  if (allIps.length) {
    ipEl.innerHTML = allIps.map((w) => {
      const isWired = w[1].startsWith(robotPrefix);
      const tag = isWired ? "有线/机器人网" : "WiFi";
      return `<span style="display:inline-block;margin-right:18px">${w[1]} <small style="color:var(--dim);font-size:13px">(${tag} · ${w[0]})</small></span>`;
    }).join("");
    if (hintEl) hintEl.textContent =
      "头显的 XRoboToolkit app 里填上面任意一个能和头显互通的 IP。" +
      "走有线/USB 共享填有线 IP，走 WiFi 填 WiFi IP。";
  } else {
    ipEl.textContent = "未检测到任何局域网 IP";
    if (hintEl) hintEl.textContent = "⚠ 工作站网络全部断开，请检查网线/WiFi。";
  }
  $("apk-link").href = s.config.apk_url;
  $("deb-cmd").textContent =
    `wget '${s.config.deb_url}' -O /tmp/xrobotoolkit.deb && sudo dpkg -i /tmp/xrobotoolkit.deb`;
  const pr = s.pico || {};
  if (pr.running) {
    $("probe-status").textContent = "探测中…"; $("probe-status").className = "proc-pill starting";
  } else if (pr.result) {
    const r = pr.result;
    // 分级链路灯：SDK → PC Service → 头显链路 → Tracker 配对 → Tracker 数据 → 全身就绪
    const chain = [
      ["SDK", r.sdk], ["PC Service", r.init],
      ["头显链路", r.headset || r.controllers],
      [`Tracker(${r.trackers_paired || 0})`, (r.trackers_paired || 0) > 0],
      ["Tracker数据", (r.motion_data || 0) > 0],
      ["全身就绪", r.body_data],
    ];
    const lights = chain.map(([n, ok]) => `${ok ? "✅" : "⬜"}${n}`).join("  ");
    if (r.body_data) {
      $("probe-status").textContent = "全身数据正常 ✓"; $("probe-status").className = "proc-pill ready";
    } else {
      $("probe-status").textContent = "未就绪"; $("probe-status").className = "proc-pill failed";
    }
    $("probe-detail").innerHTML =
      `<div style="margin:4px 0;font-size:13px">${lights}</div>` +
      (r.error ? `<div style="color:var(--warn)">${esc(r.error)}</div>` : "");
  }

  // step 4 sequencing gates
  const alive = (n) => procs[n] && procs[n].alive;
  const ready = (n) => procs[n] && procs[n].status === "ready" && procs[n].alive;
  $("btn-sim_loop").disabled = alive("sim_loop");
  $("btn-deploy_sim").disabled = !alive("sim_loop") || alive("deploy_sim") || alive("deploy_real");
  $("btn-pico_streamer").disabled = !(ready("deploy_sim") || ready("deploy_real")) || alive("pico_streamer");
  $("btn-pico_streamer-5").disabled = !ready("deploy_real") || alive("pico_streamer");

  // step 4 completion gate: CONTROL seen + estop checkbox
  const t = s.teleop || {};
  const controlSeen = sessionStorage.getItem("control_seen") === "1" || t.deploy_control;
  if (t.deploy_control) sessionStorage.setItem("control_seen", "1");
  $("complete-4").disabled = !(controlSeen && $("estop-mastered").checked);

  // step 5 auto checks
  const simClear = !alive("sim_loop") && !alive("deploy_sim");
  $("auto-sim-clear").textContent = simClear ? "✓ 已停止" : "✗ 还在运行";
  $("auto-sim-clear").style.color = simClear ? "var(--ok)" : "var(--fail)";
  const robotNet = s.ips && s.ips.robot_iface;
  $("auto-robot-net").textContent = robotNet ? `✓ ${robotNet[0]}=${robotNet[1]}` : "✗ 离线";
  $("auto-robot-net").style.color = robotNet ? "var(--ok)" : "var(--fail)";

  // needs-manual: step-5 banner + GLOBAL red strip (deploy stuck = unmissable)
  const dr = procs["deploy_real"];
  const needsManual = (dr && dr.status === "needs_manual") ||
    (procs["deploy_sim"] && procs["deploy_sim"].status === "needs_manual");
  const nmb = $("needs-manual-banner");
  if (nmb) nmb.classList.toggle("hidden", !(dr && dr.status === "needs_manual"));
  document.body.classList.toggle("needs-manual", !!needsManual);

  // safety token countdown from server view
  if (safetyToken && s.safety && !s.safety.armed) { safetyToken = null; updateSafetyUI(false); }
  if (safetyToken && s.safety && s.safety.armed) updateSafetyUI(true, s.safety.ttl);

  // bottom bar pills
  const smEl = $("pill-streammode");
  smEl.textContent = "遥操模式: " + streamModeLabel(t.stream_mode);
  smEl.className = "pill" + (t.stream_mode === "POSE" ? " pose-mode" : (t.stream_mode && t.stream_mode !== "OFF" ? " active-mode" : ""));
  $("pill-locomode").textContent = "步态: " + locoModeLabel(t.loco_mode, t.loco_mode_name);
  const ds = $("pill-deploystate");
  if (needsManual) {
    ds.textContent = "部署: ⚠ 需人工处理 — 勿 kill -9";
    ds.className = "pill err";
  } else if (t.active_deploy && t.deploy_waiting_lowstate && !t.deploy_control) {
    ds.textContent = "部署: ⚠ 收不到机器人 LowState（查 已释放高层模式 / 调试模式 / 机器人网）";
    ds.className = "pill err";
  } else {
    ds.textContent = "部署: " + (t.active_deploy
      ? `${t.active_deploy === "deploy_real" ? "真机" : "仿真"} ${t.deploy_control ? "控制中" : (t.deploy_ready ? "就绪(待 start)" : "初始化…")}`
      : "–");
    ds.className = "pill" + (t.deploy_control ? " pose-mode" : (t.deploy_ready ? " active-mode" : ""));
  }

  // checks cache on first load
  if (s.checks_cache && s.checks_cache.env && !renderState._checksDone) {
    renderChecks(s.checks_cache.env);
    renderState._checksDone = true;
  }

  // stepper done markers
  for (const [n, v] of Object.entries(s.steps || {})) {
    const tab = document.querySelector(`.step-tab[data-step="${n}"]`);
    if (tab && v.done) tab.classList.add("done");
  }
}

/* ── log polling ── */
async function pollLogs() {
  for (const name of LOG_PROCS) {
    if (!STATE || !STATE.procs || !STATE.procs[name]) continue;
    const seq = logSeqs[name] || 0;
    try {
      const res = await fetch(`/api/logs/${name}?since=${seq}`).then((r) => r.json());
      if (res.lines && res.lines.length) {
        logSeqs[name] = res.seq;
        for (const paneId of LOG_PANES[name] || []) {
          const pane = $(paneId);
          if (!pane) continue;
          pane.textContent += res.lines.join("\n") + "\n";
          const lines = pane.textContent.split("\n");
          if (lines.length > 600) pane.textContent = lines.slice(-500).join("\n");
          if (!pane.classList.contains("collapsed")) pane.scrollTop = pane.scrollHeight;
        }
        if (name === "recorder") {
          const st = res.lines.filter((l) => l.includes("STATUS")).pop();
          if (st) {
            const g = (k) => (st.match(new RegExp(k + "=(\\S+)")) || [])[1];
            const rec = g("rec"), ep = g("ep"), fr = g("frames"), sg = g("stage"), sv = g("saved");
            _recOn = (rec === "ON");
            const au = g("auto"), po = g("pose");
            const ap = $("rec-auto-pill");
            if (ap) { const on = au === "ON"; ap.textContent = on ? `auto:ON · ${po === "POSE" ? "POSE→录" : "等POSE"}` : "auto: off"; ap.className = "pill" + (on ? " ok" : ""); }
            const ac = $("rec-auto"); if (ac && document.activeElement !== ac) ac.checked = (au === "ON");
            const sp = $("rec-status");
            if (sp) { sp.textContent = `rec:${rec} saved:${sv}`; sp.className = "pill" + (_recOn ? " ok" : ""); }
            if ($("rec-ep")) $("rec-ep").textContent = _recOn ? `录制中 ep=${ep} 帧=${fr}` : "未录";
            if (sg && $("rec-stage")) $("rec-stage").textContent = sg;
            const tg = $("rec-toggle");
            if (tg) { tg.textContent = _recOn ? "■ 停止保存（空格）" : "● 开始这条（空格）"; tg.className = "btn " + (_recOn ? "danger-btn" : "primary"); }
            // light the data-source readiness chips from src=[...]
            const srcRaw = (st.match(/src=\[([^\]]*)\]/) || [])[1] || "";
            const active = new Set(srcRaw.split(",").map((s) => s.trim()));
            for (const k of ["head", "depth", "pose", "lowstate", "lhand", "rhand", "ltouch", "rtouch"]) {
              const el = $("src-" + k);
              if (el) el.className = "srcdot" + (active.has(k) ? " on" : "");
            }
            // camera frozen = silently recording duplicate frames -> flag it
            const hd = $("src-head");
            if (hd) {
              if (/CAM_FROZEN/.test(st)) { hd.className = "srcdot"; hd.textContent = "⚠相机冻帧"; }
              else hd.textContent = "相机";
            }
          }
        }
      }
    } catch (e) { /* transient */ }
  }
}

/* ── main poll loop ── */
let _lastPollOk = Date.now();
async function poll() {
  const cd = $("conn-dot");
  try {
    const s = await fetch("/api/state").then((r) => r.json());
    renderState(s);
    _lastPollOk = Date.now();
    if (cd) { cd.classList.add("on"); cd.textContent = "● 已连接"; cd.title = "状态实时刷新中"; }
    document.body.classList.remove("stale");
  } catch (e) {
    if (cd) { cd.classList.remove("on"); cd.textContent = "⚠ 连接断开"; cd.title = "无法连到控制台"; }
    document.body.classList.add("stale");
  }
}
// staleness watchdog: a frozen loop must stop looking live
setInterval(() => {
  const dt = Date.now() - _lastPollOk;
  if (dt > 3000) {
    document.body.classList.add("stale");
    const cd = $("conn-dot");
    if (cd && cd.classList.contains("on")) cd.textContent = `⚠ 状态 ${Math.round(dt / 1000)}s 未更新`;
  }
}, 1000);

$("estop-mastered").addEventListener("change", poll);

// restore persisted prefs (task goal, vis-window checkbox)
(function restorePrefs() {
  const g = localStorage.getItem("wbc_goal");
  if (g) for (const id of ["stack-goal", "rec-goal"]) { const el = $(id); if (el) el.value = g; }
  const v = localStorage.getItem("wbc_vis");
  if (v !== null && $("vis-toggle")) $("vis-toggle").checked = v === "1";
})();
// keep the two goal inputs + localStorage in sync
function _syncGoal(v) {
  localStorage.setItem("wbc_goal", v);
  for (const id of ["stack-goal", "rec-goal"]) { const el = $(id); if (el && el.value !== v) el.value = v; }
}
for (const id of ["stack-goal", "rec-goal"]) {
  const el = $(id); if (el) el.addEventListener("input", () => _syncGoal(el.value));
}
if ($("vis-toggle")) $("vis-toggle").addEventListener("change", (e) => localStorage.setItem("wbc_vis", e.target.checked ? "1" : "0"));
// arrow-key frame scrubbing in the data viewer (step 8)
document.addEventListener("keydown", (ev) => {
  if (!$("panel-8") || !$("panel-8").classList.contains("active")) return;
  if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT" || ev.target.tagName === "TEXTAREA") return;
  if (ev.key === "ArrowLeft") { ev.preventDefault(); frameStep(ev.shiftKey ? -10 : -1); }
  else if (ev.key === "ArrowRight") { ev.preventDefault(); frameStep(ev.shiftKey ? 10 : 1); }
  else if (ev.key === "Enter") { ev.preventDefault(); curNextUnlabeled(); }
  else if (!_epData) return;
  // curation shortcuts: I=in, O=out, K=keep, X=discard, Enter=done->next unlabeled
  else if (ev.key === "i" || ev.key === "I") { ev.preventDefault(); setCurStart(); }
  else if (ev.key === "o" || ev.key === "O") { ev.preventDefault(); setCurEnd(); }
  else if (ev.key === "k" || ev.key === "K") { ev.preventDefault(); setCurKeep(true); }
  else if (ev.key === "x" || ev.key === "X") { ev.preventDefault(); setCurKeep(false); }
});

showStep(localStorage.getItem("wbc_step") || "1");
poll();
setInterval(poll, 1000);
setInterval(pollLogs, 1000);
setInterval(_trainPoll, 4000);  // live queue + model loss while on step 10

// live robot viewpoint while ACT inference is running (act_infer mirrors the
// head-camera frame; the console serves it). Preload-swap to avoid flicker.
let _camFrames = 0, _camWin = Date.now();
function _inferCamTick() {
  const wrap = $("infer-cam-wrap");
  if (!wrap) return;
  if (!procAlive("act_infer")) { wrap.style.display = "none"; return; }
  wrap.style.display = "";
  const img = $("infer-cam");
  const n = new Image();
  n.onload = () => {
    img.src = n.src;
    _camFrames++;
    const dt = Date.now() - _camWin;
    if (dt >= 1000) {
      const f = $("infer-cam-fps");
      if (f) { f.textContent = (_camFrames * 1000 / dt).toFixed(0) + " fps"; f.className = "pill ok"; }
      _camFrames = 0; _camWin = Date.now();
    }
  };
  n.onerror = () => { const f = $("infer-cam-fps"); if (f) { f.textContent = "等画面…"; f.className = "pill"; } };
  n.src = "/api/infer/frame?t=" + Date.now();
  // depth view — only RGBD models write it; 404 hides the pane
  const dwrap = $("infer-depth-wrap");
  const dn = new Image();
  dn.onload = () => { const di = $("infer-depth"); if (di) di.src = dn.src; if (dwrap) dwrap.style.display = ""; };
  dn.onerror = () => { if (dwrap) dwrap.style.display = "none"; };
  dn.src = "/api/infer/depth_frame?t=" + Date.now();
}
setInterval(_inferCamTick, 130);
