# GUI i18n (Chinese default / English toggle)

The console UI is bilingual. Chinese is the in-DOM default; `static/i18n.js`
translates to English at runtime (top-right 中 / EN toggle, persisted in
localStorage `wbc_lang`). No server restart needed — it's all client-side.

How it works:
- `static/i18n.js` holds an EXACT map (whole source strings) + a length-sorted
  FRAG list (substrings for interpolated text). A MutationObserver re-translates
  dynamically-rendered content (pills, toasts, tables). Logs / code / command
  blocks are skipped so they're never mangled. The safety phrase: English users
  type `CONFIRM` (already accepted by the backend alongside 「确认姿态」).

To edit a translation / regenerate:
1. Edit the source dictionaries here: `i18n_index.json` (index.html),
   `i18n_app.json` (app.js, {exact,frag}), `i18n_server.json` (server.py),
   `i18n_checks.json` (checks.py). Keys = normalized Chinese source string.
2. Run: `python3 tools/i18n/build_i18n.py`  → rewrites `static/i18n.js`.
3. Hard-refresh the browser.
