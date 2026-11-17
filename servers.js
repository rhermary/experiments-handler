/* Servers and Databases tabs. Servers: the servers of the settings, their paths, the connection to each (opened when
   you click Connect, over ssh with your agent), their state, and the log of every action. Databases: the Postgres of
   each server (and the synchronised one on the central server): their state, and starting / stopping them, or a new
   dated database. Each action button has a ">_" button that shows its commands without running them; after an action
   ran, its commands and their output are shown (and kept in the action log).
   Depends on editors.js ($, h, api, status) and app.js (cfg, commit, clone). */
(() => {
  const S = { data: null, tab: null, busy: {}, log: [], pick: {}, modif: {}, image: {}, other: {}, syncSel: {}, ckptSel: {}, freeSel: {} };
  const LABELS = {
    base: ['Base folder', 'The other paths default to it'], db_pattern: ['Own databases', 'Postgres data folders (<server>_<date>_db)'],
    psql_sock: ['Own Postgres socket', ''], pg_port: ['Own Postgres port', ''], artifacts: ['Artifacts', 'Of the runs made here'],
    checkpoints: ['Checkpoints', 'Of the runs made here'], launcher: ['Launcher folder', 'Repo mirror, launches, queue'],
    scripts: ['Your scripts', 'launch-server.sh, server.sh (not used by the page)'], empty_db: ['Empty database', 'Copied to make a new dated one'],
    synchronised: ['Synchronised data', 'Central server only'], sync_sock: ['Synchronised Postgres socket', 'Central server only'],
    sync_port: ['Synchronised Postgres port', 'Central server only'],
    db_image: ['Database image', 'Default image to start the databases in (else the Docker image variable); can be changed at each start'],
    ui_port: ['Own MLflow UI port', 'On the server'], sync_ui_port: ['Synchronised MLflow UI port', 'On the server'],
    datasets: ['Datasets folder', 'Your datasets: <name>/processed (Datasets tab)'],
    ckpt_cache: ['Checkpoints cache', 'Synchronised checkpoints sent here: {checkpoints,artifacts}/<experiment id>/<run> (Sync tab)'],
  };
  const DERIVED = [['repo', 'Repo mirror'], ['launches', 'Launch folders'], ['queue', 'Queue'], ['sync_db_pattern', 'Synchronised databases'],
    ['sync_artifacts', 'Synchronised artifacts'], ['sync_checkpoints', 'Synchronised checkpoints']];
  const CENTRAL_KEYS = new Set(['synchronised', 'sync_sock', 'sync_port', 'sync_ui_port', 'sync_db_pattern', 'sync_artifacts', 'sync_checkpoints']);

  /* Postgres state: running, stale (socket file left, nothing answers), stopped */
  const STALE = 'Socket file left but nothing answers on it: Postgres was killed (e.g. its container stopped without a clean shutdown). Starting removes it.';
  const pgPill = (state) => state === 'running' ? pill('ok', 'Running') : state === 'stale' ? pill('warn', 'Stopped (stale socket)', STALE) : pill('bad', 'Stopped');
  const pill = (cls, text, title) => h('span', { class: 'sv-pill ' + cls, title: title || null }, text);
  /* the MLflow UI of a server's database (own | sync): served by its container on the port of Servers > paths, when
     the container runs; else '' */
  const uiLink = (sv, which, label) => {
    const db = sv.db?.[which];
    if (!db?.container) return '';
    const port = which === 'own' ? sv.paths_all.ui_port : sv.paths_all.sync_ui_port;
    const url = `http://${sv.host}:${port}/`;
    const mode = db.container.allow_modif ? 'changes allowed' : 'read-only';
    return h('a', { class: 'db-ui' + (db.container.allow_modif ? ' modif' : ''), href: url, target: '_blank', rel: 'noopener',
      title: `The MLflow UI of the ${which === 'own' ? 'own' : 'synchronised'} database of ${sv.name} (${url}): ${mode}` },
      label ? `${label} (${mode})` : `${sv.host}:${port}`, ' ↗');
  };
  const base = (p) => String(p || '').split('/').pop();
  const listed = () => S.data?.servers || [];

  async function load() {
    try { S.data = await api('/api/servers'); } catch (e) { S.data = { servers: [], error: e.message }; }
    try { S.log = (await api('/api/actions')).actions; } catch (e) { S.log = []; }
    paint();
  }

  /* ---------- actions: their commands first (nothing runs), or run them and show what ran ---------- */

  async function showPlan(server, action, args) {
    try {
      const r = await api('/api/servers/run', { server, action, args, dry: true });
      commandsDialog(r.title + ' (not run)', r.plan.map((s) => ({ ...s, planned: true })));
    } catch (e) { status(e.message, true); }
  }

  async function run(server, action, args, after, quiet) {
    S.busy[server + action] = true; paint();
    try {
      const r = await api('/api/servers/run', { server, action, args });
      const i = listed().findIndex((x) => x.name === server);
      if (i >= 0) S.data.servers[i] = r.server;
      if ('ckcatalog' in r) S.data.ckcatalog = r.ckcatalog;
      if (quiet) { delete S.busy[server + action]; return r.result.ok; }
      status(`${r.result.title} ${r.result.ok ? 'done' : 'failed'}${after && r.result.ok ? `: ${after}` : ''}`, !r.result.ok,
        { label: 'Commands and output', fn: () => commandsDialog(r.result.title, r.steps, r.result.id) });
      if (!r.result.ok || ['db_scripts', 'db_start', 'db_new', 'sync_run', 'sync_ckpt', 'sync_free', 'ds_send', 'ck_send', 'ck_remove', 'ck_link'].includes(action)) commandsDialog(r.result.title, r.steps, r.result.id);
      if (action === 'ds_send') await run(server, 'ds_status', {}, null, true);          // what it has now
      if (action === 'sync_free') S.freeSel[server] = new Set();
      if (['ck_send', 'ck_remove', 'ck_link'].includes(action)) {
        if (action === 'ck_send') S.ck.sel[server] = new Set();
        await run(server, 'ck_status', {}, null, true);
      }
      if (['db_start', 'db_stop', 'db_new', 'db_pull'].includes(action)) {      // the databases (and images) as they are now
        if (action === 'db_start' && r.result.ok) await new Promise((ok) => setTimeout(ok, 3000));   // Postgres needs a moment to answer
        await run(server, 'db_status', {}, null, true);
      }
    } catch (e) {
      if (quiet) { delete S.busy[server + action]; return false; }
      status(e.message, true);
    }
    delete S.busy[server + action];
    try { S.log = (await api('/api/actions')).actions; } catch (e) { /* keep the old log */ }
    paint();
  }

  /* the same action on every server it applies to (in parallel, each logged as its own action) */
  const ALL = {
    connect: ['Connect all', (s) => !s.disabled && !s.connected],
    disconnect: ['Disconnect all', (s) => s.connected],
    status: ['Read all states', (s) => !s.disabled && s.connected],
    db_status: ['Read all databases', (s) => !s.disabled && s.connected],
    ds_status: ['Read all datasets', (s) => !s.disabled && s.connected],
    ck_status: ['Read all caches', (s) => !s.disabled && s.connected && !s.central],
  };

  function allButton(action, opts = {}) {
    const [label, applies] = ALL[action];
    const targets = listed().filter(applies);
    const busy = S.busy['*' + action];
    return h('button', { type: 'button', class: 'sv-btn' + (opts.danger ? ' danger' : ''), disabled: busy || !targets.length ? '' : null,
      title: targets.length ? `${targets.map((t) => t.name).join(', ')} (each logged as its own action)` : 'No server to do it on',
      onclick: async (ev) => {
        const b = ev.currentTarget;
        if (opts.confirm && !b.dataset.armed) {
          b.dataset.armed = '1'; b.textContent = `Click again: ${targets.length} server${targets.length > 1 ? 's' : ''}`; b.classList.add('armed');
          setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = label; b.classList.remove('armed'); } }, 4000);
          return;
        }
        S.busy['*' + action] = true;
        const ok = await Promise.all(targets.map((t) => run(t.name, action, {}, null, true)));
        delete S.busy['*' + action];
        const failed = targets.filter((_, i) => !ok[i]).map((t) => t.name);
        status(`${label}: ${targets.length - failed.length} of ${targets.length} done` + (failed.length ? `; failed: ${failed.join(', ')} (see the action log)` : ''), !!failed.length);
        await load();
      } }, busy ? `${label}...` : `${label}${targets.length ? ` (${targets.length})` : ''}`);
  }

  /* Refresh: ask ssh which connections are open, then read the state and the databases of every connected server */
  function refreshButton() {
    const targets = listed().filter((s) => !s.disabled && s.connected);
    return h('button', { type: 'button', class: 'ghost', disabled: S.busy['*refresh'] ? '' : null,
      title: `Check the connections, then read the state and the databases of the connected servers${targets.length ? ` (${targets.map((t) => t.name).join(', ')})` : ''}; each read is logged`,
      onclick: async () => {
        S.busy['*refresh'] = true; paint();
        await load();
        const now = listed().filter((x) => !x.disabled && x.connected);
        const jobs = now.flatMap((t) => ['status', 'db_status'].map((a) => [t.name, a]));
        const ok = await Promise.all(jobs.map(([n, a]) => run(n, a, {}, null, true)));
        delete S.busy['*refresh'];
        const failed = [...new Set(jobs.filter((_, i) => !ok[i]).map(([n]) => n))];
        await load();
        status(now.length ? `Refreshed ${now.length} connected server${now.length > 1 ? 's' : ''} (${now.map((t) => t.name).join(', ')})`
          + (failed.length ? `; failed: ${failed.join(', ')} (see the action log)` : '') : 'Refreshed: no server is connected', !!failed.length);
      } }, S.busy['*refresh'] ? 'Refreshing...' : 'Refresh');
  }

  /* an action button and its ">_" (show the commands) */
  function actionButton(sv, action, label, opts = {}) {
    const busy = S.busy[sv.name + action];
    const args = opts.args || {};
    return h('span', { class: 'sv-act' },
      h('button', { type: 'button', class: 'sv-btn' + (opts.primary ? ' primary' : '') + (opts.danger ? ' danger' : ''), disabled: busy || opts.disabled ? '' : null,
        title: opts.title || null,
        onclick: (ev) => {
          const b = ev.currentTarget;
          if (opts.confirm && !b.dataset.armed) {                  // first click arms it, the second runs it
            b.dataset.armed = '1'; b.textContent = opts.confirm; b.classList.add('armed');
            setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = label; b.classList.remove('armed'); } }, 4000);
            return;
          }
          run(sv.name, action, args, opts.after);
        } }, busy ? '...' : label),
      h('button', { type: 'button', class: 'sv-cmd-btn', title: `Show the commands of "${label}" (nothing runs)`, 'aria-label': `Commands of ${label} on ${sv.name}`,
        onclick: () => showPlan(sv.name, action, args) }, '>_'));
  }

  function commandsDialog(title, steps, id) {
    let d = $('#svdlg');
    if (!d) { d = h('dialog', { id: 'svdlg', class: 'wide svdlg', 'aria-labelledby': 'sv-title' }); document.body.append(d); }
    d.replaceChildren(h('div', { class: 'sv-wrap' },
      h('div', { class: 'pk-head' }, h('h3', { id: 'sv-title' }, title), h('span', { class: 'sp' }),
        id ? h('span', { class: 'hint' }, `Logged in actions/${id}/commands.log`) : h('span', { class: 'hint' }, 'Nothing has run'),
        h('button', { type: 'button', class: 'ghost', onclick: () => d.close() }, 'Close')),
      ...steps.map((s, i) => h('div', { class: 'sv-step' + (s.planned ? '' : s.rc === 0 ? ' ok' : ' bad') },
        h('div', { class: 'sv-step-head' }, h('b', {}, `${i + 1}. ${s.label}`), h('span', { class: 'sp' }),
          s.planned ? '' : h('span', { class: 'hint' }, `Exit ${s.rc} · ${s.secs} s`)),
        h('pre', { class: 'sv-cmd' }, s.display),
        !s.planned && (s.out || s.err) ? h('details', { class: 'sv-out', open: '' }, h('summary', {}, 'Output'),
          h('pre', {}, (s.out || '') + (s.err ? `\n[stderr]\n${s.err}` : ''))) : ''))));
    d.showModal();
  }

  async function openLog(id) {
    try {
      const a = await api('/api/actions/' + encodeURIComponent(id));
      let d = $('#svdlg');
      if (!d) { d = h('dialog', { id: 'svdlg', class: 'wide svdlg' }); document.body.append(d); }
      d.replaceChildren(h('div', { class: 'sv-wrap' },
        h('div', { class: 'pk-head' }, h('h3', {}, a.title || id), h('span', { class: 'sp' }), h('span', { class: 'hint' }, a.folder + '/commands.log'),
          h('button', { type: 'button', class: 'ghost', onclick: () => d.close() }, 'Close')),
        h('pre', { class: 'sv-log' }, a.log)));
      d.showModal();
    } catch (e) { status(e.message, true); }
  }

  /* ---------- Servers tab ---------- */

  function gpuChips(st) {
    if (!st?.gpus?.length) return h('span', { class: 'hint' }, st ? 'None seen' : '?');
    return h('span', { class: 'sv-gpus' }, ...st.gpus.map((g) => h('span', { class: 'sv-gpu' + (g.free ? ' free' : ''), title: `GPU ${g.index}: ${Math.round(g.mem_used)} / ${Math.round(g.mem_total)} MiB${g.free ? ', free' : ''}` }, String(g.index))));
  }

  function pgSummary(state, latest, n) {
    if (!state) return h('span', { class: 'hint' }, '?');
    return h('div', { class: 'sv-stack' }, pgPill(state),
      latest ? h('code', { class: 'sv-path' }, base(latest)) : h('span', { class: 'hint' }, 'No folder'), n > 1 ? h('span', { class: 'hint' }, `Newest of ${n}`) : '');
  }

  function connCell(sv) {
    return h('div', { class: 'sv-stack' }, sv.connected ? pill('ok', 'Connected') : pill('off', 'Not connected'),
      sv.connected ? actionButton(sv, 'disconnect', 'Disconnect')
        : actionButton(sv, 'connect', 'Connect', { primary: true, title: 'Open one ssh connection (your agent asks for the key), reused by every command' }));
  }

  function serverRow(sv) {
    const st = sv.status;
    return h('tr', { class: (sv.disabled ? 'sv-disabled' : '') + (sv.central ? ' sv-central' : '') },
      h('td', {}, h('div', { class: 'sv-stack' }, h('b', {}, sv.name), sv.central ? pill('ref', 'Central') : '', sv.disabled ? pill('off', 'Commented out') : '',
        h('small', { class: 'sv-addr' }, `${sv.user ? sv.user + '@' : ''}${sv.host}${sv.port && sv.port !== 22 ? ':' + sv.port : ''}`))),
      h('td', {}, connCell(sv)),
      h('td', {}, pgSummary(st?.pg, st?.db_latest, st?.dbs?.length || 0)),
      h('td', {}, sv.central ? pgSummary(st?.sync_pg, st?.sync_db_latest, st?.sync_dbs?.length || 0) : h('span', { class: 'hint' }, '-')),
      h('td', {}, gpuChips(st)),
      h('td', { class: 'num' }, st?.disk_free_gb != null ? `${st.disk_free_gb} GB` : h('span', { class: 'hint' }, '?')),
      h('td', {}, h('div', { class: 'sv-stack' }, h('span', { class: 'hint' }, st ? st.when : 'Never read'),
        actionButton(sv, 'status', 'Read state', { disabled: !sv.connected, title: sv.connected ? 'Read Postgres, folders, GPUs and disk (read-only)' : 'Connect first' }))));
  }

  function pathsTable(servers) {
    const keys = [...Object.keys(LABELS), ...DERIVED.map(([k]) => k)];
    const label = (k) => LABELS[k] || [DERIVED.find(([x]) => x === k)[1], 'From the launcher / synchronised folder'];
    return h('table', { class: 'sv-paths' },
      h('thead', {}, h('tr', {}, h('th', {}), ...servers.map((s) => h('th', {}, s.name, s.central ? ' ★' : '')))),
      h('tbody', {}, ...keys.map((k) => h('tr', {}, h('th', { scope: 'row' }, label(k)[0], label(k)[1] ? h('small', {}, label(k)[1]) : ''),
        ...servers.map((s) => {
          if (CENTRAL_KEYS.has(k) && !s.central) return h('td', {}, h('span', { class: 'hint' }, '-'));
          const own = (s.paths || {})[k];
          const v = s.paths_all[k];
          return h('td', {}, v ? h('code', { class: 'sv-path' + (own ? ' changed' : ''), title: own ? 'Changed for this server' : 'Default' }, v) : h('span', { class: 'hint' }, k === 'db_image' ? 'Docker image variable' : '-'));
        })))));
  }

  function logTable(kind) {
    const rows = S.log.filter((a) => !kind || a.kind.startsWith(kind + '_') || (kind === 'sync' && a.kind.startsWith('ck_')));
    if (!rows.length) return h('p', { class: 'hint' }, 'No action yet.');
    return h('table', { class: 'sv-log-table' },
      h('thead', {}, h('tr', {}, ...['When', 'Action', 'Server', 'Steps', 'Result', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...rows.slice(0, 40).map((a) => h('tr', {},
        h('td', { class: 'hint' }, a.when), h('td', {}, a.title), h('td', {}, a.server || '-'), h('td', { class: 'num' }, String(a.steps)),
        h('td', {}, a.ok ? pill('ok', 'Done') : pill('bad', 'Failed')),
        h('td', {}, h('button', { type: 'button', class: 'sv-btn', onclick: () => openLog(a.id) }, 'Commands and Output'))))));
  }

  const NOTE = 'Nothing connects, starts or syncs by itself: Connect opens one ssh connection per server (your agent asks for the key), reused by the commands '
    + 'after it for 4 hours or until Disconnect. ">_" shows what an action runs, without running it; every action that runs is logged with its commands and output.';

  function paintServers(box) {
    const servers = listed();
    box.replaceChildren(
      h('div', { class: 'rs-head' }, h('h2', {}, 'Servers'),
        h('span', { class: 'hint' }, S.data.central ? `central server: ${S.data.central} (synchronised data)` : 'no central server yet: choose one in Edit servers'),
        h('span', { class: 'sp' }),
        h('span', { class: 'sv-all' }, allButton('connect'), allButton('status'), allButton('db_status'), allButton('disconnect', { danger: true, confirm: true })),
        h('button', { type: 'button', class: 'ghost', onclick: editServers }, 'Edit Servers'),
        refreshButton()),
      h('p', { class: 'hint sv-note' }, NOTE),
      S.data.error ? h('pre', { class: 'dlg-err' }, S.data.error) : '',
      h('div', { class: 'card sv-card' }, h('div', { class: 'rs-scroll' }, h('table', { class: 'sv-table' },
        h('thead', {}, h('tr', {}, ...['Server', 'Connection', 'Own Postgres', 'Synchronised Postgres', 'GPUs', 'Disk free', 'State'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...servers.map(serverRow))))),
      h('details', { class: 'card sv-card', open: '' }, h('summary', {}, h('b', {}, 'Paths on each server'),
        h('span', { class: 'hint' }, '  defaults from the base folder; a path changed for one server is marked (Edit servers)')),
        h('div', { class: 'rs-scroll' }, pathsTable(servers))),
      h('details', { class: 'card sv-card', open: '' }, h('summary', {}, h('b', {}, 'Action log'), h('span', { class: 'hint' }, '  every command run on a server, with its output')),
        h('div', { class: 'rs-scroll' }, logTable())));
  }

  /* ---------- Databases tab ---------- */

  function dbSection(sv, which) {
    const db = sv.db?.[which];
    const p = sv.paths_all;
    const key = `${sv.name}/${which}`;
    const title = which === 'own' ? 'Own database' : 'Synchronised database';
    const notCentral = which === 'sync' && !sv.central
      ? h('p', { class: 'db-warn' }, `${sv.name} is not the central server: syncs write to ${(listed().find((s) => s.central) || {}).name || 'no server yet'}. `,
        h('button', { type: 'button', class: 'sv-btn', onclick: (ev) => {
          const b = ev.currentTarget;
          if (!b.dataset.armed) { b.dataset.armed = '1'; b.textContent = 'Click again to make it central'; b.classList.add('armed'); setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = 'Make it the central server'; b.classList.remove('armed'); } }, 4000); return; }
          makeCentral(sv);
        } }, 'Make It the Central Server')) : '';
    const where = which === 'own' ? `${p.psql_sock} · port ${p.pg_port}` : `${p.sync_sock} · port ${p.sync_port}`;
    if (!db) return h('div', { class: 'db-sec' }, h('div', { class: 'db-head' }, h('b', {}, title), h('code', { class: 'sv-path' }, where)),
      h('p', { class: 'hint' }, sv.connected ? 'Read the databases to see their state.' : 'Connect, then read the databases.'));
    const c = db.container;
    const inUse = db.data_directory || c?.folder || null;
    const folders = db.folders || [];
    const pick = S.pick[key] && folders.includes(S.pick[key]) ? S.pick[key] : (inUse && folders.includes(inUse) ? inUse : folders[folders.length - 1]);
    S.pick[key] = pick;
    const sizes = sv.db?.sizes || {};
    const rows = folders.slice().reverse().map((f) => {
      const radio = h('input', { type: 'radio', name: `db-${key}`, checked: f === pick ? '' : null, 'aria-label': `Use ${base(f)}` });
      radio.addEventListener('change', () => { S.pick[key] = f; paint(); });
      const date = (base(f).match(/\d{4}\.\d\d\.\d\d/) || [''])[0];
      return h('tr', { class: f === inUse ? 'db-inuse' : '' }, h('td', {}, radio), h('td', {}, h('code', {}, base(f))), h('td', { class: 'hint' }, date),
        h('td', { class: 'num' }, sizes[f] != null ? `${(sizes[f] / 1024).toFixed(1)} GB` : h('span', { class: 'hint' }, '-')),
        h('td', {}, f === inUse ? pill('ok', 'In use') : ''));
    });
    // Allow changes: remembered in this browser per server and database; else the mode of the running container
    const modifKey = `xp-db-modif:${sv.name}:${which}`;
    if (S.modif[key] === undefined) {
      let saved = null;
      try { saved = localStorage.getItem(modifKey); } catch (e) { /* storage unavailable */ }
      S.modif[key] = saved !== null ? saved === '1' : !!c?.allow_modif;
    }
    const modif = h('input', { type: 'checkbox', checked: S.modif[key] ? '' : null });
    modif.addEventListener('change', () => {
      S.modif[key] = modif.checked;
      try { localStorage.setItem(modifKey, modif.checked ? '1' : '0'); } catch (e) { /* storage unavailable */ }
      paint();
    });
    const modePill = c ? (c.allow_modif ? pill('warn', 'Changes allowed', 'Started with launch.sh --allow-modif: the MLflow UI can change this database')
      : pill('ok', 'Read-only UI', 'Started without --allow-modif: the MLflow UI reads only (a read-only Postgres user)')) : '';
    const running = db.state === 'running';
    return h('div', { class: 'db-sec' },
      h('div', { class: 'db-head' }, h('b', {}, title), pgPill(db.state), modePill, h('code', { class: 'sv-path' }, where)),
      notCentral,
      h('dl', { class: 'db-facts' },
        h('dt', {}, 'Folder in use'), h('dd', {}, inUse ? h('code', { class: 'sv-path', title: db.data_directory ? 'Asked to Postgres (show data_directory)' : 'From the container' }, inUse) : h('span', { class: 'hint' }, running ? 'unknown' : 'None (stopped)')),
        h('dt', {}, 'Container'), h('dd', {}, c ? h('span', {}, h('code', {}, c.id), ` ${c.name} · ${c.image} · started ${c.started}`,
          h('span', { class: 'hint' }, ` · by ${c.by}`))
          : h('span', { class: 'hint' }, 'None of yours running')),
        ...(c ? [h('dt', {}, 'MLflow UI'), h('dd', {}, uiLink(sv, which))] : [])),
      folders.length ? h('div', { class: 'rs-scroll' }, h('table', { class: 'db-folders' },
        h('thead', {}, h('tr', {}, ...['', 'Folder', 'Date', 'Size', ''].map((t) => h('th', {}, t)))), h('tbody', {}, ...rows)))
        : h('p', { class: 'hint' }, `No folder matches ${which === 'own' ? p.db_pattern : p.sync_db_pattern}`),
      imagePicker(sv, key),
      h('div', { class: 'db-acts' },
        actionButton(sv, 'db_start', `Start on ${pick ? base(pick) : '...'}`, { primary: true, disabled: !sv.connected || running || !pick || !S.image[key],
          args: { which, folder: pick, allow_modif: !!S.modif[key], image: S.image[key] },
          title: running ? 'Already running: stop it first' : `db/launch.sh in ${S.image[key]}: Postgres, then the MLflow UI on port ${which === 'own' ? p.ui_port : p.sync_ui_port}` }),
        h('label', { class: 'db-opt', title: 'launch.sh --allow-modif: the MLflow UI can change the database (else it reads only). Remembered in this browser; used at the next Start' }, modif, 'Allow changes in the MLflow UI',
          c && !!c.allow_modif !== !!S.modif[key] ? h('span', { class: 'hint' }, ` (now ${c.allow_modif ? 'allowed' : 'read-only'}: applies at the next start)`) : ''),
        actionButton(sv, 'db_stop', 'Stop', { danger: true, confirm: 'Click again to stop', disabled: !sv.connected || !c, args: { which, container: c?.id },
          title: c ? `docker container stop ${c.id}` : 'No container of yours is running' }),
        actionButton(sv, 'db_new', 'New dated database', { confirm: 'Click again to create it', disabled: !sv.connected || !sv.db?.empty_db, args: { which },
          title: !sv.db?.empty_db ? `${p.empty_db} not found` : which === 'own'
            ? `A copy of ${p.empty_db}. The files of the previous database go with it: ${p.artifacts} and ${p.checkpoints} are moved to <name>_<date of the newest dated folder>, and empty ones are made. Stop the database first.`
            : `A copy of ${p.empty_db}` }),
        actionButton(sv, 'db_sizes', 'Measure sizes', { disabled: !sv.connected || !folders.length, args: { which }, title: 'du on the folders (can take a while)' })));
  }

  /* the image a database starts in: a tag of the images on the server (one row per repository), the default one even
     when it is missing (then: Pull), or any other name typed in */
  function imagePicker(sv, key) {
    const imgs = sv.db?.images || [];
    const found = imgs.map((i) => i.image);
    const def = sv.image_default || '';
    const cur = S.image[key] ?? (found.includes(def) || !found.length ? def : found[0]);
    S.image[key] = cur;
    const split = (i) => { const k = i.lastIndexOf(':'); return k > i.lastIndexOf('/') ? [i.slice(0, k), i.slice(k + 1)] : [i, 'latest']; };
    const choose = (i) => { S.image[key] = i; S.other = { ...S.other, [key]: false }; paint(); };
    const repos = new Map();
    for (const i of [...found, ...(def && !found.includes(def) ? [def] : [])]) {
      const [r, t] = split(i);
      if (!repos.has(r)) repos.set(r, []);
      repos.get(r).push({ image: i, tag: t, meta: imgs.find((x) => x.image === i) });
    }
    const other = S.other?.[key] || (cur && !repos.size) || (cur && ![...repos.values()].flat().some((x) => x.image === cur));
    const tagBtn = (x) => h('button', {
      type: 'button', class: 'db-tag' + (x.image === cur && !other ? ' on' : '') + (x.meta ? '' : ' missing'),
      'aria-pressed': x.image === cur && !other ? 'true' : 'false',
      title: x.meta ? `${x.image}\ncreated ${x.meta.created} · ${x.meta.size}` : `${x.image}: not on ${sv.name}`,
      onclick: () => choose(x.image) }, x.tag, x.image === def ? h('small', {}, 'Default') : '');
    const otherBtn = h('button', { type: 'button', class: 'db-tag' + (other ? ' on' : ''), onclick: () => { S.other = { ...S.other, [key]: true }; paint(); $(`.db-image-txt[data-key="${key}"]`)?.focus(); } }, 'Other…');
    const rows = [...repos].map(([r, tags], n, all) => h('div', { class: 'db-tags' }, h('code', { class: 'db-repo' }, r), ...tags.map(tagBtn),
      n === all.length - 1 ? otherBtn : ''));
    const txt = h('input', { type: 'text', value: other ? cur : '', placeholder: 'repository:tag', class: 'db-image-txt', spellcheck: 'false', 'data-key': key, 'aria-label': 'Image name' });
    txt.addEventListener('change', () => { S.image[key] = txt.value.trim(); paint(); });
    const there = found.includes(cur);
    const meta = imgs.find((x) => x.image === cur);
    return h('div', { class: 'db-image' },
      h('span', { class: 'db-image-lbl' }, 'Image'),
      h('div', { class: 'db-image-body' },
        ...(rows.length ? rows : [h('div', { class: 'db-tags' }, h('span', { class: 'hint' }, 'No image found on the server'), otherBtn)]),
        other ? h('div', { class: 'db-tags' }, txt) : '',
        cur ? h('div', { class: 'db-image-state' },
          there ? pill('ok', 'On the server') : pill('bad', 'Not on the server', 'Starting never downloads it: pull it first'),
          h('span', { class: 'hint' }, meta ? `Created ${meta.created} · ${meta.size}` : ''),
          !there ? actionButton(sv, 'db_pull', 'Pull', { disabled: !sv.connected, args: { image: cur }, title: `docker pull ${cur} on ${sv.name} (can take minutes)` }) : '',
          cur !== def && def ? h('button', { type: 'button', class: 'db-reset', onclick: () => choose(def) }, 'Back to the Default') : '') : ''));
  }

  /* a synchronised database: on the central server always, elsewhere when one was found */
  const hasSync = (sv) => sv.central || (sv.db?.sync && (sv.db.sync.folders.length || sv.db.sync.state === 'running'));

  function noSync(sv) {
    const p = sv.paths_all;
    return h('div', { class: 'db-sec db-nosync' },
      h('div', { class: 'db-head' }, h('b', {}, 'Synchronised database'), h('span', { class: 'hint' }, sv.db ? 'None on this server' : '')),
      h('p', { class: 'hint' }, `Creates ${p.synchronised} (artifacts, checkpoints, socket) and copies ${base(p.empty_db)} to ${base(p.synchronised)}/db_<today>. `
        + 'Make this server the central one afterwards to sync into it.'),
      h('div', { class: 'db-acts' }, actionButton(sv, 'db_new', 'Create a synchronised database here', {
        confirm: 'Click again to create it', args: { which: 'sync' },
        disabled: !sv.connected || !sv.db || !sv.db.empty_db,
        title: !sv.db ? 'Read the databases first' : sv.db.empty_db ? `A copy of ${p.empty_db}` : `${p.empty_db} not found on ${sv.name}` })));
  }

  async function makeCentral(sv) {
    const before = (cfg.servers.find((s) => s.central) || {}).name;
    try {
      await commit((c) => { for (const s of c.servers) { if (s.name === sv.name) s.central = true; else delete s.central; } });
      status(`${sv.name} is now the central server` + (before ? ` (was ${before}: its synchronised data stays there)` : ''));
      load();
    } catch (e) { status(e.message, true); }
  }

  function dbCard(sv) {
    return h('div', { class: 'card sv-card db-card' + (sv.central ? ' central' : '') },
      h('div', { class: 'db-top' }, h('b', { class: 'db-name' }, sv.name), sv.central ? pill('ref', 'Central') : '',
        sv.connected ? pill('ok', 'Connected') : pill('off', 'Not connected'),
        h('span', { class: 'sp' }), sv.db ? h('span', { class: 'hint' }, `Read ${sv.db.when}`) : '',
        sv.connected ? '' : actionButton(sv, 'connect', 'Connect', { primary: true }),
        actionButton(sv, 'db_status', 'Read the databases', { disabled: !sv.connected, title: 'Read-only: sockets, containers, folders, the folder in use' }),
        actionButton(sv, 'db_scripts', 'Show your scripts', { disabled: !sv.connected, title: `cat ${sv.paths_all.scripts}/launch-server.sh and server.sh` })),
      h('div', { class: 'db-secs' }, dbSection(sv, 'own'), hasSync(sv) ? dbSection(sv, 'sync') : noSync(sv)));
  }

  function paintDatabases(box) {
    const servers = listed().filter((s) => !s.disabled);
    box.replaceChildren(
      h('div', { class: 'rs-head' }, h('h2', {}, 'Databases'), h('span', { class: 'hint' }, 'The Postgres of each server, and the synchronised one on the central server'),
        h('span', { class: 'sp' }),
        h('span', { class: 'sv-all' }, allButton('connect'), allButton('db_status'), allButton('disconnect', { danger: true, confirm: true })),
        refreshButton()),
      h('p', { class: 'hint sv-note' }, 'Start copies the launcher\'s db/launch.sh and db/serve.sh to the server and runs them with the folder and image you pick: '
        + 'Postgres, then its MLflow UI (ports in the server paths). Stop is docker container stop. New dated database copies _empty_db. '
        + 'Nothing is read or started until you click; ">_" shows the commands first.'),
      ...servers.map(dbCard),
      h('details', { class: 'card sv-card' }, h('summary', {}, h('b', {}, 'Database actions'), h('span', { class: 'hint' }, '  with their commands and output')),
        h('div', { class: 'rs-scroll' }, logTable('db'))));
  }

  function paint() {
    const box = S.tab && $(`#${S.tab}tab`);
    if (!box) return;
    if (!S.data) { box.replaceChildren(h('p', { class: 'empty' }, 'Reading the servers...')); return; }
    if (S.tab === 'servers') paintServers(box); else if (S.tab === 'sync') paintSync(box); else if (S.tab === 'datasets') paintDatasets(box); else paintDatabases(box);
  }

  /* ---------- Sync tab ---------- */

  const gb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : b >= 1e3 ? `${Math.round(b / 1e3)} kB` : `${b} B`);

  /* why a server cannot be previewed / synced now ('' when it can); strict: its databases must be known to run */
  function syncReady(sv, central, strict = false) {
    const own = sv.db?.own, cdb = central?.db?.sync;
    return !sv.connected ? 'Connect it first' : !central?.connected ? `Connect ${central?.name} first`
      : (own ? own.state !== 'running' : strict) ? 'Its database is not running (Databases)'
      : (cdb ? cdb.state !== 'running' : strict) ? `The synchronised database on ${central.name} is not running (Databases)` : '';
  }

  /* Sync tab: read every database again, then preview again every server that is ready (one after the other: each
     preview opens the tunnel to the synchronised database through the same local socket) */
  function readAllSyncButton(central) {
    const busy = S.busy['*syncread'];
    return h('button', { type: 'button', class: 'sv-btn', disabled: busy ? '' : null,
      title: 'Read the databases of the connected servers, then preview again (read-only) the servers whose database and the synchronised one run',
      onclick: async () => {
        S.busy['*syncread'] = true; paint();
        const connected = listed().filter((s) => !s.disabled && s.connected);
        await Promise.all(connected.map((t) => run(t.name, 'db_status', {}, null, true)));
        await load();
        const c = listed().find((s) => s.central);
        const ready = listed().filter((s) => !s.disabled && !syncReady(s, c, true));
        const skipped = listed().filter((s) => !s.disabled && s.connected && syncReady(s, c, true)).map((s) => `${s.name}: ${syncReady(s, c, true)}`);
        let failed = [];
        for (const t of ready) {
          S.busy[t.name + 'sync_preview'] = true; paint();
          if (!(await run(t.name, 'sync_preview', {}, null, true))) failed.push(t.name);
        }
        delete S.busy['*syncread'];
        await load();
        status(`Read ${connected.length} server${connected.length === 1 ? '' : 's'}; previewed ${ready.length - failed.length} of ${ready.length}`
          + (failed.length ? `; failed: ${failed.join(', ')} (see the action log)` : '') + (skipped.length ? `; not previewed: ${skipped.join('; ')}` : ''), !!failed.length);
      } }, busy ? 'Reading and previewing...' : 'Read All Databases and Preview');
  }

  function syncCard(sv, central) {
    const pv = sv.sync;
    const own = sv.db?.own;
    const cdb = central?.db?.sync;
    const notReady = syncReady(sv, central);
    const head = h('div', { class: 'db-top' }, h('b', { class: 'db-name' }, sv.name), sv.connected ? pill('ok', 'Connected') : pill('off', 'Not connected'),
      h('span', { class: 'hint' }, 'Own database'), own ? pgPill(own.state) : h('span', { class: 'hint' }, '? (read the databases)'), uiLink(sv, 'own', 'MLflow UI'),
      h('span', { class: 'sp' }),
      actionButton(sv, 'sync_preview', pv ? 'Preview again' : 'Preview', { disabled: !!notReady, title: notReady || 'Read-only: what a sync would copy (runs, files, sizes)' }));
    const parts = [head];
    for (const [k, label] of [['results', 'results sync'], ['checkpoints', 'checkpoints sync'], ['freed', 'deletion of synced checkpoints']]) {
      const done = sv.synced?.[k];
      if (!done) continue;
      parts.push(h('div', { class: done.ok ? 'sync-last' : 'db-warn' }, h('span', {}, `Last ${label} ${done.when}: ${done.ok ? 'done and checked' : k === 'freed' ? 'failed (only folders identical on the central server can have been deleted: see the output)' : 'failed (run it again: nothing of a failed row copy is kept)'}. `),
        h('button', { type: 'button', class: 'db-reset', onclick: () => openLog(done.id) }, 'Commands and Output'),
        done.out ? h('pre', { class: 'sync-out' }, done.out.trim()) : ''));
    }
    if (!pv) {
      parts.push(h('p', { class: 'hint' }, notReady ? `To preview: ${notReady}.` : 'Preview to see what a sync would copy. Nothing is copied before you click Sync.'));
      return h('div', { class: 'card sv-card db-card' }, ...parts);
    }
    const ex = pv.experiments;
    const sel = S.syncSel[sv.name] ??= new Set(ex.filter((x) => x.new + x.changed).map((x) => x.name));
    const csel = S.ckptSel[sv.name] ??= new Set(ex.filter((x) => x.ckpt).map((x) => x.name));
    const todo = ex.filter((x) => sel.has(x.name) && x.new + x.changed);
    const ctodo = ex.filter((x) => csel.has(x.name) && x.ckpt);
    const fsel = S.freeSel[sv.name] ??= new Set();          // checkpoints to delete here (identical there): none by default
    const ftodo = ex.filter((x) => fsel.has(x.name) && x.ckpt_done > 0);
    const tot = (k, list = ex) => list.reduce((a, x) => a + x[k], 0);
    const box = (set, x, can, what) => {
      const cb = h('input', { type: 'checkbox', checked: can && set.has(x.name) ? '' : null, disabled: can ? null : '', 'aria-label': `Sync the ${what} of ${x.name}` });
      cb.addEventListener('change', () => { if (cb.checked) set.add(x.name); else set.delete(x.name); paint(); });
      return h('td', { class: 'sync-cb' }, cb);
    };
    const n = (v, cls) => h('td', { class: 'num' + (v ? (cls ? ' ' + cls : '') : ' hint') }, String(v || 0));
    const row = (x) => {
      const can = x.new + x.changed > 0;
      return h('tr', {},
        h('td', {}, x.name, x.deleted ? h('span', { class: 'hint' }, ' (deleted)') : '', h('div', { class: 'hint', title: x.older ? `Checkpoint folders in ${x.src_folder || pv.src_checkpoints + '/' + x.src_id} whose runs are not in this server's database any more (an earlier one) but are in the synchronised database: their checkpoints can still be synced` : null },
          x.older ? `checkpoints only: runs of an older database · ${x.src_folder || x.src_id} → ${x.dst_id}` : x.dst_id ? `id ${x.src_id} → ${x.dst_id}` : `id ${x.src_id} → created`)),
        box(sel, x, can, 'results'), n(x.new, 'sync-new'), n(x.changed, 'sync-chg'), n(x.synced),
        h('td', { class: 'num' + (x.old_path ? '' : ' hint'), title: x.old_example ? `Left alone, e.g. ${x.old_example}` : null }, String(x.old_path || 0)),
        h('td', { class: 'num' + (can ? '' : ' hint') }, can ? `${x.files} · ${gb(x.bytes)}` : '-'),
        box(csel, x, x.ckpt > 0, 'checkpoints'), h('td', { class: 'num' + (x.ckpt ? ' sync-new' : ' hint') }, x.ckpt ? `${x.ckpt} · ${gb(x.ckpt_bytes)}` : '0'),
        n(x.ckpt_wait),
        x.ckpt_done > 0 ? h('td', { class: 'num sync-there' }, (() => {
          const cb = h('input', { type: 'checkbox', checked: fsel.has(x.name) ? '' : null, 'aria-label': `Delete on ${sv.name} the synchronised checkpoints of ${x.name}`,
            title: 'Select to delete these checkpoint folders here (they are identical on the central server)' });
          cb.addEventListener('change', () => { if (cb.checked) fsel.add(x.name); else fsel.delete(x.name); paint(); });
          return h('label', {}, cb, ` ${x.ckpt_done} · ${gb(x.ckpt_there_bytes || 0)}`);
        })(), x.ckpt_freed ? h('div', { class: 'hint' }, `${x.ckpt_freed} deleted here`) : '')
          : h('td', { class: 'num hint' }, x.ckpt_freed ? `0 (${x.ckpt_freed} deleted here)` : '0'));
    };
    const nRes = tot('new', todo) + tot('changed', todo);
    parts.push(
      h('p', { class: 'hint' }, `Preview of ${pv.when}: into ${pv.dst_folder} on ${pv.central} (files under ${pv.dst_artifacts.replace(/\/artifacts$/, '')}/{artifacts,checkpoints}). MLflow schema ${pv.schema}.`),
      ...(pv.blocking || []).map((b) => h('p', { class: 'db-warn' }, b)),
      h('div', { class: 'rs-scroll' }, h('table', { class: 'db-folders sync-table' },
        h('thead', {},
          h('tr', {}, h('th', {}), h('th', { colspan: 6, class: 'sync-group' }, 'Results: runs and artifacts'), h('th', { colspan: 4, class: 'sync-group' }, 'Checkpoints')),
          h('tr', {}, ...['Experiment', '', 'New', 'Changed', 'Synced', 'Older paths', 'Artifacts', '', 'To copy', 'Waiting', 'There'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...ex.map(row)),
        h('tfoot', {}, h('tr', {}, h('td', {}, h('b', {}, 'Selected')), h('td', {}), h('td', { class: 'num' }, String(tot('new', todo))), h('td', { class: 'num' }, String(tot('changed', todo))),
          h('td', {}), h('td', {}), h('td', { class: 'num' }, `${tot('files', todo)} · ${gb(tot('bytes', todo))}`),
          h('td', {}), h('td', { class: 'num' }, `${tot('ckpt', ctodo)} · ${gb(tot('ckpt_bytes', ctodo))}`), h('td', {}),
          h('td', { class: 'num' }, ftodo.length ? `${tot('ckpt_done', ftodo)} · ${gb(tot('ckpt_there_bytes', ftodo))}` : ''))))),
      pv.unknown_checkpoints ? h('p', { class: 'hint' }, `${pv.unknown_checkpoints} checkpoint folder(s) of ${pv.src_checkpoints} belong to runs in neither database: left alone.`) : '',
      Object.keys(pv.not_copied || {}).length ? h('p', { class: 'hint' }, 'Not copied (not used by this project): ' + Object.entries(pv.not_copied).map(([k, v]) => `${v} ${k}`).join(', ')) : '',
      h('p', { class: 'hint' }, '"Changed": already synced, but its status, end time, params, tags, last metrics or datasets changed since: copied again. '
        + `"Older paths": artifacts not under ${pv.src_artifacts}: left alone. Checkpoints "Waiting": their run is not synchronised yet (sync its results first); `
        + '"There": already on the central server, identical; tick it to delete those folders here (measured again first: only the identical ones go).'),
      h('div', { class: 'db-acts' },
        actionButton(sv, 'sync_run', todo.length ? `Sync results: ${nRes} run${nRes > 1 ? 's' : ''} (${gb(tot('bytes', todo))})` : 'Sync results', {
          primary: true, confirm: 'Click again to sync the results', disabled: !!notReady || !todo.length || (pv.blocking || []).length > 0,
          args: { experiments: todo.map((x) => x.name) }, title: notReady || 'The artifacts, then the rows (one transaction), then checks both' }),
        actionButton(sv, 'sync_ckpt', ctodo.length ? `Sync checkpoints: ${tot('ckpt', ctodo)} run${tot('ckpt', ctodo) > 1 ? 's' : ''} (${gb(tot('ckpt_bytes', ctodo))})` : 'Sync checkpoints', {
          primary: true, confirm: 'Click again to sync the checkpoints', disabled: !sv.connected || !ctodo.length,
          args: { experiments: ctodo.map((x) => x.name) }, title: 'The checkpoint folders (tar | tar), then checks them; no database is touched' }),
        actionButton(sv, 'sync_free', ftodo.length ? `Delete here: ${tot('ckpt_done', ftodo)} synced checkpoint${tot('ckpt_done', ftodo) > 1 ? 's' : ''} (${gb(tot('ckpt_there_bytes', ftodo))})` : 'Delete synced checkpoints here', {
          danger: true, confirm: `Click again: delete on ${sv.name}`, disabled: !sv.connected || !central?.connected || !ftodo.length,
          args: { experiments: ftodo.map((x) => x.name) },
          title: ftodo.length ? `On ${sv.name} only: each folder is measured again on both servers and deleted only if its copy on ${central?.name} has the same files and bytes. Artifacts and databases are not touched.`
            : 'Tick experiments in the "There" column first' })));
    return h('div', { class: 'card sv-card db-card' }, ...parts);
  }

  function paintSync(box) {
    const servers = listed().filter((s) => !s.disabled);
    const central = servers.find((s) => s.central);
    const cdb = central?.db?.sync;
    box.replaceChildren(
      h('div', { class: 'rs-head' }, h('h2', {}, 'Sync'),
        h('span', { class: 'hint' }, central ? `runs, artifacts and checkpoints of each server → the synchronised database on ${central.name}` : 'No central server: choose one in Servers > Edit servers'),
        h('span', { class: 'sp' }),
        h('span', { class: 'sv-all' }, allButton('connect'), readAllSyncButton(central)), refreshButton()),
      central ? h('div', { class: 'card sv-card sync-central' },
        h('b', {}, `Into ${central.name}`), cdb ? pgPill(cdb.state) : h('span', { class: 'hint' }, '? (read the databases)'), uiLink(central, 'sync', 'MLflow UI'),
        h('code', { class: 'sv-path' }, cdb?.data_directory || cdb?.container?.folder || central.paths_all.sync_db_pattern),
        h('span', { class: 'hint' }, `files: ${central.paths_all.sync_artifacts}, ${central.paths_all.sync_checkpoints}`)) : '',
      h('p', { class: 'hint sv-note' }, 'Two syncs, each when you click. Results: the runs (rows, through the two Postgres sockets tunnelled here with ssh -O forward) '
        + 'and their artifacts; the artifacts first, then the rows in one transaction, then both are checked: a failed sync keeps no rows. '
        + 'Checkpoints: the checkpoint folders of the runs already synchronised (tar | tar through this machine), then checked. Nothing is deleted anywhere. ">_" shows every command.'),
      ...servers.map((sv) => syncCard(sv, central)),
      ckCacheCard(servers, central),
      h('details', { class: 'card sv-card' }, h('summary', {}, h('b', {}, 'Sync actions'), h('span', { class: 'hint' }, '  with their commands and output')),
        h('div', { class: 'rs-scroll' }, logTable('sync'))));
  }

  /* ---------- Sync tab: the checkpoints cache. The synchronised checkpoints of the central server, sent (when you
     click) into the cache of another server, for tests where GPUs are free (ckcache.py) ---------- */

  S.ck = { exp: '', block: '', q: '', sel: {}, rm: {} };
  const RUN_ID = /^[0-9a-f]{32}$/;

  /* the list blocks of the XPs whose values are run ids: {key, label, ids} */
  function runBlocks() {
    return (cfg.xps || []).flatMap((x) => (x.blocks || []).filter((b) => b.kind === 'list' && (b.items || []).some((it) => RUN_ID.test(String(it.value))))
      .map((b) => ({ key: `${x.name}\u0000${b.id}`, label: `${x.name} · ${b.label}`, ids: new Set(b.items.map((it) => String(it.value))) })));
  }

  function ckCacheCard(servers, central) {
    const cat = S.data?.ckcatalog;
    const targets = servers.filter((s) => !s.central);
    const runs = (cat?.experiments || []).flatMap((x) => x.runs.map((r) => ({ ...r, exp: x })));
    const blocks = runBlocks();
    if (S.ck.block && !blocks.some((b) => b.key === S.ck.block)) S.ck.block = '';
    if (S.ck.exp && !(cat?.experiments || []).some((x) => x.id === S.ck.exp)) S.ck.exp = '';
    const blk = blocks.find((b) => b.key === S.ck.block);
    const q = S.ck.q.trim().toLowerCase();
    const shown = runs.filter((r) => (!S.ck.exp || r.exp.id === S.ck.exp) && (!blk || blk.ids.has(r.id))
      && (!q || `${r.name} ${r.id} ${r.exp.name} ${r.status}`.toLowerCase().includes(q)));
    const state = (t, r) => {
      if (!t.ckcache) return 'unknown';
      const y = t.ckcache.runs?.[r.key];
      if (!y) return 'missing';
      return y.files === r.files && y.bytes === r.bytes && y.config === r.config ? 'same' : 'differs';
    };
    const selOf = (t) => (S.ck.sel[t.name] ??= new Set());
    const cacheOf = (t) => t.paths_all.ckpt_cache;
    const total = (t) => Object.values(t.ckcache?.runs || {}).reduce((a, y) => a + (y.bytes || 0), 0);

    const select = (label, value, options, onchange) => {
      const el = h('select', { 'aria-label': label }, ...options.map(([v, t]) => h('option', { value: v }, t)));
      el.value = value;
      el.addEventListener('change', () => onchange(el.value));
      return el;
    };
    const filter = h('input', { type: 'search', placeholder: 'Filter: name, id, status', value: S.ck.q, 'aria-label': 'Filter the runs' });
    filter.addEventListener('input', () => { S.ck.q = filter.value; clearTimeout(S.ck.t); S.ck.t = setTimeout(() => { paint(); $('#ck-filter')?.focus(); }, 250); });
    filter.id = 'ck-filter';
    const controls = h('div', { class: 'ck-controls' },
      select('Experiment', S.ck.exp, [['', `All experiments (${runs.length})`], ...(cat?.experiments || []).map((x) => [x.id, `${x.name || '(no name)'} · id ${x.id} (${x.runs.length})`])],
        (v) => { S.ck.exp = v; paint(); }),
      select('Runs of an XP block', S.ck.block, [['', 'Any run (no XP block)'], ...blocks.map((b) => [b.key, `Runs of ${b.label} (${b.ids.size})`])],
        (v) => { S.ck.block = v; paint(); }),
      filter,
      h('span', { class: 'hint' }, `${shown.length} shown`));

    const readCentral = central ? (central.connected ? actionButton(central, 'ck_read', cat ? 'Read again' : 'Read the runs', {
      title: `Read-only: the runs of the synchronised database and the checkpoint folders of ${central.paths_all.sync_checkpoints}` }) : pill('off', 'Not connected')) : '';
    const readBtn = (t) => (t.connected ? actionButton(t, 'ck_status', t.ckcache ? 'Read again' : 'Read', { title: `Read-only: the runs in ${cacheOf(t)}` })
      : pill('off', 'Not connected'));
    const linkCell = () => {
      const c = central?.ckcache;
      return central?.connected ? h('span', {}, actionButton(central, 'ck_status', c ? 'Read again' : 'Read', { title: `Read-only: ${cacheOf(central)}/checkpoints` }), ' ',
        c?.link ? pill('ok', 'Linked', `${cacheOf(central)}/checkpoints -> ${c.link}`)
          : actionButton(central, 'ck_link', 'Link', { confirm: 'Click again to link', disabled: !c,
            title: c ? `${cacheOf(central)}/{checkpoints,artifacts} become links to the synchronised folders: the same paths work on ${central.name}` : 'Read it first' })) : '';
    };
    const cell = (t, r) => {
      const st = state(t, r);
      if (st === 'unknown') return h('td', { class: 'ds-cell hint' }, '?');
      if (st === 'same') {
        const rm = S.ck.rm[t.name] === r.key;
        return h('td', { class: 'ds-cell' }, pill('ok', 'There'), ' ',
          h('button', { type: 'button', class: 'ds-ign' + (rm ? ' armed' : ''), title: `Delete ${r.key} from the cache of ${t.name} (only there)`,
            disabled: S.busy[t.name + 'ck_remove'] ? '' : null,
            onclick: () => {
              if (!rm) { S.ck.rm[t.name] = r.key; paint(); setTimeout(() => { if (S.ck.rm[t.name] === r.key) { delete S.ck.rm[t.name]; paint(); } }, 4000); return; }
              delete S.ck.rm[t.name];
              run(t.name, 'ck_remove', { runs: [r.key] });
            } }, rm ? 'Click again' : 'Remove'));
      }
      const set = selOf(t);
      const cb = h('input', { type: 'checkbox', checked: set.has(r.key) ? '' : null, 'aria-label': `Send ${r.name || r.id} to ${t.name}` });
      cb.addEventListener('change', () => { if (cb.checked) set.add(r.key); else set.delete(r.key); paint(); });
      return h('td', { class: 'ds-cell' }, h('label', {}, cb, st === 'missing' ? pill('bad', 'Missing') : pill('warn', 'Differs')));
    };
    const sendBtn = (t) => {
      const keys = new Set(runs.map((r) => r.key));
      const sel = [...selOf(t)].filter((k) => keys.has(k));
      const size = runs.filter((r) => sel.includes(r.key)).reduce((a, r) => a + r.bytes, 0);
      const why = !t.connected ? `Connect ${t.name} first` : !central?.connected ? `Connect ${central?.name} first` : !sel.length ? 'Nothing selected' : '';
      return actionButton(t, 'ck_send', sel.length ? `Send ${sel.length} (${gb(size)})` : 'Send', {
        primary: true, confirm: `Click again: ${sel.length} to ${t.name}`, disabled: !!why, args: { runs: sel },
        title: why || `From ${central.name} into ${cacheOf(t)}: each checkpoint folder (tar | tar through this machine, moved into place once complete), the configs, then a check` });
    };
    const pickMissing = (t) => {
      const todo = shown.filter((r) => ['missing', 'differs'].includes(state(t, r)));
      return todo.length ? h('button', { type: 'button', class: 'tiny', title: `Select the ${todo.length} run(s) shown that ${t.name} does not have`,
        onclick: () => { const set = selOf(t); const all = todo.every((r) => set.has(r.key)); todo.forEach((r) => (all ? set.delete(r.key) : set.add(r.key))); paint(); } },
      todo.every((r) => selOf(t).has(r.key)) ? 'Unselect shown' : `Select shown (${todo.length})`) : '';
    };
    const ex = cat?.experiments?.[0];
    const sample = (t) => `${cacheOf(t)}/checkpoints/${ex?.id || '<experiment id>'}/$RUN_ID/last.ckpt`;
    const body = !central ? h('p', { class: 'empty' }, 'No central server: choose one in Servers > Edit servers.')
      : !cat ? h('p', { class: 'empty' }, `Read the runs of ${central.name} first.`)
      : cat.folder_missing ? h('p', { class: 'db-warn' }, `${cat.folder_missing} does not exist on ${central.name}: no checkpoint synchronised yet.`)
      : h('div', {},
        cat.error ? h('p', { class: 'db-warn' }, `The synchronised database did not answer: the runs are listed by id only (start it in Databases to see their names). ${cat.error}`) : '',
        controls,
        h('div', { class: 'rs-scroll' }, h('table', { class: 'db-folders sync-table ds-table ck-table' },
          h('thead', {},
            h('tr', {}, h('th', {}, 'Run'), h('th', { class: 'ds-src' }, `${central.name} (synchronised)`), ...targets.map((t) => h('th', {}, t.name, ' ', readBtn(t)))),
            h('tr', {}, h('th', { class: 'hint' }, 'name · id · status'), h('th', { class: 'ds-src hint' }, linkCell()),
              ...targets.map((t) => h('th', { class: 'hint' }, t.ckcache?.folder_missing ? 'Empty (created by a send)' : t.ckcache ? `${Object.keys(t.ckcache.runs).length} runs · ${gb(total(t))} · read ${t.ckcache.when}` : 'not read',
                ' ', pickMissing(t))))),
          h('tbody', {}, ...shown.map((r) => h('tr', {},
            h('td', { class: 'ds-name' }, h('div', {}, h('span', {}, r.name || h('i', { class: 'hint' }, 'not in the database')),
              h('code', { class: 'hint', title: r.key }, ` ${r.id.slice(0, 8)}`)),
              h('div', { class: 'hint' }, [S.ck.exp ? '' : (r.exp.name || `experiment ${r.exp.id}`), r.status, r.start].filter(Boolean).join(' · '))),
            h('td', { class: 'ds-src' }, h('span', {}, gb(r.bytes)), h('span', { class: 'hint' }, ` · ${r.files} file${r.files === 1 ? '' : 's'}`),
              r.config ? '' : h('div', { class: 'ck-warn', title: 'artifacts/config.yaml is missing in the synchronised artifacts: a test needs it' }, 'no config.yaml')),
            ...targets.map((t) => cell(t, r))))),
          h('tfoot', {}, h('tr', {}, h('td', {}, h('b', {}, 'Send')), h('td', { class: 'ds-src hint' }, `${shown.length} of ${runs.length} runs`),
            ...targets.map((t) => h('td', { class: 'ds-cell' }, t.ckcache ? sendBtn(t) : '')))))),
        h('p', { class: 'hint' }, 'In a test XP, on any server (the central one once linked): ', h('code', {}, sample(targets[0] || central)),
          ' and ', h('code', {}, `${(targets[0] || central).paths_all.ckpt_cache}/artifacts/${ex?.id || '<experiment id>'}/$RUN_ID/artifacts/config.yaml`), '.'));
    return h('div', { class: 'card sv-card ck-card' },
      h('div', { class: 'db-top' }, h('b', { class: 'db-name' }, 'Checkpoints Cache'),
        h('span', { class: 'hint' }, central ? `the synchronised checkpoints of ${central.name}, sent to other servers for tests` : ''),
        h('span', { class: 'sp' }), cat?.when ? h('span', { class: 'hint' }, `read ${cat.when}`) : '', readCentral, allButton('ck_status')),
      h('p', { class: 'hint' }, 'Send copies the selected runs from the synchronised folders into the cache of a server (Servers > Paths: Checkpoints cache), '
        + 'with the same layout: checkpoints/<experiment id>/<run> and artifacts/<experiment id>/<run>/artifacts/config.yaml. Each run goes through this machine '
        + '(tar | tar), is moved into place once complete, then files and bytes are compared. Nothing is sent or removed by itself.'),
      body);
  }

  /* ---------- Datasets tab: the processed folders of your datasets on each server, sent from a source ---------- */

  const kb = (k) => (k == null ? '' : gb(k * 1024));
  S.dsSel = S.dsSel || {};

  function paintDatasets(box) {
    const servers = listed().filter((s) => !s.disabled);
    const central = servers.find((s) => s.central);
    S.dsSource = servers.some((s) => s.name === S.dsSource) ? S.dsSource : (central || servers[0])?.name;
    const src = servers.find((s) => s.name === S.dsSource);
    const pick = h('select', { 'aria-label': 'Source server' }, ...servers.map((s) => h('option', { value: s.name }, s.name + (s.central ? ' (central)' : ''))));
    pick.value = S.dsSource || '';
    pick.addEventListener('change', () => { S.dsSource = pick.value; S.dsSel = {}; paint(); });
    const targets = servers.filter((s) => s.name !== S.dsSource);
    const sds = src?.datasets;
    const ignored = new Set(String(cfg.project.datasets_ignore || '').split(/\s+/).filter(Boolean));
    const all = Object.keys(sds?.datasets || {}).sort((a, b) => a.localeCompare(b));
    const names = all.filter((n) => !ignored.has(n));
    const hidden = all.filter((n) => ignored.has(n));
    const setIgnored = (n, on) => commit((c) => {
      const now = new Set(String(c.project.datasets_ignore || '').split(/\s+/).filter(Boolean));
      if (on) now.add(n); else now.delete(n);
      c.project.datasets_ignore = [...now].sort().join(' ');
    }).then(() => { S.dsSel = {}; paint(); status(on ? `${n} is ignored in the Datasets tab (Show again below the table)` : `${n} is listed again`); })
      .catch((e) => status(e.message, true));
    const state = (t, n) => {
      const there = t.datasets?.datasets?.[n], here = sds.datasets[n];
      if (!t.datasets) return 'unknown';
      if (!there) return 'missing';
      return there.digest && there.digest === here.digest ? 'same' : 'differs';
    };
    const selOf = (t) => (S.dsSel[t.name] ??= new Set(names.filter((n) => ['missing', 'differs'].includes(state(t, n)))));
    const cell = (t, n) => {
      const st = state(t, n), there = t.datasets?.datasets?.[n];
      if (st === 'unknown') return h('td', { class: 'ds-cell hint' }, '?');
      if (st === 'same') return h('td', { class: 'ds-cell' }, pill('ok', 'Same'), ' ', h('span', { class: 'hint' }, there.when));
      const set = selOf(t);
      const cb = h('input', { type: 'checkbox', checked: set.has(n) ? '' : null, 'aria-label': `Send ${n} to ${t.name}` });
      cb.addEventListener('change', () => { if (cb.checked) set.add(n); else set.delete(n); paint(); });
      return h('td', { class: 'ds-cell' }, h('label', {}, cb, st === 'missing' ? pill('bad', 'Missing') : pill('warn', 'Differs'),
        there ? h('code', { title: `${there.digest || 'no digest file'} · ${kb(there.kb)} · ${there.when || ''}` }, (there.digest || 'no digest').slice(0, 8)) : ''));
    };
    const readBtn = (t) => (t.connected ? actionButton(t, 'ds_status', t.datasets ? 'Read again' : 'Read', { title: `Read-only: the processed folders in ${t.paths_all.datasets}` })
      : pill('off', 'Not connected'));
    const sendBtn = (t) => {
      const sel = [...selOf(t)].filter((n) => names.includes(n) && state(t, n) !== 'same');
      const size = sel.reduce((a, n) => a + (sds.datasets[n].kb || 0), 0);
      const why = !t.connected ? `Connect ${t.name} first` : !src.connected ? `Connect ${src.name} first` : !sel.length ? 'Nothing selected' : '';
      return actionButton(t, 'ds_send', sel.length ? `Send ${sel.length} (${kb(size)})` : 'Send', {
        primary: true, confirm: `Click again: ${sel.length} to ${t.name}`, disabled: !!why, args: { source: src.name, names: sel },
        title: why || `From ${src.name}: ${sel.join(', ')} (tar | tar through this machine; each folder replaced once its copy is complete; then the digests are checked)` });
    };
    const others = targets.map((t) => Object.keys(t.datasets?.datasets || {}).filter((n) => !all.includes(n)).length);
    box.replaceChildren(
      h('div', { class: 'rs-head' }, h('h2', {}, 'Datasets'),
        h('span', { class: 'hint' }, 'The processed folders of your datasets (<name>/processed, its version in digest.sha256), sent from a source to the other servers'),
        h('span', { class: 'sp' }),
        h('span', { class: 'sv-all' }, allButton('connect'), allButton('ds_status')), refreshButton()),
      h('div', { class: 'card sv-card sync-central' }, h('b', {}, 'Source'), pick,
        src ? h('code', { class: 'sv-path' }, src.paths_all.datasets) : '',
        src ? (src.connected ? actionButton(src, 'ds_status', sds ? 'Read again' : 'Read its datasets', { title: 'Read-only: digests and sizes' }) : pill('off', 'Not connected')) : '',
        sds?.when ? h('span', { class: 'hint' }, `read ${sds.when}`) : ''),
      h('p', { class: 'hint sv-note' }, 'Compared by digest with the source. Send copies the selected processed folders from the source (tar | tar through this machine: the '
        + 'servers do not reach each other); on the target the copy is written next to the old folder and swapped in once complete, then the digests are checked. '
        + 'Only processed/ is touched. Each send is an action of the target server, with its commands (">_") and output.'),
      !src ? h('p', { class: 'empty' }, 'No server.') : !sds ? h('p', { class: 'empty' }, `Read the datasets of ${src.name} first.`)
        : sds.folder_missing ? h('p', { class: 'db-warn' }, `${sds.folder_missing} does not exist on ${src.name} (Servers > Edit servers > Paths: Datasets folder).`)
        : h('div', { class: 'card sv-card' }, h('div', { class: 'rs-scroll' }, h('table', { class: 'db-folders sync-table ds-table' },
          h('thead', {},
            h('tr', {}, h('th', {}, 'Dataset'), h('th', { class: 'ds-src' }, `${src.name} (source)`), ...targets.map((t) => h('th', {}, t.name, ' ', readBtn(t)))),
            h('tr', {}, h('th', {}), h('th', { class: 'ds-src hint' }, 'digest · size · date'), ...targets.map((t) => h('th', { class: 'hint' },
              t.datasets?.folder_missing ? 'No datasets folder yet (created by a send)' : t.datasets ? `read ${t.datasets.when}` : 'not read')))),
          h('tbody', {}, ...names.map((n) => {
            const d = sds.datasets[n];
            return h('tr', {}, h('td', { class: 'ds-name' }, h('div', {}, h('span', {}, n),
                h('button', { type: 'button', class: 'ds-ign', title: `Leave ${n} out of this list (saved in the settings; it can be shown again below the table)`,
                  'aria-label': `Ignore ${n}`, onclick: () => setIgnored(n, true) }, 'Ignore'))),
              h('td', { class: 'ds-src' }, h('code', { title: d.digest || 'no digest file' }, (d.digest || 'no digest').slice(0, 8)), h('span', { class: 'hint' }, ` · ${kb(d.kb)} · ${d.when}`)),
              ...targets.map((t) => cell(t, n)));
          })),
          h('tfoot', {}, h('tr', {}, h('td', {}, h('b', {}, 'Send')), h('td', { class: 'ds-src hint' }, `${names.length} datasets`),
            ...targets.map((t, i) => h('td', { class: 'ds-cell' }, t.datasets ? sendBtn(t) : '', others[i] ? h('div', { class: 'hint' }, `+ ${others[i]} not on the source`) : ''))))))),
      hidden.length ? h('div', { class: 'ds-ignored' }, h('span', { class: 'ds-ignored-k' }, `Ignored (${hidden.length})`),
        ...hidden.map((n) => h('span', { class: 'ds-chip' }, n,
          h('button', { type: 'button', title: `List ${n} again`, 'aria-label': `Show ${n} again`, onclick: () => setIgnored(n, false) }, '↺')))) : '',
      h('details', { class: 'card sv-card' }, h('summary', {}, h('b', {}, 'Dataset actions'), h('span', { class: 'hint' }, '  with their commands and output')),
        h('div', { class: 'rs-scroll' }, logTable('ds'))));
  }

  /* ---------- editing the list of servers ---------- */

  function editServers() {
    const all = clone(cfg.servers);
    let d = $('#sveditdlg');
    if (!d) { d = h('dialog', { id: 'sveditdlg', class: 'wide svdlg', 'aria-labelledby': 'sve-title' }); document.body.append(d); }
    const rows = all.filter((s) => 'name' in s);
    const comments = all.filter((s) => !('name' in s));
    const err = h('p', { class: 'dlg-err', hidden: '' });
    const body = h('div', { class: 'sve-list' });
    const open = new Set();
    const draw = () => {
      body.replaceChildren(...rows.map((s, i) => {
        const inp = (key, ph, w) => {
          const e = h('input', { type: 'text', value: s[key] ?? '', placeholder: ph, class: 'mono', style: w ? `width:${w}` : null, 'aria-label': `${key} of server ${i + 1}` });
          e.addEventListener('input', () => { s[key] = key === 'port' ? e.value : e.value.trim(); });
          return e;
        };
        const central = h('input', { type: 'radio', name: 'sve-central', checked: s.central ? '' : null, 'aria-label': `${s.name || 'server'} is the central server` });
        central.addEventListener('change', () => { rows.forEach((x) => { delete x.central; }); s.central = true; draw(); });
        const off = h('input', { type: 'checkbox', checked: s.disabled ? '' : null, 'aria-label': `${s.name || 'server'} is commented out` });
        off.addEventListener('change', () => { if (off.checked) s.disabled = true; else delete s.disabled; });
        const move = (j) => { const [x] = rows.splice(i, 1); rows.splice(j, 0, x); draw(); };
        const pathBox = h('div', { class: 'sve-paths', hidden: open.has(s) ? null : '' }, ...Object.keys(LABELS).filter((k) => s.central || !CENTRAL_KEYS.has(k)).map((k) => {
          const e = h('input', { type: 'text', class: 'mono', value: (s.paths || {})[k] || '', placeholder: '(default)', 'aria-label': `${LABELS[k][0]} of ${s.name}` });
          e.addEventListener('input', () => { s.paths = { ...(s.paths || {}) }; if (e.value.trim()) s.paths[k] = e.value.trim(); else delete s.paths[k]; });
          return h('label', {}, h('span', {}, LABELS[k][0], h('small', {}, LABELS[k][1])), e);
        }));
        const nPaths = Object.keys(s.paths || {}).length;
        return h('div', { class: 'sve-row' },
          h('div', { class: 'sve-main' },
            h('span', { class: 'sve-move' },
              h('button', { type: 'button', class: 'sv-btn', disabled: i === 0 ? '' : null, 'aria-label': `Move ${s.name} up`, onclick: () => move(i - 1) }, '↑'),
              h('button', { type: 'button', class: 'sv-btn', disabled: i === rows.length - 1 ? '' : null, 'aria-label': `Move ${s.name} down`, onclick: () => move(i + 1) }, '↓')),
            inp('name', 'name', '9em'), inp('host', 'Host or address', '14em'), inp('port', '22', '4.5em'), inp('user', 'user', '8em'),
            h('label', { class: 'sve-flag', title: 'Where the synchronised data lives (one server)' }, central, 'central'),
            h('label', { class: 'sve-flag', title: 'Kept in the list, never contacted' }, off, 'Commented out'),
            h('button', { type: 'button', class: 'sv-btn', onclick: () => { if (open.has(s)) open.delete(s); else open.add(s); draw(); } }, `Paths${nPaths ? ` (${nPaths} changed)` : ''}`),
            h('button', { type: 'button', class: 'sv-btn danger', onclick: () => { rows.splice(i, 1); draw(); } }, 'Remove')),
          pathBox);
      }));
    };
    draw();
    d.replaceChildren(h('div', { class: 'sv-wrap' },
      h('div', { class: 'pk-head' }, h('h3', { id: 'sve-title' }, 'Servers'), h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'ghost', onclick: () => { rows.push({ name: '', host: '', port: 22, user: '' }); draw(); } }, '+ Server'),
        h('button', { type: 'button', class: 'primary', onclick: async () => {
          try {
            for (const s of rows) {
              if (!s.name || !s.host) throw new Error('Each server needs a name and a host');
              s.port = Number(s.port || 22);
              if (s.paths && !Object.keys(s.paths).length) delete s.paths;
            }
            await commit((c) => { c.servers = [...rows, ...comments]; });
            d.close(); load();
          } catch (e) { err.textContent = e.message; err.hidden = false; }
        } }, 'Save'),
        h('button', { type: 'button', class: 'ghost', onclick: () => d.close() }, 'Close')),
      h('p', { class: 'hint' }, 'The order here is the order everywhere (and in the GPU view). Paths: leave a field empty to use the default from the base folder.'),
      err, body));
    d.showModal();
  }

  /* ---------- the tabs ---------- */

  $('main').append(h('section', { id: 'serverstab', 'aria-label': 'Servers' }), h('section', { id: 'databasestab', 'aria-label': 'Databases' }),
    h('section', { id: 'synctab', 'aria-label': 'Sync' }), h('section', { id: 'datasetstab', 'aria-label': 'Datasets' }));
  window.XPServers = {
    show(tab = 'servers') { S.tab = tab; if (!S.data) load(); else paint(); },
    hide() { S.tab = null; },
    reload: load,
  };
})();
