/* Servers and Databases tabs. Servers: the servers of the settings, their paths, the connection to each (opened when
   you click Connect, over ssh with your agent), their state, and the log of every action. Databases: the Postgres of
   each server (and the synchronised one on the central server): their state, and starting / stopping them, or a new
   dated database. Each action button has a ">_" button that shows its commands without running them; after an action
   ran, its commands and their output are shown (and kept in the action log).
   Depends on editors.js ($, h, api, status) and app.js (cfg, commit, clone). */
(() => {
  const S = { data: null, tab: null, busy: {}, log: [], pick: {}, modif: {}, image: {}, other: {}, syncSel: {} };
  const LABELS = {
    base: ['Base folder', 'the other paths default to it'], db_pattern: ['Own databases', 'Postgres data folders (<server>_<date>_db)'],
    psql_sock: ['Own Postgres socket', ''], pg_port: ['Own Postgres port', ''], artifacts: ['Artifacts', 'of the runs made here'],
    checkpoints: ['Checkpoints', 'of the runs made here'], launcher: ['Launcher folder', 'repo mirror, launches, queue'],
    scripts: ['Your scripts', 'launch-server.sh, server.sh (not used by the page)'], empty_db: ['Empty database', 'copied to make a new dated one'],
    synchronised: ['Synchronised data', 'central server only'], sync_sock: ['Synchronised Postgres socket', 'central server only'],
    sync_port: ['Synchronised Postgres port', 'central server only'],
    db_image: ['Database image', 'default image to start the databases in (else the Docker image variable); can be changed at each start'],
    ui_port: ['Own MLflow UI port', 'on the server'], sync_ui_port: ['Synchronised MLflow UI port', 'on the server'],
  };
  const DERIVED = [['repo', 'Repo mirror'], ['launches', 'Launch folders'], ['queue', 'Queue'], ['sync_db_pattern', 'Synchronised databases'],
    ['sync_artifacts', 'Synchronised artifacts'], ['sync_checkpoints', 'Synchronised checkpoints']];
  const CENTRAL_KEYS = new Set(['synchronised', 'sync_sock', 'sync_port', 'sync_ui_port', 'sync_db_pattern', 'sync_artifacts', 'sync_checkpoints']);

  /* Postgres state: running, stale (socket file left, nothing answers), stopped */
  const STALE = 'socket file left but nothing answers on it: Postgres was killed (e.g. its container stopped without a clean shutdown). Starting removes it.';
  const pgPill = (state) => state === 'running' ? pill('ok', 'running') : state === 'stale' ? pill('warn', 'stopped (stale socket)', STALE) : pill('bad', 'stopped');
  const pill = (cls, text, title) => h('span', { class: 'sv-pill ' + cls, title: title || null }, text);
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
      if (quiet) { delete S.busy[server + action]; return r.result.ok; }
      status(`${r.result.title} ${r.result.ok ? 'done' : 'failed'}${after && r.result.ok ? `: ${after}` : ''}`, !r.result.ok,
        { label: 'Commands and output', fn: () => commandsDialog(r.result.title, r.steps, r.result.id) });
      if (!r.result.ok || ['db_scripts', 'db_start', 'db_new', 'sync_run'].includes(action)) commandsDialog(r.result.title, r.steps, r.result.id);
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
  };

  function allButton(action, opts = {}) {
    const [label, applies] = ALL[action];
    const targets = listed().filter(applies);
    const busy = S.busy['*' + action];
    return h('button', { type: 'button', class: 'sv-btn' + (opts.danger ? ' danger' : ''), disabled: busy || !targets.length ? '' : null,
      title: targets.length ? `${targets.map((t) => t.name).join(', ')} (each logged as its own action)` : 'no server to do it on',
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
        id ? h('span', { class: 'hint' }, `logged in actions/${id}/commands.log`) : h('span', { class: 'hint' }, 'nothing has run'),
        h('button', { type: 'button', class: 'ghost', onclick: () => d.close() }, 'Close')),
      ...steps.map((s, i) => h('div', { class: 'sv-step' + (s.planned ? '' : s.rc === 0 ? ' ok' : ' bad') },
        h('div', { class: 'sv-step-head' }, h('b', {}, `${i + 1}. ${s.label}`), h('span', { class: 'sp' }),
          s.planned ? '' : h('span', { class: 'hint' }, `exit ${s.rc} · ${s.secs} s`)),
        h('pre', { class: 'sv-cmd' }, s.display),
        !s.planned && (s.out || s.err) ? h('details', { class: 'sv-out', open: '' }, h('summary', {}, 'output'),
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
    if (!st?.gpus?.length) return h('span', { class: 'hint' }, st ? 'none seen' : '?');
    return h('span', { class: 'sv-gpus' }, ...st.gpus.map((g) => h('span', { class: 'sv-gpu' + (g.free ? ' free' : ''), title: `GPU ${g.index}: ${Math.round(g.mem_used)} / ${Math.round(g.mem_total)} MiB${g.free ? ', free' : ''}` }, String(g.index))));
  }

  function pgSummary(state, latest, n) {
    if (!state) return h('span', { class: 'hint' }, '?');
    return h('div', { class: 'sv-stack' }, pgPill(state),
      latest ? h('code', { class: 'sv-path' }, base(latest)) : h('span', { class: 'hint' }, 'no folder'), n > 1 ? h('span', { class: 'hint' }, `newest of ${n}`) : '');
  }

  function connCell(sv) {
    return h('div', { class: 'sv-stack' }, sv.connected ? pill('ok', 'connected') : pill('off', 'not connected'),
      sv.connected ? actionButton(sv, 'disconnect', 'Disconnect')
        : actionButton(sv, 'connect', 'Connect', { primary: true, title: 'Open one ssh connection (your agent asks for the key), reused by every command' }));
  }

  function serverRow(sv) {
    const st = sv.status;
    return h('tr', { class: (sv.disabled ? 'sv-disabled' : '') + (sv.central ? ' sv-central' : '') },
      h('td', {}, h('div', { class: 'sv-stack' }, h('b', {}, sv.name), sv.central ? pill('ref', 'central') : '', sv.disabled ? pill('off', 'commented out') : '',
        h('small', { class: 'sv-addr' }, `${sv.user ? sv.user + '@' : ''}${sv.host}${sv.port && sv.port !== 22 ? ':' + sv.port : ''}`))),
      h('td', {}, connCell(sv)),
      h('td', {}, pgSummary(st?.pg, st?.db_latest, st?.dbs?.length || 0)),
      h('td', {}, sv.central ? pgSummary(st?.sync_pg, st?.sync_db_latest, st?.sync_dbs?.length || 0) : h('span', { class: 'hint' }, '-')),
      h('td', {}, gpuChips(st)),
      h('td', { class: 'num' }, st?.disk_free_gb != null ? `${st.disk_free_gb} GB` : h('span', { class: 'hint' }, '?')),
      h('td', {}, h('div', { class: 'sv-stack' }, h('span', { class: 'hint' }, st ? st.when : 'never read'),
        actionButton(sv, 'status', 'Read state', { disabled: !sv.connected, title: sv.connected ? 'Read Postgres, folders, GPUs and disk (read-only)' : 'Connect first' }))));
  }

  function pathsTable(servers) {
    const keys = [...Object.keys(LABELS), ...DERIVED.map(([k]) => k)];
    const label = (k) => LABELS[k] || [DERIVED.find(([x]) => x === k)[1], 'from the launcher / synchronised folder'];
    return h('table', { class: 'sv-paths' },
      h('thead', {}, h('tr', {}, h('th', {}), ...servers.map((s) => h('th', {}, s.name, s.central ? ' ★' : '')))),
      h('tbody', {}, ...keys.map((k) => h('tr', {}, h('th', { scope: 'row' }, label(k)[0], label(k)[1] ? h('small', {}, label(k)[1]) : ''),
        ...servers.map((s) => {
          if (CENTRAL_KEYS.has(k) && !s.central) return h('td', {}, h('span', { class: 'hint' }, '-'));
          const own = (s.paths || {})[k];
          const v = s.paths_all[k];
          return h('td', {}, v ? h('code', { class: 'sv-path' + (own ? ' changed' : ''), title: own ? 'changed for this server' : 'default' }, v) : h('span', { class: 'hint' }, k === 'db_image' ? 'Docker image variable' : '-'));
        })))));
  }

  function logTable(kind) {
    const rows = S.log.filter((a) => !kind || a.kind.startsWith(kind + '_'));
    if (!rows.length) return h('p', { class: 'hint' }, 'No action yet.');
    return h('table', { class: 'sv-log-table' },
      h('thead', {}, h('tr', {}, ...['When', 'Action', 'Server', 'Steps', 'Result', ''].map((t) => h('th', {}, t)))),
      h('tbody', {}, ...rows.slice(0, 40).map((a) => h('tr', {},
        h('td', { class: 'hint' }, a.when), h('td', {}, a.title), h('td', {}, a.server || '-'), h('td', { class: 'num' }, String(a.steps)),
        h('td', {}, a.ok ? pill('ok', 'done') : pill('bad', 'failed')),
        h('td', {}, h('button', { type: 'button', class: 'sv-btn', onclick: () => openLog(a.id) }, 'Commands and output'))))));
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
        h('button', { type: 'button', class: 'ghost', onclick: editServers }, 'Edit servers'),
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
        } }, 'Make it the central server')) : '';
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
        h('td', {}, f === inUse ? pill('ok', 'in use') : ''));
    });
    const modif = h('input', { type: 'checkbox', checked: S.modif[key] ? '' : null });
    modif.addEventListener('change', () => { S.modif[key] = modif.checked; paint(); });
    const running = db.state === 'running';
    return h('div', { class: 'db-sec' },
      h('div', { class: 'db-head' }, h('b', {}, title), pgPill(db.state), h('code', { class: 'sv-path' }, where)),
      notCentral,
      h('dl', { class: 'db-facts' },
        h('dt', {}, 'Folder in use'), h('dd', {}, inUse ? h('code', { class: 'sv-path', title: db.data_directory ? 'asked to Postgres (show data_directory)' : 'from the container' }, inUse) : h('span', { class: 'hint' }, running ? 'unknown' : 'none (stopped)')),
        h('dt', {}, 'Container'), h('dd', {}, c ? h('span', {}, h('code', {}, c.id), ` ${c.name} · ${c.image} · started ${c.started}`,
          h('span', { class: 'hint' }, ` · by ${c.by}${c.allow_modif ? ' · changes allowed in the MLflow UI' : ''}`))
          : h('span', { class: 'hint' }, 'none of yours running')),
        ...(c ? [h('dt', {}, 'MLflow UI'), h('dd', {}, h('code', {}, `${sv.host}:${which === 'own' ? p.ui_port : p.sync_ui_port}`))] : [])),
      folders.length ? h('div', { class: 'rs-scroll' }, h('table', { class: 'db-folders' },
        h('thead', {}, h('tr', {}, ...['', 'Folder', 'Date', 'Size', ''].map((t) => h('th', {}, t)))), h('tbody', {}, ...rows)))
        : h('p', { class: 'hint' }, `no folder matches ${which === 'own' ? p.db_pattern : p.sync_db_pattern}`),
      imagePicker(sv, key),
      h('div', { class: 'db-acts' },
        actionButton(sv, 'db_start', `Start on ${pick ? base(pick) : '...'}`, { primary: true, disabled: !sv.connected || running || !pick || !S.image[key],
          args: { which, folder: pick, allow_modif: !!S.modif[key], image: S.image[key] }, after: 'read the databases again to see it',
          title: running ? 'Already running: stop it first' : `db/launch.sh in ${S.image[key]}: Postgres, then the MLflow UI on port ${which === 'own' ? p.ui_port : p.sync_ui_port}` }),
        h('label', { class: 'db-opt', title: 'launch.sh --allow-modif: the MLflow UI can change the database (else it reads only)' }, modif, 'allow changes in the MLflow UI'),
        actionButton(sv, 'db_stop', 'Stop', { danger: true, confirm: 'Click again to stop', disabled: !sv.connected || !c, args: { which, container: c?.id }, after: 'read the databases again to see it',
          title: c ? `docker container stop ${c.id}` : 'no container of yours is running' }),
        actionButton(sv, 'db_new', 'New dated database', { confirm: 'Click again to create it', disabled: !sv.connected || !sv.db?.empty_db, args: { which },
          title: sv.db?.empty_db ? `a copy of ${p.empty_db}` : `${p.empty_db} not found` }),
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
      onclick: () => choose(x.image) }, x.tag, x.image === def ? h('small', {}, 'default') : '');
    const otherBtn = h('button', { type: 'button', class: 'db-tag' + (other ? ' on' : ''), onclick: () => { S.other = { ...S.other, [key]: true }; paint(); $(`.db-image-txt[data-key="${key}"]`)?.focus(); } }, 'other…');
    const rows = [...repos].map(([r, tags], n, all) => h('div', { class: 'db-tags' }, h('code', { class: 'db-repo' }, r), ...tags.map(tagBtn),
      n === all.length - 1 ? otherBtn : ''));
    const txt = h('input', { type: 'text', value: other ? cur : '', placeholder: 'repository:tag', class: 'db-image-txt', spellcheck: 'false', 'data-key': key, 'aria-label': 'Image name' });
    txt.addEventListener('change', () => { S.image[key] = txt.value.trim(); paint(); });
    const there = found.includes(cur);
    const meta = imgs.find((x) => x.image === cur);
    return h('div', { class: 'db-image' },
      h('span', { class: 'db-image-lbl' }, 'Image'),
      h('div', { class: 'db-image-body' },
        ...(rows.length ? rows : [h('div', { class: 'db-tags' }, h('span', { class: 'hint' }, 'no image found on the server'), otherBtn)]),
        other ? h('div', { class: 'db-tags' }, txt) : '',
        cur ? h('div', { class: 'db-image-state' },
          there ? pill('ok', 'on the server') : pill('bad', 'not on the server', 'starting never downloads it: pull it first'),
          h('span', { class: 'hint' }, meta ? `created ${meta.created} · ${meta.size}` : ''),
          !there ? actionButton(sv, 'db_pull', 'Pull', { disabled: !sv.connected, args: { image: cur }, after: 'read the databases again to see it', title: `docker pull ${cur} on ${sv.name} (can take minutes)` }) : '',
          cur !== def && def ? h('button', { type: 'button', class: 'db-reset', onclick: () => choose(def) }, 'back to the default') : '') : ''));
  }

  /* a synchronised database: on the central server always, elsewhere when one was found */
  const hasSync = (sv) => sv.central || (sv.db?.sync && (sv.db.sync.folders.length || sv.db.sync.state === 'running'));

  function noSync(sv) {
    const p = sv.paths_all;
    return h('div', { class: 'db-sec db-nosync' },
      h('div', { class: 'db-head' }, h('b', {}, 'Synchronised database'), h('span', { class: 'hint' }, sv.db ? 'none on this server' : '')),
      h('p', { class: 'hint' }, `Creates ${p.synchronised} (artifacts, checkpoints, socket) and copies ${base(p.empty_db)} to ${base(p.synchronised)}/db_<today>. `
        + 'Make this server the central one afterwards to sync into it.'),
      h('div', { class: 'db-acts' }, actionButton(sv, 'db_new', 'Create a synchronised database here', {
        confirm: 'Click again to create it', args: { which: 'sync' }, after: 'read the databases again to see it',
        disabled: !sv.connected || !sv.db || !sv.db.empty_db,
        title: !sv.db ? 'Read the databases first' : sv.db.empty_db ? `a copy of ${p.empty_db}` : `${p.empty_db} not found on ${sv.name}` })));
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
      h('div', { class: 'db-top' }, h('b', { class: 'db-name' }, sv.name), sv.central ? pill('ref', 'central') : '',
        sv.connected ? pill('ok', 'connected') : pill('off', 'not connected'),
        h('span', { class: 'sp' }), sv.db ? h('span', { class: 'hint' }, `read ${sv.db.when}`) : '',
        sv.connected ? '' : actionButton(sv, 'connect', 'Connect', { primary: true }),
        actionButton(sv, 'db_status', 'Read the databases', { disabled: !sv.connected, title: 'Read-only: sockets, containers, folders, the folder in use' }),
        actionButton(sv, 'db_scripts', 'Show your scripts', { disabled: !sv.connected, title: `cat ${sv.paths_all.scripts}/launch-server.sh and server.sh` })),
      h('div', { class: 'db-secs' }, dbSection(sv, 'own'), hasSync(sv) ? dbSection(sv, 'sync') : noSync(sv)));
  }

  function paintDatabases(box) {
    const servers = listed().filter((s) => !s.disabled);
    box.replaceChildren(
      h('div', { class: 'rs-head' }, h('h2', {}, 'Databases'), h('span', { class: 'hint' }, 'the Postgres of each server, and the synchronised one on the central server'),
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
    if (S.tab === 'servers') paintServers(box); else if (S.tab === 'sync') paintSync(box); else paintDatabases(box);
  }

  /* ---------- Sync tab ---------- */

  const gb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : b >= 1e3 ? `${Math.round(b / 1e3)} kB` : `${b} B`);

  function syncCard(sv, central) {
    const pv = sv.sync;
    const own = sv.db?.own;
    const cdb = central?.db?.sync;
    const notReady = !sv.connected ? 'connect it first' : !central?.connected ? `connect ${central?.name} first`
      : own && own.state !== 'running' ? 'its database is not running (Databases)' : cdb && cdb.state !== 'running' ? `the synchronised database on ${central.name} is not running (Databases)` : '';
    const head = h('div', { class: 'db-top' }, h('b', { class: 'db-name' }, sv.name), sv.connected ? pill('ok', 'connected') : pill('off', 'not connected'),
      h('span', { class: 'hint' }, 'own database'), own ? pgPill(own.state) : h('span', { class: 'hint' }, '? (read the databases)'),
      h('span', { class: 'sp' }),
      actionButton(sv, 'sync_preview', pv ? 'Preview again' : 'Preview', { disabled: !!notReady, title: notReady || 'Read-only: what a sync would copy (runs, files, sizes)' }));
    const parts = [head];
    if (sv.synced) {
      parts.push(h('p', { class: sv.synced.ok ? 'hint' : 'db-warn' }, `Last sync ${sv.synced.when}: ${sv.synced.ok ? 'done and checked' : 'failed (nothing of the failed step was kept)'}. `,
        h('button', { type: 'button', class: 'db-reset', onclick: () => openLog(sv.synced.id) }, 'commands and output'),
        sv.synced.out ? h('pre', { class: 'sync-out' }, sv.synced.out.trim()) : ''));
    }
    if (!pv) {
      parts.push(h('p', { class: 'hint' }, notReady ? `To preview: ${notReady}.` : 'Preview to see what a sync would copy. Nothing is copied before you click Sync.'));
      return h('div', { class: 'card sv-card db-card' }, ...parts);
    }
    const sel = S.syncSel[sv.name] ??= new Set(pv.experiments.filter((x) => x.new + x.changed).map((x) => x.name));
    const todo = pv.experiments.filter((x) => sel.has(x.name) && x.new + x.changed);
    const tot = (k, list = pv.experiments) => list.reduce((a, x) => a + x[k], 0);
    const row = (x) => {
      const can = x.new + x.changed > 0;
      const cb = h('input', { type: 'checkbox', checked: can && sel.has(x.name) ? '' : null, disabled: can ? null : '', 'aria-label': `Sync ${x.name}` });
      cb.addEventListener('change', () => { if (cb.checked) sel.add(x.name); else sel.delete(x.name); paint(); });
      const n = (v, cls) => h('td', { class: 'num' + (v ? (cls ? ' ' + cls : '') : ' hint') }, String(v || 0));
      return h('tr', { class: can ? '' : 'sync-done' }, h('td', {}, cb), h('td', {}, x.name, x.deleted ? h('span', { class: 'hint' }, ' (deleted)') : ''),
        n(x.new, 'sync-new'), n(x.changed, 'sync-chg'), n(x.synced), h('td', { class: 'num' + (x.old_path ? '' : ' hint'), title: x.old_example ? `left alone, e.g. ${x.old_example}` : null }, String(x.old_path || 0)),
        h('td', { class: 'num' }, can ? `${x.files} · ${gb(x.bytes)}` : ''),
        h('td', { class: 'hint' }, x.dst_id ? `id ${x.src_id} → ${x.dst_id}` : `id ${x.src_id} → created`));
    };
    parts.push(
      h('p', { class: 'hint' }, `Preview of ${pv.when}: into ${pv.dst_folder} on ${pv.central} (files under ${pv.dst_artifacts.replace(/\/artifacts$/, '')}/{artifacts,checkpoints}). MLflow schema ${pv.schema}.`),
      ...(pv.blocking || []).map((b) => h('p', { class: 'db-warn' }, b)),
      h('div', { class: 'rs-scroll' }, h('table', { class: 'db-folders sync-table' },
        h('thead', {}, h('tr', {}, ...['', 'Experiment', 'New', 'Changed', 'Already synced', 'Older paths', 'Files to copy', 'Experiment id'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...pv.experiments.map(row)),
        h('tfoot', {}, h('tr', {}, h('td', {}), h('td', {}, h('b', {}, 'Selected')), h('td', { class: 'num' }, String(tot('new', todo))), h('td', { class: 'num' }, String(tot('changed', todo))),
          h('td', {}), h('td', {}), h('td', { class: 'num' }, `${tot('files', todo)} · ${gb(tot('bytes', todo))}`), h('td', {}))))),
      Object.keys(pv.not_copied || {}).length ? h('p', { class: 'hint' }, 'Not copied (not used by this project): ' + Object.entries(pv.not_copied).map(([k, v]) => `${v} ${k}`).join(', ')) : '',
      h('p', { class: 'hint' }, '"Changed": already synced, but its status, end time, params, tags, last metrics or datasets changed since: it is copied again. '
        + '"Older paths": runs whose artifacts are not under ' + pv.src_artifacts + ': left alone.'),
      h('div', { class: 'db-acts' },
        actionButton(sv, 'sync_run', todo.length ? `Sync ${tot('new', todo) + tot('changed', todo)} run${tot('new', todo) + tot('changed', todo) > 1 ? 's' : ''} (${gb(tot('bytes', todo))})` : 'Sync', {
          primary: true, confirm: 'Click again to sync', disabled: !!notReady || !todo.length || (pv.blocking || []).length > 0,
          args: { experiments: todo.map((x) => x.name) }, title: notReady || 'Copies the files, then the rows (one transaction), then checks both' })));
    return h('div', { class: 'card sv-card db-card' }, ...parts);
  }

  function paintSync(box) {
    const servers = listed().filter((s) => !s.disabled);
    const central = servers.find((s) => s.central);
    const cdb = central?.db?.sync;
    box.replaceChildren(
      h('div', { class: 'rs-head' }, h('h2', {}, 'Sync'),
        h('span', { class: 'hint' }, central ? `runs, artifacts and checkpoints of each server → the synchronised database on ${central.name}` : 'no central server: choose one in Servers > Edit servers'),
        h('span', { class: 'sp' }),
        h('span', { class: 'sv-all' }, allButton('connect'), allButton('db_status')), refreshButton()),
      central ? h('div', { class: 'card sv-card sync-central' },
        h('b', {}, `Into ${central.name}`), cdb ? pgPill(cdb.state) : h('span', { class: 'hint' }, '? (read the databases)'),
        h('code', { class: 'sv-path' }, cdb?.data_directory || cdb?.container?.folder || central.paths_all.sync_db_pattern),
        h('span', { class: 'hint' }, `files: ${central.paths_all.sync_artifacts}, ${central.paths_all.sync_checkpoints}`)) : '',
      h('p', { class: 'hint sv-note' }, 'Everything goes through this machine over your ssh connections: the two Postgres sockets are tunnelled here (ssh -O forward), '
        + 'the rows are copied in one transaction, the run folders go through tar | tar. Files first, then rows, then both are checked; '
        + 'a sync that fails keeps no rows (run it again). Nothing is deleted anywhere. ">_" shows every command.'),
      ...servers.map((sv) => syncCard(sv, central)),
      h('details', { class: 'card sv-card' }, h('summary', {}, h('b', {}, 'Sync actions'), h('span', { class: 'hint' }, '  with their commands and output')),
        h('div', { class: 'rs-scroll' }, logTable('sync'))));
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
            inp('name', 'name', '9em'), inp('host', 'host or address', '14em'), inp('port', '22', '4.5em'), inp('user', 'user', '8em'),
            h('label', { class: 'sve-flag', title: 'Where the synchronised data lives (one server)' }, central, 'central'),
            h('label', { class: 'sve-flag', title: 'Kept in the list, never contacted' }, off, 'commented out'),
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
    h('section', { id: 'synctab', 'aria-label': 'Sync' }));
  window.XPServers = {
    show(tab = 'servers') { S.tab = tab; if (!S.data) load(); else paint(); },
    hide() { S.tab = null; },
    reload: load,
  };
})();
