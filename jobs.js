/* Jobs (server side: jobs.py): your containers running on this machine and on the connected servers, as the launch
   scripts start them; their last output, and Stop (asked twice, logged as an action). The Jobs button of the side
   panel opens it; it is read again every 15 s while open.
   Depends on editors.js ($, h, api, status). */
(() => {
  let dlg = null, timer = null, last = null, busy = false, past = null, pastBusy = false;
  const pastOpen = {};                              // "server\u0000log name" -> its output shown
  const open = {};                                  // "server\u0000id" -> the logs shown under a job
  const asking = {};                               // the same key -> waiting for "Confirm stop"

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

  async function showPast(p, j) {
    const k = `${p.server || ''}\u0000${j.name}`;
    if (pastOpen[k] !== undefined) { delete pastOpen[k]; paint(); return; }
    pastOpen[k] = 'reading...';
    paint();
    try { pastOpen[k] = (await api('/api/jobs/past_log', { server: p.server || '', name: j.name })).text || '(empty)'; } catch (e) { pastOpen[k] = e.message; }
    paint();
  }

  const STATE = { running: ['ok', 'Running'], done: ['ok', 'Done'], failed: ['bad', 'Failed'], unknown: ['off', 'No exit status'] };

  function pastRow(p, j) {
    const k = `${p.server || ''}\u0000${j.name}`;
    const [cls, text] = STATE[j.state] || ['off', j.state];
    return h('li', { class: 'q-item j-past-item ' + j.state },
      h('div', { class: 'j-main' },
        h('div', { class: 'q-what' }, h('span', { class: 'sv-pill ' + cls, title: j.exit != null ? `exit code ${j.exit}` : 'The recorder stopped before the job (e.g. the launcher restarted)' },
          text + (j.state === 'failed' ? ` (exit ${j.exit})` : '')), ' ', h('b', {}, j.xp), ' ',
          h('span', { class: 'q-opt' }, h('small', {}, 'container'), ' ', j.id)),
        h('span', { class: 'hint' }, `started ${j.started}`),
        h('div', { class: 'q-acts' }, h('button', { type: 'button', class: 'tiny', 'aria-pressed': String(pastOpen[k] !== undefined), onclick: () => showPast(p, j) },
          pastOpen[k] !== undefined ? 'Hide Output' : 'Output'))),
      pastOpen[k] !== undefined ? h('pre', { class: 'j-logs' }, pastOpen[k]) : '');
  }

  function pastSection() {
    const places = (past?.places || []).filter((p) => p.jobs.length || p.error);
    const n = (past?.places || []).reduce((a, p) => a + p.jobs.length, 0);
    return h('section', { class: 'j-place j-past' },
      h('div', { class: 'pk-head' }, h('h4', {}, 'Past Jobs'),
        h('span', { class: 'hint' }, past ? `${n} kept output${n === 1 ? '' : 's'} (the last ${60} per server), read at ${past.when || '...'}` : 'The outputs kept by the launches'),
        h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'ghost', onclick: refreshPast }, pastBusy ? 'Reading...' : past ? 'Read Again' : 'Read')),
      past?.error ? h('p', { class: 'dlg-err' }, past.error) : '',
      past && !n ? h('p', { class: 'hint' }, 'No kept output yet: the jobs launched from now on keep theirs (@@KEEP_LOGS@@ in the template).') : '',
      ...places.map((p) => h('div', { class: 'j-past-place' },
        h('h4', {}, p.label, ' ', h('span', { class: 'hint' }, p.error || p.folder || '')),
        p.jobs.length ? h('ol', { class: 'q-list' }, ...p.jobs.map((j) => pastRow(p, j))) : '')));
  }

  async function showLogs(p, j) {
    const k = keyOf(p, j);
    if (open[k] !== undefined) { delete open[k]; paint(); return; }
    open[k] = 'reading...';
    paint();
    try { open[k] = (await api('/api/jobs/logs', { server: p.server || '', id: j.id })).text || '(no output yet)'; } catch (e) { open[k] = e.message; }
    paint();
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
          h('button', { type: 'button', class: 'tiny', 'aria-pressed': String(open[k] !== undefined), onclick: () => showLogs(p, j) }, open[k] !== undefined ? 'Hide output' : 'Output'),
          asking[k] ? h('button', { type: 'button', class: 'tiny', onclick: () => { delete asking[k]; paint(); } }, 'Cancel') : '',
          h('button', { type: 'button', class: 'tiny danger', title: 'docker stop: the job gets SIGTERM, then SIGKILL after 30 s', onclick: () => stop(p, j) },
            asking[k] ? 'Confirm stop' : 'Stop'))),
      h('div', { class: 'q-meta hint' }, `${j.id} · started ${j.started} UTC`, j.launch ? ' · launch ' : '', j.launch ? h('code', {}, j.launch) : '',
        j.cmd ? h('span', { class: 'j-cmd', title: j.cmd }, ` · ${j.cmd}`) : ''),
      asking[k] ? h('p', { class: 'q-note' }, 'Stop this job? It runs: ', h('code', {}, asking[k])) : '',
      open[k] !== undefined ? h('pre', { class: 'j-logs' }, open[k]) : '');
  }

  function paint() {
    if (!dlg) return;
    const places = last?.places || [];
    const n = places.reduce((a, p) => a + p.jobs.length, 0);
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
