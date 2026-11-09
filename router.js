/* The address of the page follows what is shown, so every view has a link that can be copied, bookmarked or opened
   in a new tab, and Back / Forward work:
     #xp=<XP>&b.<block id>=<option>&loop=<option>&tab=launcher|results|mlflow&rsel=<selection>&view=table|plot|runs|v:<view>&pv=script
      &mlflow=<page of the MLflow UI>&gpus=<server>
   Edited values of variables are not in it: they are saved with the XP (xps[].values).
   Depends on app.js (cfg, st, syncSelection, draw, setPvTab), results.js (XPResults) and gpus.js (XPGpus). */
(() => {
  let applying = false, started = false;

  /* what is shown now, as URL parameters */
  function current() {
    const p = new URLSearchParams();
    if (!cfg || !st.xp) return p;
    p.set('xp', st.xp);
    for (const [bid, opt] of Object.entries(st.blocks)) p.set('b.' + bid, Array.isArray(opt) ? JSON.stringify(opt) : opt);
    if (st.loop) p.set('loop', st.loop);
    const tab = window.XPResults?.tab || 'launcher';
    if (tab !== 'launcher') p.set('tab', tab);
    if (tab === 'results') {
      if (XPResults.selection) p.set('rsel', XPResults.selection);
      p.set('view', XPResults.view);
    }
    if (tab === 'launcher' && $('#tab-script')?.getAttribute('aria-pressed') === 'true') p.set('pv', 'script');
    if (tab === 'mlflow') { const f = XPResults.frameHash(); if (f && f !== '/') p.set('mlflow', f); }
    if (window.XPGpus?.server) p.set('gpus', XPGpus.server);
    return p;
  }

  const text = (p) => '#' + p.toString().replace(/%2F/g, '/');

  /* the link of a view: the current one with some parameters changed */
  function href(changes = {}) {
    const p = current();
    if ('tab' in changes) for (const k of ['view', 'pv', 'mlflow']) p.delete(k);     // they belong to the old tab
    for (const [k, v] of Object.entries(changes)) { if (v == null) p.delete(k); else p.set(k, v); }
    return text(p);
  }

  /* put what is shown in the address bar: a new history entry, except for moves inside the MLflow UI
     (the frame already adds its own entries) */
  function sync() {
    if (applying || !started) return;
    const now = current(), want = text(now);
    if (want === location.hash) return;
    const old = new URLSearchParams(location.hash.slice(1));
    const onlyFrame = old.get('tab') === 'mlflow' && now.get('tab') === 'mlflow'
      && [...new Set([...old.keys(), ...now.keys()])].every((k) => k === 'mlflow' || old.get(k) === now.get(k));
    history[onlyFrame ? 'replaceState' : 'pushState'](null, '', want);
  }
  const later = () => setTimeout(sync, 0);

  /* show what the address says */
  function apply() {
    const p = new URLSearchParams(location.hash.slice(1));
    applying = true;
    try {
      const xp = p.get('xp');
      if (xp && cfg.xps.some((x) => x.name === xp)) {
        if (xp !== st.xp) st.blocks = {};
        st.xp = xp;
      }
      for (const [k, v] of p) {
        if (!k.startsWith('b.')) continue;
        let val = v;
        if (v.startsWith('[')) { try { val = JSON.parse(v); } catch (e) { /* a plain option name */ } }
        st.blocks[k.slice(2)] = val;
      }
      if (p.has('loop')) st.loop = p.get('loop');
      syncSelection();                         // unknown XPs, blocks or options fall back to the defaults
      draw();
      setPvTab(p.get('pv') === 'script' ? 'script' : 'yaml');
      const tab = ['results', 'mlflow', 'servers', 'databases', 'sync'].includes(p.get('tab')) ? p.get('tab') : 'launcher';
      if (p.get('view')) XPResults.setView(p.get('view'));
      if (tab === 'results' && p.get('rsel') !== XPResults.selection) XPResults.setSelection(p.get('rsel'));
      if (tab === 'mlflow') {
        const page = p.get('mlflow');
        XPResults.openMlflow(page && page !== XPResults.frameHash() ? '#' + page : null);
      } else XPResults.switchTab(tab);
      if (p.get('gpus')) XPGpus.show(p.get('gpus')); else XPGpus.hide();
    } finally {
      applying = false;
    }
    later();                                    // the address, cleaned of what did not apply
  }

  window.addEventListener('xp-loaded', () => {
    if (started) { later(); return; }
    started = true;
    if (location.hash.length > 1) apply();
    else history.replaceState(null, '', text(current()));
  });
  window.addEventListener('popstate', () => { if (started) apply(); });
  window.addEventListener('xp-state', later);
  // any click or change may show something else; dialogs closed with Esc too
  for (const ev of ['click', 'change', 'close']) document.addEventListener(ev, later, true);
  window.addEventListener('keyup', (ev) => { if (ev.key === 'Enter' || ev.key === ' ' || ev.key.startsWith('Arrow')) later(); }, true);

  window.XPRouter = { href, sync };
})();
