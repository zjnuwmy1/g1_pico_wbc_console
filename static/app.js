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
const LOG_PROCS = ["install_pico", "pc_service", "sim_loop", "deploy_sim", "deploy_real", "pico_streamer", "brainco_hand", "gui_drive", "recorder"];
// pico_streamer log is mirrored into two panes (step 4 + step 5)
const LOG_PANES = {
  install_pico: ["log-install_pico"], pc_service: ["log-pc_service"],
  sim_loop: ["log-sim_loop"], deploy_sim: ["log-deploy_sim"],
  deploy_real: ["log-deploy_real"],
  pico_streamer: ["log-pico_streamer", "log-pico_streamer-5"],
  brainco_hand: ["log-brainco_hand"],
  gui_drive: ["log-gui_drive"],
  recorder: ["log-recorder"],
};

const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opts = body !== undefined
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opts);
  return r.json();
}

/* ── stepper navigation ── */
function showStep(n) {
  document.querySelectorAll(".step-panel").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll(".step-tab").forEach((t) => t.classList.remove("active"));
  $(`panel-${n}`).classList.add("active");
  document.querySelector(`.step-tab[data-step="${n}"]`).classList.add("active");
  localStorage.setItem("wbc_step", n);
}
document.querySelectorAll(".step-tab").forEach((t) =>
  t.addEventListener("click", () => showStep(t.dataset.step)));

/* ── actions ── */
async function runChecks(scope) {
  const res = await api("/api/checks/run", { scope });
  if (scope === "env") renderChecks(res.checks);
}

async function startProc(name) {
  const body = {};
  if (name === "pico_streamer") body.vis = $("vis-toggle").checked;
  if (name === "deploy_real") {
    if (!safetyToken) { alert("请先完成安全确认门"); return; }
    body.safety_token = safetyToken;
  }
  const res = await api(`/api/procs/${name}/start`, body);
  if (!res.ok && res.error) alert(res.error);
  if (name === "deploy_real") { safetyToken = null; updateSafetyUI(false); }
  poll();
}

async function stopProc(name) { await api(`/api/procs/${name}/stop`, {}); poll(); }

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

/* ── episode recorder (StageAct data collection): relay to /api/record ── */
let _recOn = false;
async function recCmd(cmd, extra) {
  if (cmd === "discard" && !(await uiConfirm("丢弃当前 episode？此条数据不保存。"))) return;
  await api("/api/record", Object.assign({ cmd }, extra || {}));
}
function recToggle() { recCmd(_recOn ? "stop" : "start"); }   // one button / spacebar / grip+A
async function recPrepare() {
  // one-click: start the recorder backend (if not already up) + set the goal
  if (!(STATE && STATE.procs && STATE.procs.recorder && STATE.procs.recorder.alive)) {
    await api("/api/procs/recorder/start", {});
    await new Promise((r) => setTimeout(r, 1500));   // let it bind sockets / DDS
  }
  const goal = (($("rec-goal") && $("rec-goal").value) || "").trim() || "拿瓶子";
  await api("/api/record", { cmd: "goal", goal });
  poll();
}
function recStage(stage, label) {
  api("/api/record", { cmd: "stage", stage, label });
  if ($("rec-stage")) $("rec-stage").textContent = String(stage);
}
// spacebar = start/stop an episode while step 7 is focused (desk operation)
document.addEventListener("keydown", (ev) => {
  if (ev.code === "Space" && document.getElementById("panel-7")?.classList.contains("active")
      && ev.target.tagName !== "INPUT" && ev.target.tagName !== "TEXTAREA") {
    ev.preventDefault();
    recToggle();
  }
});

/* ── in-page confirm modal ── the native confirm() can be permanently
   suppressed by the browser ("阻止此页面再创建对话框"), which makes every
   danger button that gates on it look unclickable ("点不动"). This self-drawn
   modal never gets blocked. Returns a Promise<bool>. ── */
function uiConfirm(message) {
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
  const res = await api("/api/estop", {});
  alert("急停已触发:\n" + (res.actions || []).join("\n"));
  poll();
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
  if (!res.ok) { alert(res.error); return; }
  safetyToken = res.safety_token;
  updateSafetyUI(true, res.expires_in);
}

function updateSafetyUI(armed, ttl) {
  const st = $("safety-status");
  if (armed) {
    st.textContent = `已解锁 (${ttl || 120}s 内有效, 单次)`;
    st.className = "proc-pill ready";
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
  navigator.clipboard.writeText($(id).textContent).then(() => alert("已复制"));
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
  const [txt, cls] = map[p.status] || [p.status, ""];
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

  // needs-manual banner
  const dr = procs["deploy_real"];
  $("needs-manual-banner").classList.toggle("hidden", !(dr && dr.status === "needs_manual"));

  // safety token countdown from server view
  if (safetyToken && s.safety && !s.safety.armed) { safetyToken = null; updateSafetyUI(false); }
  if (safetyToken && s.safety && s.safety.armed) updateSafetyUI(true, s.safety.ttl);

  // bottom bar pills
  const smEl = $("pill-streammode");
  smEl.textContent = "遥操模式: " + streamModeLabel(t.stream_mode);
  smEl.className = "pill" + (t.stream_mode === "POSE" ? " pose-mode" : (t.stream_mode && t.stream_mode !== "OFF" ? " active-mode" : ""));
  $("pill-locomode").textContent = "步态: " + locoModeLabel(t.loco_mode, t.loco_mode_name);
  const ds = $("pill-deploystate");
  ds.textContent = "部署: " + (t.active_deploy
    ? `${t.active_deploy === "deploy_real" ? "真机" : "仿真"} ${t.deploy_control ? "控制中" : (t.deploy_ready ? "就绪" : "初始化…")}`
    : "–");
  ds.className = "pill" + (t.deploy_control ? " pose-mode" : (t.deploy_ready ? " active-mode" : ""));

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
            const sp = $("rec-status");
            if (sp) { sp.textContent = `rec:${rec} saved:${sv}`; sp.className = "pill" + (_recOn ? " ok" : ""); }
            if ($("rec-ep")) $("rec-ep").textContent = _recOn ? `录制中 ep=${ep} 帧=${fr}` : "未录";
            if (sg && $("rec-stage")) $("rec-stage").textContent = sg;
            const tg = $("rec-toggle");
            if (tg) { tg.textContent = _recOn ? "■ 停止保存（空格）" : "● 开始这条（空格）"; tg.className = "btn " + (_recOn ? "danger-btn" : "primary"); }
          }
        }
      }
    } catch (e) { /* transient */ }
  }
}

/* ── main poll loop ── */
async function poll() {
  try {
    const s = await fetch("/api/state").then((r) => r.json());
    renderState(s);
    $("conn-dot").classList.add("on");
  } catch (e) {
    $("conn-dot").classList.remove("on");
  }
}

$("estop-mastered").addEventListener("change", poll);

showStep(localStorage.getItem("wbc_step") || "1");
poll();
setInterval(poll, 1000);
setInterval(pollLogs, 1000);
