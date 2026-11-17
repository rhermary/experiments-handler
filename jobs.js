/* Jobs (server side: jobs.py): your containers running on this machine and on the connected servers, as the launch
   scripts start them; their last output, and Stop (asked twice, logged as an action). The Jobs button of the side
   panel opens it; it is read again every 15 s while open.
   Depends on editors.js ($, h, api, status). */
(() => {
  let dlg = null, timer = null, last = null, busy = false, past = null, pastBusy = false;
  const pastOpen = {};                              // "server\u0000log name" -> its output shown
  const showHidden = {};                            // server -> its hidden outputs listed too
  const open = {};                                  // "server\u0000id" -> the logs shown under a job
  const asking = {};                               // the same key -> waiting for "Confirm stop"
  const vars = {};                                 // the same key -> the variables shown (read when asked)

  const dur = (s) => (s == null ? '' : s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`
    : `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`);
  const keyOf = (p, j) => `${p.server || ''}\u0000${j.id}`;

  async function refresh() {
    if (busy) return;
    busy = true;
    try { last = await api('/api/jobs'); } catch (e) { last = { error: e.message, places: [] }; }
    busy = false;
    badge();
    if (dlg?.open) paint();
  }

  function badge() {
    const n = (last?.places || []).reduce((a, p) => a + p.jobs.length, 0);
    $('#jobs').textContent = n ? `Jobs (${n} running)` : 'Jobs';
  }

  async function refreshPast() {
    if (pastBusy) return;
    pastBusy = true; paint();
    try { past = await api('/api/jobs/past'); } catch (e) { past = { error: e.message, places: [] }; }
    pastBusy = false;
    if (dlg?.open) paint();
  }

  /* an output box: shown at its bottom (the newest lines) when read */
  const reading = new Set();                        // keys being read again (their Refresh button says so)
  // the tag of an output box: its key encoded (a key holds \u0000, which CSS.escape turns into U+FFFD: no match)
  const outTag = (k) => encodeURIComponent(k);
  const outBox = (tag) => dlg?.querySelector(`pre[data-out="${CSS.escape(tag)}"]`);
  function toBottom(k) {                            // after paint() put back the old positions (its microtask runs first)
    queueMicrotask(() => {
      const pre = outBox(outTag(k));
      if (pre) pre.scrollTop = pre.scrollHeight;
    });
  }
  async function readOut(store, k, fetch, empty) {
    reading.add(k); paint();
    try { store[k] = (await fetch()).text || empty; } catch (e) { store[k] = e.message; }
    reading.delete(k); paint();
    toBottom(k);
  }
  const refreshBtn = (k, fn) => h('button', { type: 'button', class: 'tiny', disabled: reading.has(k) ? '' : null,
    title: 'Read the output again and go to its end', onclick: fn }, reading.has(k) ? 'Refreshing...' : 'Refresh');

  const pastKey = (p, j) => `${p.server || ''}\u0000${j.name}`;
  const readPast = (p, j) => readOut(pastOpen, pastKey(p, j), () => api('/api/jobs/past_log', { server: p.server || '', name: j.name }), '(empty)');
  async function showPast(p, j) {
    const k = pastKey(p, j);
    if (pastOpen[k] !== undefined) { delete pastOpen[k]; paint(); return; }
    pastOpen[k] = 'reading...';
    await readPast(p, j);
  }

  /* hide (or show again) kept outputs: only the listing changes, the log files stay */
  async function setHidden(p, js, hidden) {
    if (!js.length) return;
    js.forEach((j) => { j.hidden = hidden; });
    paint();
    try { await api('/api/jobs/past_hide', { server: p.server || '', names: js.map((j) => j.name), hidden }); }
    catch (e) { js.forEach((j) => { j.hidden = !hidden; }); past.error = e.message; paint(); }
  }

  const STATE = { running: ['ok', 'Running'], done: ['ok', 'Done'], failed: ['bad', 'Failed'], unknown: ['off', 'No exit status'] };

  function pastRow(p, j) {
    const k = `${p.server || ''}\u0000${j.name}`;
    const [cls, text] = STATE[j.state] || ['off', j.state];
    return h('li', { class: 'q-item j-past-item ' + j.state + (j.hidden ? ' is-hidden' : '') },
      h('div', { class: 'j-main' },
        h('div', { class: 'q-what' }, h('span', { class: 'sv-pill ' + cls, title: j.exit != null ? `exit code ${j.exit}` : 'The recorder stopped before the job (e.g. the launcher restarted)' },
          text + (j.state === 'failed' ? ` (exit ${j.exit})` : '')), ' ', h('b', {}, j.xp), ' ',
          h('span', { class: 'q-opt' }, h('small', {}, 'container'), ' ', j.id)),
        h('span', { class: 'hint' }, `started ${j.started}`),
        h('div', { class: 'q-acts' }, h('button', { type: 'button', class: 'tiny', 'aria-pressed': String(pastOpen[k] !== undefined), onclick: () => showPast(p, j) },
          pastOpen[k] !== undefined ? 'Hide Output' : 'Output'),
          pastOpen[k] !== undefined ? refreshBtn(k, () => readPast(p, j)) : '',
          h('button', { type: 'button', class: 'tiny', title: j.hidden ? 'List it again in Past Jobs' : 'Seen: stop listing it (the log file is kept)',
            onclick: () => setHidden(p, [j], !j.hidden) }, j.hidden ? 'Unhide' : 'Hide'))),
      pastOpen[k] !== undefined ? h('pre', { class: 'j-logs', 'data-out': outTag(k) }, pastOpen[k]) : '');
  }

  function pastPlace(p) {
    const key = p.server || '';
    const visible = p.jobs.filter((j) => !j.hidden), hidden = p.jobs.filter((j) => j.hidden);
    const finished = visible.filter((j) => j.state !== 'running');
    const listed = showHidden[key] ? p.jobs : visible;
    return h('div', { class: 'j-past-place' },
      h('div', { class: 'pk-head' }, h('h4', {}, p.label, ' ', h('span', { class: 'hint' }, p.error || p.folder || '')),
        h('span', { class: 'sp' }),
        hidden.length ? h('button', { type: 'button', class: 'tiny', 'aria-pressed': String(!!showHidden[key]),
          onclick: () => { showHidden[key] = !showHidden[key]; paint(); } }, showHidden[key] ? `Leave Out ${hidden.length} Hidden` : `Show ${hidden.length} Hidden`) : '',
        finished.length ? h('button', { type: 'button', class: 'tiny', title: 'Hide every listed job that has ended (the log files are kept)',
          onclick: () => setHidden(p, finished, true) }, `Hide ${finished.length} Ended`) : ''),
      listed.length ? h('ol', { class: 'q-list' }, ...listed.map((j) => pastRow(p, j))) : '');
  }

  function pastSection() {
    const places = (past?.places || []).filter((p) => p.jobs.length || p.error);
    const n = (past?.places || []).reduce((a, p) => a + p.jobs.filter((j) => !j.hidden).length, 0);
    return h('section', { class: 'j-place j-past' },
      h('div', { class: 'pk-head' }, h('h4', {}, 'Past Jobs'),
        h('span', { class: 'hint' }, past ? `${n} kept output${n === 1 ? '' : 's'} (the last ${60} per server, hidden ones left out), read at ${past.when || '...'}` : 'The outputs kept by the launches'),
        h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'ghost', onclick: refreshPast }, pastBusy ? 'Reading...' : past ? 'Read Again' : 'Read')),
      past?.error ? h('p', { class: 'dlg-err' }, past.error) : '',
      past && !n && !(past.places || []).some((p) => p.jobs.length) ? h('p', { class: 'hint' }, 'No kept output yet: the jobs launched from now on keep theirs (@@KEEP_LOGS@@ in the template).') : '',
      ...places.map(pastPlace));
  }

  const readLogs = (p, j) => readOut(open, keyOf(p, j), () => api('/api/jobs/logs', { server: p.server || '', id: j.id }), '(no output yet)');
  /* the variables of the task now (read inside the container): the loop ones (this iteration) first */
  async function showVars(p, j) {
    const k = keyOf(p, j);
    if (vars[k] !== undefined) { delete vars[k]; paint(); return; }
    vars[k] = null; paint();
    try { vars[k] = await api('/api/jobs/vars', { server: p.server || '', id: j.id }); } catch (e) { vars[k] = { error: e.message }; }
    paint();
  }
  function varsBox(v) {
    if (v === null) return h('p', { class: 'hint' }, 'Reading the variables of the task...');
    if (v.error) return h('p', { class: 'dlg-err' }, v.error);
    const pfx = (cfg?.project?.env_prefix) || '';
    const row = (x) => h('tr', { class: x.loop ? 'j-var-loop' : '' }, h('td', { class: 'mono', title: x.name }, x.name.slice(pfx.length)),
      h('td', { class: 'mono' }, x.value), h('td', { class: 'hint' }, x.loop ? 'this iteration' : ''));
    const loop = v.vars.filter((x) => x.loop), rest = v.vars.filter((x) => !x.loop);
    return h('div', { class: 'j-vars' },
      h('p', { class: 'hint' }, v.container_only ? 'No task running at this moment (between two iterations of the loop): the variables of the container'
        : h('span', {}, 'Task: ', h('code', {}, v.task))),
      h('div', { class: 'rs-scroll' }, h('table', { class: 'j-var-table' }, h('tbody', {}, ...loop.map(row), ...rest.map(row)))));
  }

  async function showLogs(p, j) {
    const k = keyOf(p, j);
    if (open[k] !== undefined) { delete open[k]; paint(); return; }
    open[k] = 'reading...';
    await readLogs(p, j);
  }

  async function stop(p, j) {
    const k = keyOf(p, j);
    if (!asking[k]) {
      try { asking[k] = (await api('/api/jobs/stop', { server: p.server || '', id: j.id, dry: true })).plan[0].display; } catch (e) { status(e.message, true); return; }
      paint();
      return;
    }
    delete asking[k];
    status(`Stopping ${j.xp || j.name} on ${p.label}...`);
    try {
      const r = await api('/api/jobs/stop', { server: p.server || '', id: j.id });
      status(r.ok ? `Stopped ${j.xp || j.name} on ${p.label}` : `Could not stop it: ${r.output}`, !r.ok);
    } catch (e) { status(e.message, true); }
    refresh();
  }

  function jobRow(p, j) {
    const k = keyOf(p, j);
    return h('li', { class: 'q-item' },
      h('div', { class: 'j-main' },
        h('div', { class: 'q-what' }, h('b', {}, j.xp || j.name), ' ',
          h('span', { class: 'q-opt', title: 'Container' }, h('small', {}, 'Container'), ' ', j.name),
          j.devices ? h('span', { class: 'q-opt', title: 'The devices variable' }, h('small', {}, 'GPUs'), ' ', j.devices) : '',
          h('span', { class: 'q-opt' }, h('small', {}, 'Image'), ' ', j.image)),
        h('span', { class: 'hint', title: `Started ${j.started} (UTC)` }, `Running for ${dur(j.age_s)}`),
        h('div', { class: 'q-acts' },
          h('button', { type: 'button', class: 'tiny', 'aria-pressed': String(open[k] !== undefined), onclick: () => showLogs(p, j) }, open[k] !== undefined ? 'Hide Output' : 'Output'),
          open[k] !== undefined ? refreshBtn(k, () => readLogs(p, j)) : '',
          h('button', { type: 'button', class: 'tiny', 'aria-pressed': String(vars[k] !== undefined),
            title: 'The variables of the task as it runs now, with the values the command loop gives this iteration', onclick: () => showVars(p, j) },
            vars[k] !== undefined ? 'Hide Variables' : 'Variables'),
          asking[k] ? h('button', { type: 'button', class: 'tiny', onclick: () => { delete asking[k]; paint(); } }, 'Cancel') : '',
          h('button', { type: 'button', class: 'tiny danger', title: 'docker stop: the job gets SIGTERM, then SIGKILL after 30 s', onclick: () => stop(p, j) },
            asking[k] ? 'Confirm stop' : 'Stop'))),
      h('div', { class: 'q-meta hint' }, `${j.id} · started ${j.started} UTC`, j.launch ? ' · launch ' : '', j.launch ? h('code', {}, j.launch) : '',
        j.config ? ' · config ' : '', j.config ? h('code', { title: j.cmd }, j.config) : ''),
      vars[k] !== undefined ? varsBox(vars[k]) : '',
      asking[k] ? h('p', { class: 'q-note' }, 'Stop this job? It runs: ', h('code', {}, asking[k])) : '',
      open[k] !== undefined ? h('pre', { class: 'j-logs', 'data-out': outTag(k) }, open[k]) : '');
  }

  function paint() {
    if (!dlg) return;
    const kept = new Map([...dlg.querySelectorAll('pre[data-out]')].map((x) => [x.dataset.out,
      x.scrollTop + x.clientHeight >= x.scrollHeight - 4 ? 'bottom' : x.scrollTop]));
    const places = last?.places || [];
    const n = places.reduce((a, p) => a + p.jobs.length, 0);
    queueMicrotask(() => kept.forEach((at, tag) => {           // after the new content is in
      const pre = outBox(tag);
      if (pre) pre.scrollTop = at === 'bottom' ? pre.scrollHeight : at;
    }));
    dlg.replaceChildren(h('div', { class: 'q-wrap' },
      h('div', { class: 'pk-head' }, h('h3', { id: 'j-title' }, 'Jobs'),
        h('span', { class: 'hint' }, last ? `${n} running · read at ${last.when || '...'} · every 15 s` : 'reading...'),
        h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'ghost', onclick: refresh }, busy ? 'Reading...' : 'Refresh'),
        h('button', { type: 'button', class: 'ghost', onclick: () => dlg.close() }, 'Close')),
      h('p', { class: 'dlg-note' }, 'Your containers running on this machine and on the connected servers (the ones running as you; the launcher\'s own, '
        + 'its databases and itself, are left out). Output: the last 200 lines. Stop: docker stop, asked twice and logged with the actions of the server. '
        + 'Past Jobs: the outputs kept by the launches (the last 400 lines), with their exit status.'),
      last?.error ? h('p', { class: 'dlg-err' }, last.error) : '',
      ...places.map((p) => h('section', { class: 'j-place' + (p.connected ? '' : ' off') },
        h('h4', {}, p.label, ' ', h('span', { class: 'hint' }, p.error || `${p.jobs.length} job${p.jobs.length === 1 ? '' : 's'}`)),
        p.jobs.length ? h('ol', { class: 'q-list' }, ...p.jobs.map((j) => jobRow(p, j))) : '')),
      pastSection()));
  }

  function show() {
    if (!dlg) {
      dlg = h('dialog', { id: 'jobsdlg', class: 'wide queuedlg', 'aria-labelledby': 'j-title' });
      dlg.addEventListener('close', () => { clearInterval(timer); timer = null; });
      document.body.append(dlg);
    }
    paint();
    dlg.showModal();
    refresh();
    refreshPast();
    clearInterval(timer);
    timer = setInterval(refresh, 15000);
  }

  $('#jobs').addEventListener('click', show);
  refresh();
  setInterval(() => { if (!dlg?.open) refresh(); }, 60000);   // the count on the button
  window.XPJobs = { open: show, refresh };
})();
