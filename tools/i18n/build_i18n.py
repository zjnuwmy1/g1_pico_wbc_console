import json

idx = json.load(open('tools/i18n/i18n_index.json'))
app = json.load(open('tools/i18n/i18n_app.json'))
srv = json.load(open('tools/i18n/i18n_server.json'))
chk = json.load(open('tools/i18n/i18n_checks.json'))

# ---- EXACT map: app/server/checks first, index overrides collisions (capitalized wins) ----
EXACT = {}
EXACT.update(app['exact'])
EXACT.update(srv['exact'])
EXACT.update(chk['exact'])
EXACT.update(idx)            # index wins the benign collisions

# a few belt-and-suspenders additions (whole strings set via direct .textContent)
EXACT.setdefault('取消', 'Cancel')
EXACT.setdefault('确认', 'Confirm')
EXACT.setdefault('✓ PC Service 已安装', '✓ PC Service installed')
EXACT.setdefault('✗ 尚未安装', '✗ Not installed yet')

# ---- FRAG list: app + server frags, pruned, length-sorted (longest first) ----
DROP = {'前', ' 移', ' 转'}          # context-dependent drive-readout single chars
frag_pairs = []
seen = set()
for z, e in app['frag'] + srv.get('frag', []) + chk.get('frag', []):
    if z in DROP:
        continue
    if z == ' 段' and z in seen:      # the duplicate ' 段' -> keep only the first (episodes)
        continue
    if z in seen:
        continue
    seen.add(z)
    frag_pairs.append([z, e])
# Enum / status NAMES appear embedded inside interpolated pills (e.g.
# "遥操模式: 全身跟随 (POSE)", "运行中 ✓ (pid 4567)", "部署: 真机 控制中"),
# so the whole node is an EXACT-miss — add the names as fragments too, taking
# the wording straight from EXACT so it stays consistent. Length-sort below
# guarantees "行走规划"/"受伤行走" win over the bare "行走".
NAME_KEYS = [
    # stream modes
    '关闭', '全身跟随', '行走规划', '上身冻结+行走', '暂停', '三点跟随(头+双手)', '三点跟随',
    # loco gaits
    '待机', '慢走', '行走', '奔跑', '下蹲', '双腿跪', '单腿跪', '俯卧', '爬行',
    '拳击待机', '拳击行走', '左直拳', '右直拳', '左勾拳', '右勾拳', '组合拳A', '组合拳B',
    '前跳', '潜行', '受伤行走',
    # process-pill statuses (get a " (pid N)" suffix -> exact-miss)
    '运行中 ✓', '停止中…', '已退出', '异常退出', '需人工处理!', '启动中…', '未运行', '已停止',
]
# deploy sub-states embedded in "部署: …" — not standalone EXACT keys, give explicit en
NAME_FALLBACK = {'真机': 'real', '仿真': 'sim', '控制中': 'in control'}
for k in NAME_KEYS:
    if k in EXACT and k not in seen:
        seen.add(k)
        frag_pairs.append([k, EXACT[k]])
for k, v in NAME_FALLBACK.items():
    if k not in seen:
        seen.add(k)
        frag_pairs.append([k, v])

# longest key first so specific fragments win over short ones
frag_pairs.sort(key=lambda p: len(p[0]), reverse=True)

print("EXACT entries:", len(EXACT))
print("FRAG entries :", len(frag_pairs))

EXACT_JSON = json.dumps(EXACT, ensure_ascii=False, indent=0).replace('\n', '')
# compact but readable-ish; one entry per line is huge, keep compact
EXACT_JSON = json.dumps(EXACT, ensure_ascii=False, separators=(',', ':'))
FRAG_JSON = json.dumps(frag_pairs, ensure_ascii=False, separators=(',', ':'))

ENGINE = r'''/* i18n.js — runtime bilingual (zh default / en) engine for the G1 PICO console.
   Chinese is the in-DOM default; this swaps to English by source-string lookup.
   No build step, no server change: translate happens entirely in the browser,
   and a MutationObserver catches dynamically-rendered text (pills, toasts,
   tables) so app.js needs almost no changes. Toggle persists in localStorage. */
"use strict";
(function () {
  var EXACT = __EXACT__;
  var FRAG = __FRAG__;                       // [zh, en], pre-sorted longest-first
  var GOAL_IDS = ["stack-goal", "rec-goal"];
  var CJK = /[一-鿿]/;

  function norm(s) { return s.replace(/\s+/g, " ").trim(); }

  // Translate one source string. Returns it unchanged unless LANG === 'en'.
  function translate(s) {
    if (s == null || LANG !== "en") return s;
    var lead = (s.match(/^\s*/) || [""])[0];
    var tail = s.length > lead.length ? (s.match(/\s*$/) || [""])[0] : "";
    var core = s.slice(lead.length, s.length - tail.length);
    var k = norm(core);
    if (Object.prototype.hasOwnProperty.call(EXACT, k)) return lead + EXACT[k] + tail;
    if (!CJK.test(s)) return s;              // already english / nothing to do
    var out = s, hit = false;
    for (var i = 0; i < FRAG.length; i++) {
      if (out.indexOf(FRAG[i][0]) >= 0) { out = out.split(FRAG[i][0]).join(FRAG[i][1]); hit = true; }
    }
    return hit ? out : s;
  }
  window.__t = translate;                    // app.js uiToast/uiConfirm use this

  var LANG = localStorage.getItem("wbc_lang") === "en" ? "en" : "zh";
  var origText = new WeakMap();              // textnode -> last Chinese source (for restore)
  var origAttr = new WeakMap();              // element -> { attr: zh }
  var valState = new WeakMap();              // input  -> { zh, en } (goal-input default)
  var SELF = new WeakSet();                  // nodes we just wrote (skip our own mutation)

  var SKIP_TAG = { SCRIPT: 1, STYLE: 1, PRE: 1, CODE: 1, KBD: 1, TEXTAREA: 1, OPTION: 0 };
  var SKIP_CLASS = /(?:^|\s)(?:logpane|cmdblock|big-ip|langtoggle)(?:\s|$)/;
  function skip(node) {
    var el = node.nodeType === 3 ? node.parentNode : node;
    while (el && el.nodeType === 1) {
      if (SKIP_TAG[el.tagName]) return true;
      var c = el.getAttribute && el.getAttribute("class");
      if (c && SKIP_CLASS.test(c)) return true;
      el = el.parentNode;
    }
    return false;
  }

  function doText(node) {
    if (skip(node)) return;
    var v = node.nodeValue;
    if (LANG === "en") {
      if (CJK.test(v)) origText.set(node, v);            // app wrote Chinese -> cache it
      else if (!origText.has(node)) return;              // pure english / no source
      var t = translate(origText.get(node));
      if (node.nodeValue !== t) { SELF.add(node); node.nodeValue = t; }
    } else if (origText.has(node)) {
      var z = origText.get(node);
      if (node.nodeValue !== z) { SELF.add(node); node.nodeValue = z; }
    }
  }

  function doAttr(el, attr) {
    if (!el.getAttribute) return;
    var v = el.getAttribute(attr);
    if (v == null) return;
    if (LANG === "en") {
      if (CJK.test(v)) { var o = origAttr.get(el) || {}; o[attr] = v; origAttr.set(el, o); }
      var src = (origAttr.get(el) || {})[attr];
      if (src == null) return;
      var t = translate(src);
      if (v !== t) el.setAttribute(attr, t);
    } else {
      var oo = origAttr.get(el);
      if (oo && oo[attr] != null && v !== oo[attr]) el.setAttribute(attr, oo[attr]);
    }
  }

  function doVal(el) {                        // only the goal inputs; never fight user typing
    if (!el) return;
    var v = el.value;
    if (LANG === "en") {
      if (CJK.test(v)) {                      // currently shows the Chinese default
        var k = norm(v);
        var en = Object.prototype.hasOwnProperty.call(EXACT, k) ? EXACT[k] : v;
        valState.set(el, { zh: v, en: en });
        if (v !== en) el.value = en;
      }
      // not CJK -> already english (we set it) or user-typed -> leave alone
    } else {
      var st = valState.get(el);             // restore only the value WE set
      if (st && st.en !== st.zh && el.value === st.en) el.value = st.zh;
    }
  }

  function walk(root) {
    if (root.nodeType === 3) { doText(root); return; }
    if (root.nodeType !== 1) return;
    if (skip(root)) return;
    var tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var n; while ((n = tw.nextNode())) doText(n);
    var withAttr = root.querySelectorAll ? root.querySelectorAll("[placeholder],[title],[alt]") : [];
    for (var i = 0; i < withAttr.length; i++) {
      doAttr(withAttr[i], "placeholder"); doAttr(withAttr[i], "title"); doAttr(withAttr[i], "alt");
    }
    if (root.matches && root.matches("[placeholder],[title],[alt]")) {
      doAttr(root, "placeholder"); doAttr(root, "title"); doAttr(root, "alt");
    }
  }

  function applyLang(lang) {
    LANG = lang === "en" ? "en" : "zh";
    try { localStorage.setItem("wbc_lang", LANG); } catch (e) {}
    document.documentElement.lang = LANG === "en" ? "en" : "zh-CN";
    document.body.classList.toggle("lang-en", LANG === "en");
    document.body.classList.toggle("lang-zh", LANG !== "en");
    var bz = document.getElementById("lang-zh"), be = document.getElementById("lang-en");
    if (bz) bz.classList.toggle("active", LANG !== "en");
    if (be) be.classList.toggle("active", LANG === "en");
    walk(document.body);
    for (var i = 0; i < GOAL_IDS.length; i++) doVal(document.getElementById(GOAL_IDS[i]));
    // <title> lives in <head> (outside the observed body)
    if (!applyLang._title) applyLang._title = document.title;
    document.title = translate(applyLang._title);
  }
  window.setLang = applyLang;

  // ---- observe dynamic content (app.js renders pills / toasts / tables) ----
  var obs = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var m = muts[i];
      if (m.type === "characterData") {
        if (SELF.has(m.target)) { SELF.delete(m.target); continue; }
        doText(m.target);
      } else if (m.type === "attributes") {
        if (m.attributeName) doAttr(m.target, m.attributeName);
      } else {
        for (var j = 0; j < m.addedNodes.length; j++) walk(m.addedNodes[j]);
      }
    }
  });

  function start() {
    applyLang(LANG);
    SELF = new WeakSet();        // drop self-marks from the initial pass (observer wasn't live yet)
    obs.observe(document.body, {
      subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["placeholder", "title", "alt"]
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
'''

out = ENGINE.replace('__EXACT__', EXACT_JSON).replace('__FRAG__', FRAG_JSON)
open('/home/ubuntu/Desktop/g1_pico_wbc_console/static/i18n.js', 'w', encoding='utf-8').write(out)
print("wrote static/i18n.js  (%d bytes)" % len(out))
