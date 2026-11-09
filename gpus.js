/* GPU usage of the servers (side panel button): memory, load and the processes on each GPU.
   The current server is read locally (ssh only for the owners of new processes), the others with one ssh call.
   Depends on editors.js ($, h, api, openDialog) and app.js (cfg, table, commit). */
(() => {
  let dlg = null, timer = null, info = null, server = null;
  const KEY = 'xp-gpu-server';
  const MiB = (v) => (v == null ? '?' : v >= 1024 ? `${(v / 1024).toFixed(1)} GiB` : `${Math.round(v)} MiB`);
  const ago = (s) => (s == null ? '' : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 * 2 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} d`);

  /* GPU indices of APP_SETTINGS__DEVICES in the current selection, e.g. "[1, 3]" */
  function selected() {
    const name = Object.keys(table).find((n) => /DEVICES$/.test(n) && n.startsWith(cfg.project.env_prefix));
    return new Set((table[name]?.value || '').match(/\d+/g)?.map(Number) || []);
  }

  function bar(frac, cls) {
    const pct = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
    return h('div', { class: 'gpu-bar ' + cls, role: 'img', 'aria-label': `${pct}%` }, h('span', { style: `width: ${pct}%` }));
  }

  function card(g, mine) {
    const used = g.mem_total ? g.mem_used / g.mem_total : 0;
    const free = !g.processes.length && (g.mem_used ?? 0) < 512;
    const rows = g.processes.map((p) => h('tr', {},
      h('td', {}, p.visible ? p.user : h('span', { class: 'hint', title: p.why || 'not visible' }, '?')),
      h('td', { class: 'mono' }, p.pid),
      h('td', { class: 'mono num' }, MiB(p.mem)),
      h('td', { class: 'num' }, ago(p.elapsed)),
      h('td', { class: 'mono cmd', title: p.cmd || '' }, p.xp ? h('b', {}, `XP=${p.xp}`) : '', p.xp ? ' ' : '', p.cmd || '')));
    return h('div', { class: 'gpu' + (mine ? ' mine' : '') + (free ? ' free' : '') },
      h('div', { class: 'gpu-head' }, h('b', {}, `#${g.index}`), h('span', {}, g.name),
        mine ? h('span', { class: 'gpu-tag mine', title: 'APP_SETTINGS__DEVICES of the selected XP (when it runs on this server)' }, 'this XP (DEVICES)') : '',
        free ? h('span', { class: 'gpu-tag free' }, 'free') : '',
        h('span', { class: 'sp' }), h('span', { class: 'hint' }, g.temp != null ? `${g.temp} °C` : '')),
      h('div', { class: 'gpu-meters' },
        h('span', { class: 'gpu-k' }, 'memory'), bar(used, used > .9 ? 'hot' : 'mem'),
        h('span', { class: 'gpu-v mono' }, `${MiB(g.mem_used)} / ${MiB(g.mem_total)}  (${MiB(g.mem_free)} free)`),
        h('span', { class: 'gpu-k' }, 'load'), bar((g.util ?? 0) / 100, 'util'),
        h('span', { class: 'gpu-v mono' }, g.util == null ? '?' : `${g.util} %`)),
      rows.length ? h('table', { class: 'gpu-procs' },
        h('thead', {}, h('tr', {}, ...['user', 'pid', 'memory', 'running', 'command'].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...rows)) : h('p', { class: 'hint' }, 'No process.'));
  }

  const ALL = '__all__';
  const isCurrent = () => !server || server === info?.current || server === 'local';
  const MiBs = (v) => (v == null ? '?' : v >= 1024 ? `${(v / 1024).toFixed(0)}G` : `${Math.round(v)}M`);

  /* all servers: one tile per server (like the tmux panes of states.sh), a compact line per GPU */
  function tile(sv, mine) {
    const head = h('div', { class: 'gt-head' }, h('b', {}, sv.server), sv.server === info?.current ? h('span', { class: 'gpu-tag mine' }, 'this machine') : '', h('span', { class: 'sp' }));
    if (!sv.ok) {
      head.append(h('span', { class: 'gt-state bad' }, 'unreachable'));
      return h('div', { class: 'gt bad', title: sv.error }, head, h('p', { class: 'gt-err' }, sv.error));
    }
    const free = sv.gpus.filter((g) => !g.processes.length && (g.mem_used ?? 0) < 512).length;
    head.append(h('span', { class: 'gt-state' + (free ? ' ok' : ' busy') }, `${free}/${sv.gpus.length} free`));
    const lines = sv.gpus.map((g) => {
      const used = g.mem_total ? g.mem_used / g.mem_total : 0;
      const users = {};
      for (const p of g.processes) { const u = p.visible ? p.user : '?'; users[u] = (users[u] || 0) + 1; }
      const isFree = !g.processes.length && (g.mem_used ?? 0) < 512;
      return h('div', { class: 'gt-gpu' + (isFree ? ' free' : '') + (mine?.has(g.index) ? ' mine' : ''),
        title: `#${g.index} ${g.name}\n${MiBs(g.mem_used)} / ${MiBs(g.mem_total)} used, load ${g.util ?? '?'} %, ${g.temp ?? '?'} °C`
          + g.processes.map((p) => `\n  ${p.visible ? p.user : '?'} pid ${p.pid} ${MiBs(p.mem)}${p.xp ? ' XP=' + p.xp : ''}`).join('') },
        h('span', { class: 'gt-idx' }, `#${g.index}`),
        bar(used, used > 0.9 ? 'hot' : 'mem'),
        h('span', { class: 'gt-mem mono' }, `${MiBs(g.mem_used)}/${MiBs(g.mem_total)}`),
        h('span', { class: 'gt-util mono' }, g.util == null ? '' : `${g.util}%`),
        h('span', { class: 'gt-users' }, ...(Object.keys(users).length ? Object.entries(users).map(([u, n]) => h('span', { class: 'gt-user', title: n > 1 ? `${u}: ${n} processes` : u }, n > 1 ? `${u} ×${n}` : u)) : [h('span', { class: 'hint' }, isFree ? 'free' : '')])));
    });
    return h('button', { type: 'button', class: 'gt', title: `Open ${sv.server}`, onclick: () => pick(sv.server) }, head, ...lines,
      h('div', { class: 'gt-foot hint' }, `${sv.hostname || ''} · ${sv.ms} ms`));
  }

  function pick(name) {
    server = name;
    const sel = $('#gpu-server');
    if (sel) sel.value = name;
    try { localStorage.setItem(KEY, server); } catch (e) { /* storage unavailable */ }
    $('#gpu-body').replaceChildren(h('p', { class: 'empty' }, 'Asking ' + server + '...'));
    schedule(); refresh();
  }

  async function refreshAll() {
    const body = $('#gpu-body');
    const t0 = performance.now();
    if (!body.querySelector('.gt-grid')) body.replaceChildren(h('p', { class: 'empty' }, 'Asking every server (in parallel)...'));
    try {
      const r = await api('/api/gpus/all');
      if (server !== ALL) return;
      const mine = selected();
      const ok = r.servers.filter((x) => x.ok);
      const gpus = ok.flatMap((x) => x.gpus);
      const free = gpus.filter((g) => !g.processes.length && (g.mem_used ?? 0) < 512).length;
      body.replaceChildren(h('div', { class: 'gt-grid' }, ...r.servers.map((x) => tile(x, x.server === r.current ? mine : null))));
      $('#gpu-time').textContent = `${free} free GPUs out of ${gpus.length} on ${ok.length}/${r.servers.length} servers - ${Math.round(performance.now() - t0)} ms - ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      body.replaceChildren(h('p', { class: 'dlg-err' }, e.message));
    }
  }

  async function refresh() {
    const body = $('#gpu-body');
    if (!body) return;
    if (server === ALL) { refreshAll(); return; }
    const t0 = performance.now();
    try {
      const r = await api('/api/gpus' + (isCurrent() ? '' : `?server=${encodeURIComponent(server)}`));
      const mine = isCurrent() ? selected() : new Set();
      const hidden = r.gpus.some((g) => g.processes.some((p) => !p.visible));
      body.replaceChildren(...r.gpus.map((g) => card(g, mine.has(g.index))),
        hidden ? h('p', { class: 'dlg-note' }, 'Processes marked ? could not be named (hover for the reason).') : '',
        r.note ? h('p', { class: 'pk-warn' }, r.note) : '');
      const how = r.mode === 'local'
        ? `local nvidia-smi${r.via ? `, owners through ${r.via}` : ''}: ${r.ssh_calls} ssh call${r.ssh_calls === 1 ? '' : 's'}`
        : `ssh ${r.via}: 1 call`;
      $('#gpu-time').textContent = `${r.hostname} - ${how} - ${Math.round(performance.now() - t0)} ms - ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      body.replaceChildren(h('p', { class: 'dlg-err' }, e.message),
        h('p', { class: 'dlg-note' }, 'The server must be reachable with ssh from where the launcher runs, with the key in the ssh agent (BatchMode: no password prompt).'));
      $('#gpu-time').textContent = '';
    }
  }

  function schedule() {
    clearInterval(timer);
    const every = server === ALL ? 20000 : isCurrent() ? 5000 : 15000;
    timer = $('#gpu-auto')?.checked ? setInterval(refresh, every) : null;
    const lab = $('#gpu-auto-label');
    if (lab) lab.textContent = ` every ${every / 1000} s`;
  }

  /* one line per server: Name host[:port] [user] */
  function editServers() {
    const text = (info?.listed || info?.servers || []).map((s) => (s.comment != null ? s.comment
      : `${s.disabled ? '# ' : ''}${s.name} ${s.host}${s.port && s.port !== 22 ? ':' + s.port : ''}${s.user ? ' ' + s.user : ''}`)).join('\n');
    openDialog({
      title: 'GPU servers',
      wide: true,
      fields: [{ key: 'text', type: 'area', rows: 10, mono: true, label: 'One server per line: name host[:port] [user]', value: text,
        hint: 'A line starting with #, // or % is ignored (the server stays in the list, not asked). The launcher connects with ssh (keys from the ssh agent, no password prompt). The server it runs on is found by itself '
          + '(host name, or the docker host when it runs in a container) and is read locally.' }],
      onSubmit: async (v) => {
        const parse = (l) => {
          const [name, addr, user, ...rest] = l.split(/\s+/);
          if (!addr || rest.length) return null;
          const [host, port] = addr.split(':');
          if (!/^[\w.-]+$/.test(host) || (port !== undefined && !/^\d+$/.test(port)) || (user && !/^[\w.-]+$/.test(user))) return null;
          return { name, host, port: Number(port || 22), user: user || '' };
        };
        const servers = v.text.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
          const mark = l.match(/^(#|\/\/|%)\s*/);
          if (!mark) {
            const s = parse(l);
            if (!s) throw new Error(`"${l}": give a name and a host[:port], then optionally a user`);
            return s;
          }
          const s = parse(l.slice(mark[0].length));
          return s ? { ...s, disabled: true } : { comment: l };          // a commented server, or just a comment
        });
        const before = Object.fromEntries((cfg.servers || []).filter((s) => s.name).map((s) => [s.name, s]));
        for (const s of servers) {
          const old = before[s.name];
          if (old?.central) s.central = true;
          if (old?.paths) s.paths = old.paths;
        }
        await commit((c) => { c.servers = servers; });
        await openServers(true);
      },
    });
  }

  async function openServers(refreshId = false) {
    try { info = await api('/api/gpus/servers' + (refreshId ? '?refresh=1' : '')); } catch (e) { info = { servers: [], current: null, how: e.message }; }
    const sel = $('#gpu-server');
    if (!sel) return;
    const opts = info.servers.map((s) => h('option', { value: s.name }, s.name + (s.name === info.current ? '  (this machine)' : '')));
    if (!info.current && info.local_gpus) opts.unshift(h('option', { value: 'local' }, `this machine (${info.hostname})`));
    if (info.servers.length) opts.unshift(h('option', { value: ALL }, 'All servers (summary)'));
    sel.replaceChildren(...opts);
    let want = server;
    try { want = want || localStorage.getItem(KEY); } catch (e) { /* storage unavailable */ }
    if (!want || ![...sel.options].some((o) => o.value === want)) want = info.current || (info.local_gpus ? 'local' : info.servers[0]?.name);
    server = want; sel.value = want ?? '';
    $('#gpu-how').textContent = info.current ? `This machine is ${info.current} (found by ${info.how}).` : `This machine is not one of the servers (${info.how}).`;
    schedule();
    refresh();
  }

  function open() {
    if (!dlg) {
      dlg = h('dialog', { id: 'gpudlg', class: 'wide gpudlg', 'aria-labelledby': 'gpu-title' });
      dlg.addEventListener('close', () => { clearInterval(timer); timer = null; });
      document.body.append(dlg);
    }
    const auto = h('input', { type: 'checkbox', id: 'gpu-auto' });
    auto.checked = true;
    auto.addEventListener('change', schedule);
    const sel = h('select', { id: 'gpu-server', 'aria-label': 'Server' });
    sel.addEventListener('change', () => pick(sel.value));
    dlg.replaceChildren(h('div', { class: 'gpu-wrap' },
      h('div', { class: 'pk-head' }, h('h3', { id: 'gpu-title' }, 'GPUs on'), sel,
        h('button', { type: 'button', class: 'tiny', onclick: editServers, title: 'Edit the list of servers' }, 'Servers...'),
        h('span', { class: 'sp' }),
        h('label', { class: 'hint gpu-auto' }, auto, h('span', { id: 'gpu-auto-label' }, ' every 5 s')),
        h('button', { type: 'button', class: 'ghost', onclick: refresh }, 'Refresh'),
        h('button', { type: 'button', class: 'ghost', onclick: () => dlg.close() }, 'Close')),
      h('div', { class: 'gpu-sub' }, h('span', { class: 'hint', id: 'gpu-how' }), h('span', { class: 'hint', id: 'gpu-time' })),
      h('div', { id: 'gpu-body', class: 'gpu-list' }, h('p', { class: 'empty' }, 'Reading nvidia-smi...'))));
    dlg.showModal();
    openServers();
  }

  $('#gpus').addEventListener('click', () => open());
  // for router.js (the address of the page)
  window.XPGpus = {
    get server() { return dlg?.open ? server : null; },
    show(name) { if (name) server = name; if (!dlg?.open) open(); else if (name) pick(name); },
    hide() { if (dlg?.open) dlg.close(); },
  };
})();
