/* Queue of experiments (server side: xpqueue.py). "Add to queue" freezes the current selection into a script; the
   queue starts the items one after the other, each when enough GPUs are free, and sets only the devices variable
   then. Items run on this machine or on the server chosen in "Run on" when they were queued: each server has its own
   line. The Queue button of the side panel shows it.
   Depends on editors.js ($, h, api, status, openDialog) and app.js (cfg, st, table, blk, xpDef). */
(() => {
  let dlg = null, timer = null, last = null;
  const STATUS = { waiting: 'waiting', starting: 'starting', started: 'started', failed: 'failed' };

  function devicesVar() {
    return Object.keys(table).find((n) => n.startsWith(cfg.project.env_prefix) && /DEVICES$/i.test(n));
  }
  function parseDevices(v) {
    const t = String(v || '').trim();
    if (t.startsWith('[')) { const idx = (t.match(/\d+/g) || []).map(Number); return { idx, n: Math.max(1, idx.length) }; }
    if (/^\d+$/.test(t)) return { idx: null, n: Math.max(1, Number(t)) };
    return { idx: null, n: 1 };
  }

  function badge() {
    const b = $('#queue');
    if (!b || !last) return;
    const waiting = last.items.filter((x) => x.status === 'waiting' && !x.manual).length;
    const manual = last.items.filter((x) => x.status === 'waiting' && x.manual).length;
    b.textContent = `Queue${waiting ? ` (${waiting} waiting)` : ''}${manual ? ` (${manual} manual)` : ''}${last.paused && waiting ? ' - paused' : ''}`;
  }

  async function refresh() {
    try { last = await api('/api/queue' + (dlg?.open ? '?gpus=1' : '')); } catch (e) { return; }
    badge();
    if (dlg?.open) paint();
  }

  /* ---------- adding the current selection ---------- */

  /* the GPUs of where an item runs: this machine, or its server */
  function gpusOf(server) { return server ? last.server_gpus?.[server]?.gpus || null : last.gpus || null; }

  function addDialog() {
    if (!st.xp) { status('Create an XP first', true); return; }
    const server = runOn();
    const dv = devicesVar();
    const value = dv ? table[dv]?.value ?? '' : '';
    const p = parseDevices(value);
    const opts = xpDef().blocks.filter((b) => b.rows.length > 1).map((b) => `${b.label}: ${st.blocks[b.id]}`).join(' · ');
    openDialog({
      title: `Queue "${st.xp}"${server ? ` on ${server}` : ''}`,
      fields: [
        { key: 'when', type: 'select', label: 'Start', value: 'auto', options: [
          { value: 'auto', label: 'automatically, in its turn, when its GPUs are free' },
          { value: 'manual', label: 'manually: it waits in the queue until I start it (Start now)' }] },
        { type: 'note', label: `${server ? `Runs on ${server} (Run on, next to Launch), in its own line, when its GPUs are free. ` : ''}${opts || 'no option to choose'}${st.loop ? ` · loop: ${st.loop}` : ''}. The script is written now from this selection (later edits of the XP do not change it); `
          + (dv ? `only ${dv} is set when it starts.` : 'there is no devices variable (…DEVICES): it starts as soon as it is first in the queue.') },
        ...(dv ? [
          { key: 'mode', type: 'select', label: `GPUs (${dv} is ${value || 'empty'} now)`, value: 'auto', options: [
            { value: 'auto', label: 'any free GPUs, as many as below' },
            ...(p.idx ? [{ value: 'fixed', label: `exactly ${value}, when they are free` }] : [])] },
          { key: 'count', label: 'How many GPUs (any free ones)', value: String(p.n) },
        ] : []),
      ],
      submitLabel: 'Add to queue',
      onSubmit: async (v) => {
        const count = v.count === undefined ? undefined : Number(v.count);
        if (count !== undefined && !(count >= 1)) throw new Error('How many GPUs: a number, 1 or more');
        const r = await api('/api/queue/add', { sel: st, mode: v.mode || 'auto', count, manual: v.when === 'manual', server });
        last = r.queue;
        badge();
        if (r.item.manual) { status(`Queued "${st.xp}" to start by hand: Queue > Start now`, false, { label: 'Show the queue', fn: open }); return; }
        const pos = last.items.filter((x) => x.status === 'waiting' && !x.manual && (x.server || '') === server).length;
        status(`Queued "${st.xp}" (${pos === 1 ? 'first in line' : `#${pos} in line`}): it starts when ${r.item.mode === 'fixed' ? `GPUs ${r.item.devices_var ? value : ''} are` : r.item.mode === 'none' ? 'it is first' : `${r.item.count} GPU(s) are`} free`, false,
          { label: 'Show the queue', fn: open });
      },
    });
  }

  /* ---------- the queue ---------- */

  /* GPUs chosen in the queue window for each waiting item (until it starts); otherwise a suggestion */
  const chosen = {};

  /* {item id: [GPU indices]}: what each waiting item would start on: the GPUs picked by hand, else its fixed ones,
     else the next free ones not suggested to an item above it */
  function suggestions(items) {
    const taken = {}, out = {};                              // per server: GPUs suggested to an item above
    for (const it of items.filter((x) => x.status === 'waiting')) {
      const where = it.server || '';
      const t = taken[where] || (taken[where] = new Set());
      const free = (gpusOf(it.server) || []).filter((g) => g.free).map((g) => g.index);
      if (chosen[it.id]) out[it.id] = chosen[it.id];
      else if (it.mode === 'fixed') out[it.id] = it.fixed || [];
      else out[it.id] = free.filter((g) => !t.has(g)).slice(0, it.count);
      out[it.id].forEach((g) => t.add(g));
    }
    return out;
  }

  function gpuPicker(it, sel) {
    if (it.mode === 'none') return h('span', { class: 'hint' }, 'no devices variable');
    const list = gpusOf(it.server);
    if (!list) {
      const input = h('input', { type: 'text', class: 'mono q-devin', value: chosen[it.id] ? `[${chosen[it.id].join(', ')}]` : it.queued_value || '',
        'aria-label': `${it.devices_var} for ${it.xp}`, placeholder: '[0]' });
      input.addEventListener('input', () => { const m = input.value.match(/\d+/g); chosen[it.id] = m ? m.map(Number) : []; });
      return h('span', { class: 'q-gpus' }, h('span', { class: 'hint' }, 'GPUs:'), input);
    }
    return h('span', { class: 'q-gpus', role: 'group', 'aria-label': `GPUs to start ${it.xp} on` },
      ...list.map((g) => h('button', { type: 'button', class: 'q-gpu ' + (g.free ? 'free' : 'busy'), 'aria-pressed': String(sel.includes(g.index)),
        title: `GPU ${g.index} (${g.name}): ${g.free ? 'free' : g.why}; ${Math.round(g.mem_used)} / ${Math.round(g.mem_total)} MiB. Click to choose it for this item`,
        onclick: () => {
          const now = new Set(sel);
          if (now.has(g.index)) now.delete(g.index); else now.add(g.index);
          chosen[it.id] = [...now].sort((a, b) => a - b);
          paint();
        } }, String(g.index))));
  }

  async function startOn(it, devices) {
    try { last = await api('/api/queue/act', { action: 'start', id: it.id, devices }); } catch (e) { status(e.message, true); return; }
    delete chosen[it.id];
    badge(); paint();
  }

  async function act(action, id) {
    try { last = await api('/api/queue/act', { action, id }); } catch (e) { status(e.message, true); return; }
    badge();
    paint();
  }

  function itemRow(it, i, items, sugg) {
    const waiting = it.status === 'waiting';
    const want = it.mode === 'fixed' ? `GPUs [${(it.fixed || []).join(', ')}]` : it.mode === 'none' ? 'no devices variable' : `${it.count} free GPU${it.count > 1 ? 's' : ''}`;
    const given = it.devices != null && it.status !== 'waiting' ? ` → ${it.devices_var}=${it.devices}` : '';
    const btn = (label, action, title, cls = 'tiny') => h('button', { type: 'button', class: cls, title, onclick: () => act(action, it.id) }, label);
    return h('li', { class: 'q-item ' + it.status },
      h('div', { class: 'q-main' },
        h('span', { class: 'q-pos', title: it.manual ? 'started by hand only' : 'its turn in the queue' },
          waiting ? (it.manual ? '✋' : String(items.filter((x, k) => k <= i && x.status === 'waiting' && !x.manual && (x.server || '') === (it.server || '')).length)) : ''),
        h('div', { class: 'q-what' },
          h('b', {}, it.xp), ' ', h('span', { class: 'q-opt', title: 'where it runs (Run on, when it was queued)' }, it.server ? `on ${it.server}` : 'this machine'),
          h('div', { class: 'q-opts' }, ...(it.options || []).map((o) => h('span', { class: 'q-opt' }, h('small', {}, o.block), ' ', o.option)),
            it.loop ? h('span', { class: 'q-opt loop', title: 'command loop' }, it.loop) : '')),
        h('div', { class: 'q-dev' }, h('span', {}, want), h('span', { class: 'hint' }, given), waiting ? gpuPicker(it, sugg[it.id] || []) : ''),
        h('span', { class: 'q-status ' + it.status + (waiting && it.manual ? ' manual' : '') }, waiting && it.manual ? 'manual' : STATUS[it.status] || it.status),
        h('div', { class: 'q-acts' },
          waiting && items.slice(0, i).some((x) => x.status === 'waiting') ? btn('↑', 'up', 'Earlier in the queue') : '',
          waiting && items.slice(i + 1).some((x) => x.status === 'waiting') ? btn('↓', 'down', 'Later in the queue') : '',
          waiting ? (() => {
            const sel = sugg[it.id] || [];
            const ok = it.mode === 'none' || sel.length;
            return h('button', { type: 'button', class: 'tiny q-start' + (it.manual ? ' primary' : ''), disabled: ok ? null : '',
              title: ok ? 'Start it now on the GPUs selected on the left' : 'Select GPUs first',
              onclick: () => startOn(it, it.mode === 'none' ? undefined : `[${sel.join(', ')}]`) }, it.mode === 'none' ? 'Start' : `Start on ${sel.join(', ') || '…'}`);
          })() : '',
          waiting ? btn(it.manual ? 'Make automatic' : 'Make manual', it.manual ? 'auto' : 'manual',
            it.manual ? 'Let the queue start it in its turn, when its GPUs are free' : 'Keep it in the queue but start it only by hand') : '',
          it.status === 'failed' ? btn('Retry', 'retry', 'Put it back in the queue') : '',
          it.status !== 'starting' ? btn('Remove', 'remove', '', 'tiny danger') : '')),
      h('div', { class: 'q-meta hint' }, `queued ${it.created.replace('T', ' ')}`, it.started ? ` · started ${it.started.replace('T', ' ')}` : '',
        it.path ? ' · ' : '', it.path ? h('code', {}, it.path) : '', it.folder ? ` · on ${it.server}: ` : '', it.folder ? h('code', {}, it.folder) : ''),
      it.output ? h('details', { class: 'q-out' }, h('summary', {}, it.status === 'failed' ? 'Output (failed)' : 'Output of the launch'), h('pre', {}, it.output)) : '');
  }

  function paint() {
    if (!dlg || !last) return;
    if (dlg.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;   // typing devices
    const items = last.items;
    for (const id of Object.keys(chosen)) if (!items.some((x) => x.id === id && x.status === 'waiting')) delete chosen[id];
    const sugg = suggestions(items);
    const waiting = items.filter((x) => x.status === 'waiting').length;
    const done = items.length - waiting - items.filter((x) => x.status === 'starting').length;
    dlg.replaceChildren(h('div', { class: 'q-wrap' },
      h('div', { class: 'pk-head' }, h('h3', { id: 'q-title' }, 'Queue'),
        h('span', { class: 'hint' }, `${waiting} waiting · checked every ${last.poll_s} s`),
        h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'ghost', onclick: () => act(last.paused ? 'resume' : 'pause') }, last.paused ? 'Resume' : 'Pause'),
        done ? h('button', { type: 'button', class: 'ghost', title: 'Remove the started and failed items', onclick: () => act('clear') }, `Clear ${done} done`) : '',
        h('button', { type: 'button', class: 'ghost', onclick: () => dlg.close() }, 'Close')),
      h('p', { class: 'dlg-note' }, 'Items start one after the other, each when enough GPUs are free (no process, under 512 MiB used), on this machine or on the server they were queued for '
        + '(each server has its own line; its GPUs are read over ssh); only the devices variable is set then. A GPU just given to an item counts as taken for 5 minutes. Manual items (✋) wait until you start them, '
        + 'and do not hold up the others. Start launches an item now on the GPUs selected next to it (a suggestion: click a GPU to change it). Add the current selection with "Add to queue" next to Launch.'),
      last.note ? h('p', { class: 'q-note' + (last.paused ? ' paused' : '') }, last.paused ? 'Paused: nothing starts until you resume.' + (last.note !== 'paused' ? ` ${last.note}` : '') : last.note) : '',
      h('p', { class: 'q-gpuline hint' }, ...(last.gpus ? ['GPUs of this machine:', ...last.gpus.map((g) => h('span', { class: 'q-gpu-s ' + (g.free ? 'free' : 'busy'), title: g.why || 'free' }, `${g.index} ${g.free ? 'free' : g.why}`))]
        : [last.gpus_note || ''])),
      ...Object.entries(last.server_gpus || {}).map(([name, x]) => h('p', { class: 'q-gpuline hint' }, ...(x.gpus
        ? [`GPUs of ${name}:`, ...x.gpus.map((g) => h('span', { class: 'q-gpu-s ' + (g.free ? 'free' : 'busy'), title: g.why || 'free' }, `${g.index} ${g.free ? 'free' : g.why}`))]
        : [x.note || `GPUs of ${name} unknown`]))),
      items.length ? h('ol', { class: 'q-list' }, ...items.map((it, i) => itemRow(it, i, items, sugg))) : h('p', { class: 'empty' }, 'The queue is empty.')));
  }

  function open() {
    if (!dlg) {
      dlg = h('dialog', { id: 'queuedlg', class: 'wide queuedlg', 'aria-labelledby': 'q-title' });
      dlg.addEventListener('close', () => { clearInterval(timer); timer = null; });
      document.body.append(dlg);
    }
    dlg.replaceChildren(h('p', { class: 'empty' }, 'Reading the queue...'));
    dlg.showModal();
    refresh();
    clearInterval(timer);
    timer = setInterval(refresh, 4000);
  }

  $('#queue').addEventListener('click', open);
  $('#enqueue').addEventListener('click', addDialog);
  refresh();
  setInterval(() => { if (!dlg?.open) refresh(); }, 30000);    // the count on the button
  window.XPQueue = { open, refresh };
})();
