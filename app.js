/* Athanor page. Depends on editors.js ($, h, api, status, openDialog, slug). */
let cfg = null;                                   // config from the server (normalized)
let st = { xp: null, blocks: {}, loop: null, values: {} };   // selection: option per block, command loop, edited values (saved per XP)
let table = {};                                   // effective variables from the last render: name -> {value, source, scope, ...}
let loopInfo = null;                              // from the last render: {loop, loop_vars, has_loop_env, undefined}
let checks = {};                                  // from the last preview: name -> {level, notes}
const closedGroups = new Set(['docker']);   // the docker group is long and rarely edited

const GROUPS = [
  ['docker', 'Docker / generic', 'script variables; tick "container" in a variable to pass it to the container (-e)'],
  ['run', 'Run', 'the command loop and its variables'],
  ['process', 'Process (APP_*)', 'environment of the program (jsonargparse), always passed to the container'],
];

const xpDef = () => cfg.xps.find((x) => x.name === st.xp);
const blk = (bid) => xpDef()?.blocks.find((b) => b.id === bid);                       // block of the current XP
const blkIn = (c, bid) => c.xps.find((x) => x.name === st.xp).blocks.find((b) => b.id === bid);   // same, in a copy
const prefix = () => cfg.project.env_prefix;
const fullName = (b, k) => (!b.path || k.startsWith(prefix()) ? k : b.path + k);
const clone = (o) => JSON.parse(JSON.stringify(o));

/* ---------- the variables of an XP (core.xp_variables): the shared ones (cfg.variables) without the ones it removed,
   with its edits, then its own. xp: '' shared, 'edited' a shared one edited for this XP, 'own' of this XP only. ---------- */

const VAR_FIELDS = ['label', 'value', 'options', 'env'];

function xpVarsOf(c, xpName) {
  const own = c.xps.find((x) => x.name === xpName)?.variables || {};
  const out = {};
  for (const [g] of GROUPS) {
    const lst = [];
    for (const v of c.variables[g]) {
      const o = own[v.name];
      if (o?.removed) continue;
      lst.push(o ? { ...v, ...Object.fromEntries(VAR_FIELDS.filter((k) => k in o).map((k) => [k, o[k]])), pretty: o.pretty ?? (('value' in o) ? o.value : v.pretty), xp: 'edited' } : { ...v, xp: '' });
    }
    for (const [n, o] of Object.entries(own)) if (!o.removed && o.group === g) lst.push({ name: n, label: o.label, value: o.value, options: o.options, env: o.env, pretty: o.pretty ?? o.value, xp: 'own' });
    out[g] = lst;
  }
  return out;
}
const xpVars = (g) => (st.xp ? xpVarsOf(cfg, st.xp)[g] : cfg.variables[g]);
const sharedOf = (c, name) => {
  for (const [g] of GROUPS) { const v = c.variables[g].find((x) => x.name === name); if (v) return { g, v }; }
  return null;
};

/* set a variable for XP `xpName` only (in the copy `c`): the fields that differ from the shared one, or a variable of
   its own (group needed) */
function setXpVar(c, xpName, name, fields, group) {
  const x = c.xps.find((y) => y.name === xpName);
  x.variables = x.variables || {};
  const sh = sharedOf(c, name);
  if (sh) {
    const diff = Object.fromEntries(VAR_FIELDS.filter((k) => k in fields && JSON.stringify(fields[k]) !== JSON.stringify(sh.v[k])).map((k) => [k, fields[k]]));
    if (Object.keys(diff).length) x.variables[name] = diff; else delete x.variables[name];
  } else {
    const old = x.variables[name] || {};
    x.variables[name] = { group: group || old.group || (name.startsWith(prefix()) ? 'process' : 'run'), label: '', value: '', options: [], env: false,
      ...Object.fromEntries(VAR_FIELDS.filter((k) => k in old).map((k) => [k, old[k]])), ...fields };
  }
  if (!Object.keys(x.variables).length) delete x.variables;
}

/* remove a variable from XP `xpName` only: a shared one is marked removed there, one of its own is deleted */
function removeXpVar(c, xpName, name) {
  const x = c.xps.find((y) => y.name === xpName);
  x.variables = x.variables || {};
  if (sharedOf(c, name)) x.variables[name] = { removed: true }; else delete x.variables[name];
  if (x.values) delete x.values[name];
  if (!Object.keys(x.variables).length) delete x.variables;
}

/* ---------- the edited values, saved per XP (xps[].values) ---------- */

let valuesFor = null;                             // the XP st.values belongs to
let valuesTimer = null;

function sameValues(a, b) { return JSON.stringify(Object.entries(a || {}).sort()) === JSON.stringify(Object.entries(b || {}).sort()); }

function scheduleSaveValues() {
  clearTimeout(valuesTimer);
  valuesTimer = setTimeout(saveValues, 700);
}

async function saveValues(xpName = st.xp, values = st.values) {
  const xp = cfg.xps.find((y) => y.name === xpName);
  if (!xp || (xpName === st.xp && valuesFor !== st.xp) || sameValues(xp.values, values)) return;
  const next = clone(cfg);
  const x = next.xps.find((y) => y.name === xpName);
  if (Object.keys(values).length) x.values = { ...values }; else delete x.values;
  try { cfg = await api('/api/config', next, 'PUT'); status(`Saved the values of ${xpName}`); } catch (e) { status(e.message, true); }
}

/* ---------- loading and saving ---------- */

function syncSelection() {
  if (!cfg.xps.some((x) => x.name === st.xp)) st.xp = cfg.xps[0]?.name ?? null;
  const blocks = {};
  for (const { id, rows } of xpDef()?.blocks || []) {
    const c = st.blocks[id];
    if (Array.isArray(c)) {                                   // values selected in a list block
      const ok = c.filter((n) => rows.some((r) => r.name === n && !r.loop_all));
      blocks[id] = ok.length ? ok : rows[0].name;
    } else blocks[id] = rows.some((r) => r.name === c) ? c : rows[0].name;
  }
  st.blocks = blocks;
  const loops = cfg.loop.options;
  if (!loops.some((r) => r.name === st.loop)) st.loop = loops[0]?.name ?? null;
  if (valuesFor !== st.xp) {                      // another XP: save the edits of the previous one, show its saved ones
    if (valuesFor) { clearTimeout(valuesTimer); saveValues(valuesFor, { ...st.values }); }
    st.values = { ...(xpDef()?.values || {}) };
    valuesFor = st.xp;
  }
  const known = new Set(st.xp ? Object.values(xpVarsOf(cfg, st.xp)).flat().map((v) => v.name) : []);
  for (const k of Object.keys(st.values)) if (!known.has(k)) delete st.values[k];
}

async function load() {
  try { cfg = await api('/api/config'); } catch (e) { status(e.message, true); return; }
  syncSelection();
  draw();
  window.dispatchEvent(new Event('xp-loaded'));       // router.js: apply the address of the page
}

// Apply `mutate` to a copy of the config and save it (the server validates). On failure nothing changes.
async function commit(mutate, { after, deleted, done } = {}) {
  const before = clone(cfg);
  const next = clone(cfg);
  mutate(next);
  const x = next.xps.find((y) => y.name === st.xp);
  if (x && valuesFor === st.xp) { if (Object.keys(st.values).length) x.values = { ...st.values }; else delete x.values; }
  cfg = await api('/api/config', next, 'PUT');
  if (after) after();
  syncSelection();
  draw();
  scheduleSaveValues();                           // `after` may have changed the edited values
  if (deleted || done) {
    status(deleted ? `Deleted ${deleted}` : done, false, { label: 'Undo', fn: async () => {
      try { cfg = await api('/api/config', before, 'PUT'); syncSelection(); draw(); status('Restored'); } catch (e) { status(e.message, true); }
    } });
  } else status('Saved');
}

/* ---------- XPs ---------- */

function editXp(oldName) {
  const fields = [{ key: 'name', label: 'XP name', value: oldName ?? '' }];
  if (oldName == null && st.xp) fields.push({ key: 'copy', label: `Start with a copy of the blocks of "${st.xp}"`, type: 'check', value: true });
  openDialog({
    title: oldName == null ? 'New XP' : `Rename "${oldName}"`,
    fields,
    onSubmit: async (v) => {
      const name = v.name.trim();
      if (!name) throw new Error('Name is required');
      const from = st.xp;
      await commit((c) => {
        if (oldName == null) c.xps.push({ name, blocks: v.copy && from ? clone(c.xps.find((x) => x.name === from).blocks) : [] });
        else c.xps.find((x) => x.name === oldName).name = name;
      }, { after: () => { st.xp = name; } });
    },
  });
}

const deleteXp = (name) => commit((c) => { c.xps = c.xps.filter((x) => x.name !== name); }, { deleted: `XP "${name}"` }).catch((e) => status(e.message, true));

/* ---------- blocks and options ---------- */

/* What the program does with each variable of an option being edited (ignored, rejected, what it replaces),
   shown under each row of the dialog. `box` is the pairs field. */
const LEVEL_TEXT = { ok: 'OK', info: 'OK', warn: 'CHECK', error: 'PROBLEM' };

function showChecks(box, bid, r) {
  const b = blk(bid);
  for (const { key, el, status } of box._rows()) {
    const res = key ? r.vars[fullName(b, key)] : null;
    status.classList.remove('pending');
    if (!res) {
      el.replaceChildren(); el.className = 'pair-check'; el.hidden = true;
      status.className = 'pair-status'; status.textContent = ''; status.disabled = true; status.title = key ? 'Not checked' : '';
      continue;
    }
    status.className = 'pair-status ' + res.level;
    status.textContent = LEVEL_TEXT[res.level] + (res.fix ? ' *' : '');
    status.disabled = false;
    status.title = (res.notes.find((n) => n.level === res.level) || res.notes[0])?.text || 'Nothing to report';
    el.className = 'pair-check ' + res.level;
    const kids = res.notes.length ? res.notes.map((n) => h('span', { class: n.level }, n.text)) : [h('span', { class: 'info' }, 'Nothing to report.')];
    if (res.fix) {
      kids.push(h('button', { type: 'button', class: 'tiny', title: res.fix.value, onclick: () => {
        const drop = new Set([...res.fix.drop, fullName(b, key)]);
        const pairs = box._get().filter(([k]) => !drop.has(fullName(b, k)));
        const rel = (n) => (b.path && n.startsWith(b.path) && n.length > b.path.length ? n.slice(b.path.length) : n);
        const at = pairs.findIndex(([k]) => fullName(b, k) === res.fix.name);
        if (at >= 0) pairs[at][1] = res.fix.value; else pairs.push([rel(res.fix.name), res.fix.value]);
        box._set(pairs);
      } }, 'Fix: ' + res.fix.label));
    }
    el.replaceChildren(...kids);
  }
  const extra = box.querySelector('.pairs-missing') || h('div', { class: 'pairs-missing' });
  extra.replaceChildren(...r.missing.map((m) => h('p', { class: 'pk-warn' }, `${m.cls} (${m.path}) needs ${m.args.join(', ')}: no default and nothing sets it`)),
    ...r.notes.map((t) => h('p', { class: 'pk-warn' }, t)));
  box.querySelector('.pairs-bar').before(extra);
}

function checker(bid, name) {
  let timer = null, seq = 0;
  return (box) => {
    clearTimeout(timer);
    for (const { status } of box._rows()) status.classList.add('pending');
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const r = await api('/api/check', { sel: st, draft: { block: bid, row: name, vars: box._get() } });
        if (mine === seq && box.isConnected) showChecks(box, bid, r);
      } catch (e) {
        if (mine !== seq || !box.isConnected) return;
        for (const { el, status } of box._rows()) {
          el.replaceChildren(); el.className = 'pair-check'; el.hidden = true;
          status.className = 'pair-status'; status.textContent = '?'; status.disabled = true; status.title = 'Cannot check: ' + e.message;
        }
        const extra = box.querySelector('.pairs-missing') || h('div', { class: 'pairs-missing' });
        extra.replaceChildren(h('p', { class: 'pk-warn' }, 'Cannot check: ' + e.message));
        box.querySelector('.pairs-bar').before(extra);
      }
    }, 250);
  };
}

function editOption(bid, name) {
  const b = blk(bid);
  if (b.kind === 'list') { editListBlock(bid); return; }      // its options come from the list
  const row = name == null ? null : b.rows.find((r) => r.name === name);
  const onChange = checker(bid, name);
  openDialog({
    title: name == null ? `Add option to "${b.label}"` : `Edit "${name}"`,
    wide: true,
    fields: [
      { key: 'name', label: 'Name', value: row?.name ?? '' },
      { key: 'vars', type: 'pairs', label: 'Variables set by this option', value: Object.entries(row?.vars ?? {}).map(([k, x]) => [k, row.pretty?.[k] ?? x]),
        keyPlaceholder: b.path ? 'name after the block path' : `${prefix()}... or a script variable`,
        resolve: (k) => (fullName(b, k) !== k ? '-> ' + fullName(b, k) : ''),
        onChange,
        actions: [{ label: 'Format as YAML', title: 'Show dicts and lists written on one line as indented YAML (same value)',
          onclick: async (el) => {
            const pairs = el._get();
            try { const r = await api('/api/yaml', { values: pairs.map(([, x]) => x) }); el._set(pairs.map(([k], i) => [k, r.values[i]])); } catch (e) { status(e.message, true); }
          } }, { label: 'Browse arguments', title: 'Pick the program arguments from the classes in src/; they are added here',
          onclick: (el) => window.argPicker.open({ block: bid, row: name, get: () => el._get(), set: (pairs) => el._set(pairs),
            label: () => el.closest('form')?.querySelector('.dfield input')?.value.trim() }) }],
        hint: (b.path ? `Block path: ${b.path}. A name that starts with ${prefix()} is used as it is. `
          : `Full names: ${prefix()}... for the program, or any script variable (e.g. PERC_20K). $UPPER_CASE in a value is expanded by the script. `)
          + 'Each variable is checked against the base config and the classes in src/ with the other blocks as selected.' },
    ],
    onSubmit: async (v) => {
      const nm = v.name.trim();
      if (!nm) throw new Error('Name is required');
      await commit((c) => {
        const rows = blkIn(c, bid).rows;
        const nr = { name: nm, vars: Object.fromEntries(v.vars) };
        if (name == null) rows.push(nr); else rows[rows.findIndex((r) => r.name === name)] = nr;
      }, { after: () => { st.blocks[bid] = nm; } });
    },
    remove: name == null ? null : async () => {
      await commit((c) => { const rows = blkIn(c, bid).rows; rows.splice(rows.findIndex((r) => r.name === name), 1); }, { deleted: `option "${name}"` });
    },
  });
  const el = document.querySelector('#dlgform .pairs');
  if (el) onChange(el);
}

function editBlock(bid) {
  const b = blk(bid);
  if (b.kind === 'list') { editListBlock(bid); return; }
  openDialog({
    title: `Block "${b.label}" of "${st.xp}"`,
    fields: [
      { key: 'label', label: 'Name', value: b.label },
      { key: 'path', label: 'Path (environment variable prefix, optional)', value: b.path, mono: true,
        placeholder: `${prefix()}TASK__INIT_ARGS__MODEL__INIT_ARGS__EXTRACTOR__INIT_ARGS__LLM__`,
        hint: 'Variables of its options are relative to this path. Leave empty to use full names.' },
      { type: 'note', label: `This block belongs to "${st.xp}" only: other XPs have their own copies.` },
      ...(xpDef().blocks.length > 1 ? [{ key: 'merge', type: 'select', label: 'Merge with another block (one option per combination; delete the ones you do not need)', value: '',
        options: [{ value: '', label: '- no -' }, ...xpDef().blocks.filter((x) => x.id !== bid).map((x) => ({ value: x.id, label: `${x.label} (${x.rows.length} options)` }))] }] : []),
    ],
    onSubmit: async (v) => {
      if (!v.label.trim()) throw new Error('Name is required');
      await commit((c) => { const x = blkIn(c, bid); x.label = v.label.trim(); x.path = v.path.trim(); });
      if (v.merge) {
        const before = clone(cfg);
        cfg = (await api('/api/blocks/merge', { xp: st.xp, first: bid, second: v.merge })).config;
        syncSelection(); draw();
        status(`Merged into "${blk(bid).label}"`, false, { label: 'Undo', fn: async () => { cfg = await api('/api/config', before, 'PUT'); syncSelection(); draw(); status('Restored'); } });
      }
    },
    remove: () => removeBlock(bid),
  });
}

/* A list block: a variable and its values. Its options are generated: one per value, and "all (N)" that runs the
   command loop once per value ($VAR becomes the loop variable ${var}). Variables built from it use $VAR. */
function editListBlock(bid, init = {}) {
  const b = bid ? blk(bid) : { label: init.label || '', path: init.path || '', var: 'RUN_ID', items: [], vars: {}, loop: true };
  const lines = (b.items || []).map((it) => (it.label ? `${it.value}  # ${it.label}` : it.value)).join('\n');
  const v0 = b.var || 'RUN_ID';
  openDialog({
    title: bid ? `List "${b.label}" of "${st.xp}"` : `New list block in "${st.xp}"`,
    wide: true,
    fields: [
      { key: 'label', label: 'Name', value: b.label },
      { key: 'var', label: 'Variable (each option sets it to one value)', value: b.var, mono: true, placeholder: 'RUN_ID',
        hint: 'A script variable (e.g. RUN_ID), or a program argument (APP_...). Other variables use it as $' + v0 + '.' },
      { key: 'items', type: 'area', rows: 8, mono: true, label: 'Values, one per line (optional label after #)', value: lines,
        hint: 'e.g.  361d59187e3c46bea1c5a9409470f0a8  # polite-dog-639. No spaces or quotes in a value.' },
      { key: 'vars', type: 'pairs', label: 'Variables built from it (set by every option of the list)', value: Object.entries(b.vars || {}),
        keyPlaceholder: prefix() + 'TASK__INIT_ARGS__CHECKPOINT_PATH',
        hint: 'Use $' + v0 + ' in the values, e.g. /data/checkpoints/1/$' + v0 + '/last.ckpt' },
      { key: 'loop', type: 'check', label: 'Offer "all (N)": run the command loop once per value', value: b.loop !== false },
      { key: 'path', label: 'Path (environment variable prefix, optional)', value: b.path, mono: true },
      { type: 'note', label: 'Picking one value sets the variable like an option (the Results tab tells the runs apart by it). '
        + '"all (N)" wraps the command loop in  for <var> in <values>; do ... done  and every $VAR becomes the loop variable, set inside the loop for each run.' },
    ],
    submitLabel: bid ? 'Save' : 'Add',
    onSubmit: async (v) => {
      if (!v.label.trim()) throw new Error('Name is required');
      const items = v.items.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
        const [value, ...rest] = l.split('#');
        const label = rest.join('#').trim();
        return { value: value.trim(), ...(label ? { label } : {}) };
      });
      if (!items.length) throw new Error('Give at least one value');
      const def = { label: v.label.trim(), path: v.path.trim(), kind: 'list', var: v.var.trim(), items,
        vars: Object.fromEntries(v.vars.filter(([k]) => k.trim())), loop: !!v.loop };
      await commit((c) => {
        const xp = c.xps.find((x) => x.name === st.xp);
        if (bid) Object.assign(blkIn(c, bid), def);
        else xp.blocks.push({ id: uniqueId(xp.blocks, def.label), ...def, rows: [] });
      });
    },
    remove: bid ? () => removeBlock(bid) : null,
  });
}

const uniqueId = (blocks, base) => {
  let id = slug(base), i = 2;
  while (blocks.some((b) => b.id === id)) id = `${slug(base)}_${i++}`;
  return id;
};

/* A new empty block, or a copy of blocks from any XP (a copy: later changes do not touch the original). */
function addBlock() {
  const sources = [];
  cfg.xps.forEach((x, xi) => {
    if (!x.blocks.length) return;
    if (x.name !== st.xp && x.blocks.length > 1) sources.push({ value: `${xi}:*`, label: `${x.name}: all ${x.blocks.length} blocks` });
    x.blocks.forEach((b) => sources.push({ value: `${xi}:${b.id}`, label: `${x.name} / ${b.label}  (${b.rows.length} option${b.rows.length === 1 ? '' : 's'})` }));
  });
  const fields = [];
  if (sources.length) {
    fields.push({ key: 'source', type: 'select', label: 'Copy from an XP', value: '',
      options: [{ value: '', label: 'No: new empty block' }, ...sources],
      hint: 'The copy belongs to this XP: editing it does not change the original.' });
  }
  fields.push({ key: 'kind', type: 'select', label: 'New block', value: 'options', options: [
    { value: 'options', label: 'options: each sets its own variables' },
    { value: 'list', label: 'a list of values (run ids...): pick one, or loop over all of them' }] },
  { key: 'label', label: 'Name (new block, or to rename the copy)', value: '', placeholder: 'XP variant' },
    { key: 'path', label: 'New block: path (optional)', value: '', mono: true, placeholder: `${prefix()}TASK__INIT_ARGS__`,
      hint: 'Environment variable prefix; variables of its options are relative to it.' });
  openDialog({
    title: `Add block to "${st.xp}"`,
    fields,
    submitLabel: 'Add',
    onSubmit: async (v) => {
      if (!v.source && !v.label.trim()) throw new Error('Name is required');
      if (!v.source && v.kind === 'list') { setTimeout(() => editListBlock(null, { label: v.label.trim(), path: v.path.trim() }), 0); return; }
      await commit((c) => {
        const xp = c.xps.find((x) => x.name === st.xp);
        if (!v.source) {
          xp.blocks.push({ id: uniqueId(xp.blocks, v.label), label: v.label.trim(), path: v.path.trim(), rows: [{ name: 'none', vars: {} }] });
          return;
        }
        const [xi, bid] = [Number(v.source.split(':')[0]), v.source.slice(v.source.indexOf(':') + 1)];
        const from = c.xps[xi].blocks;
        for (const b of bid === '*' ? from : from.filter((x) => x.id === bid)) {
          const copy = clone(b);
          if (bid !== '*' && v.label.trim()) copy.label = v.label.trim();
          copy.id = uniqueId(xp.blocks, copy.label);
          xp.blocks.push(copy);
        }
      });
    },
  });
}

const removeBlock = (bid) => commit((c) => {
  const xp = c.xps.find((x) => x.name === st.xp);
  xp.blocks = xp.blocks.filter((b) => b.id !== bid);
}, { deleted: `block "${blk(bid).label}" from "${st.xp}"` }).catch((e) => status(e.message, true));

function editLoop(name) {
  const row = name == null ? null : cfg.loop.options.find((r) => r.name === name);
  openDialog({
    title: name == null ? 'Add a command loop' : `Command loop "${name}"`,
    wide: true,
    fields: [
      { key: 'name', label: 'Name', value: row?.name ?? '' },
      { key: 'value', type: 'area', rows: 16, mono: true, label: 'Shell text (runs inside CMD="...")', value: row?.value ?? '',
        hint: 'Loop variables: for window in ... defines $window (write \\$window: it is evaluated in the container). '
          + '$UPPER_CASE is expanded by the script. Put @@LOOP_ENV@@ on its own line before the command: it is replaced by '
          + 'the variables set inside the loop (chosen in the Run section).' },
    ],
    onSubmit: async (v) => {
      const nm = v.name.trim();
      if (!nm) throw new Error('Name is required');
      await commit((c) => {
        const rows = c.loop.options;
        const nr = { name: nm, value: v.value };
        if (name == null) rows.push(nr); else rows[rows.findIndex((r) => r.name === name)] = nr;
      }, { after: () => { st.loop = nm; } });
    },
    remove: name == null ? null : async () => {
      await commit((c) => { c.loop.options = c.loop.options.filter((r) => r.name !== name); }, { deleted: `command loop "${name}"` });
    },
  });
}

const setLoopEnv = (mutate) => commit((c) => { mutate(c.loop.env); }).catch((e) => status(e.message, true));

/* Run section, top: the command loop, the variables it defines, and what is set inside it (@@LOOP_ENV@@). */
function drawLoopPanel() {
  const box = $('#looppanel');
  if (!box) return;
  const cur = cfg.loop.options.find((r) => r.name === st.loop);
  const pills = cfg.loop.options.map((r) => h('button', { type: 'button', class: 'pill', 'aria-pressed': String(r.name === st.loop),
    title: r.value.trim(), onclick: () => { st.loop = r.name; draw(); } }, r.name));
  const kids = [h('div', { class: 'lp-row' }, h('span', { class: 'lp-key' }, 'Command loop'), ...pills,
    cur ? h('button', { type: 'button', class: 'tiny', onclick: () => editLoop(cur.name) }, 'Edit') : '',
    h('button', { type: 'button', class: 'tiny', onclick: () => editLoop(null) }, '+ Add'))];
  const info = loopInfo && loopInfo.loop === st.loop ? loopInfo : null;
  if (cur) {
    // the loop as it runs for this selection (lists looped over around it, @@LOOP_ENV@@ filled), or as written
    const lists = info?.lists || [];
    const heads = new Set(lists.map((l) => 'for ' + l.loop_var + ' in '));
    const effective = (info?.effective || cur.value.trim()).split('\n');
    let depth = 0, inHead = false;
    const lines = effective.map((ln, i) => {
      const t = ln.trim();
      const indent = ln.length - ln.trimStart().length;
      let cls = '';
      if (inHead) { cls = 'lp-list lp-val'; if (t.endsWith('; do')) inHead = false; }
      else if (t.startsWith('# list ')) cls = 'lp-list';
      else if ([...heads].some((x) => t.startsWith(x)) && indent === depth * 4) { cls = 'lp-list'; depth += 1; inHead = !t.endsWith('; do'); }
      else if (t === 'done' && depth && indent === (depth - 1) * 4 && effective.slice(i + 1).every((x) => x.trim() === 'done')) { cls = 'lp-list'; depth -= 1; }
      else if (/^[A-Z_][A-Z0-9_]*=".*" \\$/.test(t)) cls = 'lp-env';
      return h('span', { class: 'lp-line ' + cls }, ln + '\n');
    });
    const shown = h('pre', { class: 'lp-pre' }, ...lines);
    const written = h('pre', { class: 'lp-pre', hidden: '' }, cur.value.trim());
    const pick = (k) => (ev) => {
      shown.hidden = k !== 'run';
      written.hidden = k === 'run';
      ev.currentTarget.parentNode.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(b === ev.currentTarget)));
    };
    const toggle = h('span', { class: 'seg lp-seg' },
      h('button', { type: 'button', 'aria-pressed': 'true', onclick: pick('run') }, 'as it runs'),
      h('button', { type: 'button', 'aria-pressed': 'false', onclick: pick('written') }, 'as written'));
    const what = lists.map((l) => 'the list "' + l.block + '" (' + l.values + ' values)').join(', ');
    const legend = h('span', { class: 'hint lp-legend' },
      lists.length ? h('span', { class: 'lp-list-k' }, 'loop over ' + what + ', around the command loop') : '',
      h('span', { class: 'lp-env-k' }, 'set at @@LOOP_ENV@@'));
    const title = 'show the loop' + (lists.length ? ' (with ' + (lists.length === 1 ? 'a list' : lists.length + ' lists') + ' looped over)' : '');
    kids.push(h('details', { class: 'lp-text' }, h('summary', {}, title), h('div', { class: 'lp-bar' }, toggle, legend), shown, written));
  }
  kids.push(h('div', { class: 'lp-row' }, h('span', { class: 'lp-key', title: 'Defined by "for x in ..." in the loop; evaluated in the container' }, 'Loop variables'),
    ...(info ? (info.loop_vars.length ? info.loop_vars.map((n) => {
      const l = (info.lists || []).find((x) => x.loop_var === n);
      return h('code', { class: 'scope-chip loopvar' + (l ? ' fromlist' : ''),
        title: l ? 'from the list "' + l.block + '": ' + l.values + ' values ($' + l.var + ' in the variables)' : 'defined by the command loop' },
      '$' + n, l ? h('small', {}, ' list "' + l.block + '" x' + l.values) : '');
    }) : [h('span', { class: 'hint' }, 'none')]) : [h('span', { class: 'hint' }, '...')])));
  for (const u of info?.undefined || []) kids.push(h('p', { class: 'pk-warn' }, `$${u.var} is used by ${u.used_by.join(', ')} but this loop does not define it`));

  // @@LOOP_ENV@@: automatic ones (value uses a loop variable) + the ones chosen in loop.env
  const inLoop = Object.values(table).filter((e) => e.loop);
  const chosen = new Set(cfg.loop.env);
  const chips = inLoop.map((e) => {
    const auto = !chosen.has(e.name);
    return h('span', { class: 'scope-chip loop', title: `${e.name}="${e.value}"  (${e.source})` }, short2(e.name),
      auto ? h('small', {}, 'uses a loop variable')
        : h('button', { type: 'button', class: 'x', 'aria-label': `Take ${e.name} out of the loop`,
          onclick: () => setLoopEnv((l) => l.splice(l.indexOf(e.name), 1)) }, 'x'));
  });
  for (const n of cfg.loop.env) if (!table[n]) {
    chips.push(h('span', { class: 'scope-chip loop missing', title: 'No variable of this XP has this name' }, n, h('small', {}, 'not defined'),
      h('button', { type: 'button', class: 'x', 'aria-label': `Remove ${n}`, onclick: () => setLoopEnv((l) => l.splice(l.indexOf(n), 1)) }, 'x')));
  }
  const candidates = Object.values(table).filter((e) => e.scope === 'script' && e.name !== 'XP');
  const add = h('select', { class: 'lp-add', title: 'Script variables; they become environment variables of each run' },
    h('option', { value: '' }, '+ set a script variable inside the loop...'),
    ...candidates.map((e) => h('option', { value: e.name }, `${e.name} = ${e.value.length > 40 ? e.value.slice(0, 37) + '...' : e.value}`)));
  add.addEventListener('change', () => { if (add.value) setLoopEnv((l) => l.push(add.value)); });
  kids.push(h('div', { class: 'lp-row' }, h('span', { class: 'lp-key', title: 'Replaced by NAME="value" lines, set for each run of the command' }, 'Inside the loop', h('code', {}, '@@LOOP_ENV@@')),
    ...(chips.length ? chips : [h('span', { class: 'hint' }, 'nothing')]), add));
  if (info && !info.has_loop_env && inLoop.length) kids.push(h('p', { class: 'pk-warn' }, `"${st.loop}" has no @@LOOP_ENV@@ line: add one before the command (Edit).`));
  box.replaceChildren(...kids);
}

/* readable short name: APP_TASK__INIT_ARGS__MODEL__INIT_ARGS__LR -> task.model.lr */
function short2(name) {
  const p = prefix();
  if (!name.startsWith(p)) return name;
  return name.slice(p.length).split(cfg.project.env_sep).filter((s) => s && s !== 'INIT_ARGS').join('.').toLowerCase();
}

const SCOPE_TEXT = { script: 'script', container: '-e', loop: 'loop' };
const SCOPE_TITLE = { script: 'script variable: not passed to the container', container: 'passed to the container with -e',
  loop: 'set inside the loop, for each run (@@LOOP_ENV@@)' };

/* Under each block: the variables its selected option sets, by scope, with the checker's verdict. */
function blockVars(b) {
  const rowName = st.blocks[b.id];
  const row = b.rows.find((r) => r.name === rowName);
  const mine = `${b.label}: ${rowName}`;
  const entries = Object.entries(row?.vars || {});
  if (!entries.length) return [h('p', { class: 'hint' }, 'sets nothing')];
  return entries.map(([k, raw]) => {
    const full = fullName(b, k), e = table[full], c = checks[full];
    const over = e && e.source !== mine;
    const scope = e?.scope || (full.startsWith(prefix()) ? 'container' : 'script');
    const bad = !over && c && (c.level === 'error' || c.level === 'warn') ? c.level : '';
    const shown = b.path && full.startsWith(b.path) && full !== b.path ? full.slice(b.path.length).split(cfg.project.env_sep).filter((x) => x && x !== 'INIT_ARGS').join('.').toLowerCase() : short2(full);
    const flat = raw.replace(/\s+/g, ' ').trim();
    const el = h('div', { class: `bv ${scope} ${bad}` + (over ? ' over' : ''), draggable: 'true',
      title: `${full}=${raw}\n${SCOPE_TITLE[scope]}` + (over ? `\nOverridden by ${e.source}` : '') + (c?.notes?.length && !over ? '\n\n' + c.notes.join('\n') : '')
        + '\n\nDrag onto an option of any block to copy it there (hold Shift to move it).' },
      h('small', { class: 'bv-scope' }, SCOPE_TEXT[scope]), h('b', {}, shown), h('code', {}, flat),
      over ? h('small', { class: 'flag' }, 'overridden') : bad ? h('small', { class: 'flag' }, bad === 'error' ? 'ignored / rejected' : 'check') : '');
    el.addEventListener('dragstart', (ev) => {
      dragged = { bid: b.id, row: rowName, full, value: raw };
      ev.dataTransfer.setData('text/plain', `${full}=${raw}`);
      ev.dataTransfer.effectAllowed = 'copyMove';
      document.body.classList.add('dragging-var');
    });
    el.addEventListener('dragend', () => { dragged = null; document.body.classList.remove('dragging-var'); clearDropMarks(); });
    return el;
  });
}

function drawBlockVars() {
  for (const b of xpDef()?.blocks || []) {
    const box = document.querySelector(`.card-vars[data-bid="${CSS.escape(b.id)}"]`);
    if (box) box.replaceChildren(...blockVars(b));
  }
}

/* ---------- base configs written by the launcher, and XPs imported from MLflow ---------- */

// base configs written by the launcher: <scripts_dir>/<XP>/configs/<xp>.yaml, next to the XP's scripts (as core.py)
const xpDir = (name) => `${cfg.project.scripts_dir.replace(/\/+$/, '')}/${String(name).replace(/[^\w.\-]+/g, '_').replace(/^[._]+|[._]+$/g, '') || 'xp'}`;
const genDir = (name) => `${xpDir(name)}/configs`;
const isGenerated = (path) => {
  const sd = cfg.project.scripts_dir.replace(/\/+$/, '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const p = String(path || '').trim().replace(/^\.\//, '');
  return !p.split('/').includes('..') && new RegExp(`^${sd}/[^/]+/configs/[^/]+$`).test(p);
};

function newConfig(current) {
  openDialog({
    title: `New base config for "${st.xp}"`,
    fields: [
      { key: 'source', type: 'select', label: 'Start from', value: current ? 'copy' : 'empty',
        options: [{ value: 'copy', label: `a copy of ${current || 'the current config'}` }, { value: 'empty', label: 'an empty config (task + settings)' }] },
      { type: 'note', label: `Written to ${genDir(st.xp)}/${slug(st.xp)}.yaml, outside git: the script copies it into the container and puts it back after the checkout of the commit, so it works whatever the commit. `
        + 'A "Base config" block of this XP selects it; relative subconfig paths are rewritten for the new place.' },
    ],
    submitLabel: 'Create',
    onSubmit: async (v) => {
      const r = await api('/api/xpconfig/create', { sel: st, source: v.source, from: current });
      cfg = r.config; syncSelection(); draw();
      status(`Wrote ${r.path}`);
      setTimeout(() => editConfigFile(r.path), 0);
    },
  });
}

async function editConfigFile(path) {
  if (!isGenerated(path)) { status(`${path} is a file of the repository (taken at the commit): use New... to make an editable copy`, true); return; }
  let text;
  try { text = (await api('/api/xpconfig/read', { path })).text; } catch (e) { status(e.message, true); return; }
  openDialog({
    title: path,
    wide: true,
    fields: [
      { key: 'text', type: 'area', rows: 30, mono: true, label: 'YAML (the variables of the blocks are applied on top of it; the preview shows the result)', value: text },
    ],
    onSubmit: async (v) => { await api('/api/xpconfig/write', { path, text: v.text }); status(`Saved ${path}`); schedulePreview(); },
  });
}

async function importXp() {
  let list;
  try { list = (await api('/api/mlflow/experiments')).experiments; } catch (e) { status(e.message, true); return; }
  if (!list.length) { status('No MLflow experiment found', true); return; }
  openDialog({
    title: 'Import an XP from MLflow',
    fields: [
      { key: 'experiment', type: 'select', label: 'MLflow experiment', value: list[0].name, options: list.map((e) => ({ value: e.name, label: e.name })) },
      { key: 'xp', label: 'XP name (empty: the name of the experiment)', value: '' },
      { type: 'note', label: 'Reads the config.yaml of every finished run. The base config is the first run\'s (written to ' + cfg.project.scripts_dir + '/<XP>/configs); '
        + 'what differs between the runs becomes blocks, one per thing that changes (a value, a list, or a whole object when its class changes), and the commits a "Commit" block. '
        + 'If an XP of that name exists, only the values it does not have yet are added (sync). Merge blocks from their Block dialog, delete the ones you do not need.' },
    ],
    submitLabel: 'Import',
    onSubmit: async (v) => {
      status('Reading the runs...');
      const r = await api('/api/import', { experiment: v.experiment, xp: v.xp });
      cfg = r.config; st.xp = r.summary.xp; syncSelection(); draw();
      const s2 = r.summary;
      status(s2.created ? `Imported ${s2.runs} runs into "${s2.xp}": ${s2.blocks} blocks, ${s2.options} options (base config ${s2.config})`
        : `Synced "${s2.xp}" with ${s2.runs} runs: ${s2.blocks} new blocks, ${s2.options} new options`);
    },
  });
}

$('#newxp').after(h('button', { class: 'ghost wide', id: 'importxp', type: 'button', title: 'A new XP from the runs of an MLflow experiment (or the new values of its runs)', onclick: importXp }, 'Import from MLflow...'));

/* the commit and base-config variables get a chooser */
function pickCommit(name, input) {
  openChooser({
    title: 'Commit',
    current: input.value.trim(),
    load: async () => {
      const r = await api('/api/commits?n=80');
      return { note: 'All local branches, newest first. The script copies .git into the container and checks the commit out'
          + (r.dirty ? '; uncommitted changes in the working tree are not part of any commit.' : '.'),
        items: r.commits.map((c) => ({ value: c.hash, label: `${c.short}  ${c.subject}`, sub: `${c.date} - ${c.author}`, tags: c.refs, mark: c.head ? 'HEAD' : '' })) };
    },
    onPick: (v) => setField(name, input, v),
  });
}

function pickConfig(name, input) {
  openChooser({
    title: 'Base config',
    current: input.value.trim(),
    load: async () => {
      const r = await api('/api/files', st);
      const files = [...r.files].sort((a, b) => (a.includes('/subconfigs/') - b.includes('/subconfigs/')) || a.localeCompare(b));
      return { note: `configs/ at ${r.source}. Subconfigs are listed last.`,
        items: files.map((f) => ({ value: f, label: f.split('/').pop(), group: f.split('/').slice(0, -1).join('/') })) };
    },
    onPick: (v) => setField(name, input, v),
  });
}

function setField(name, input, v) {
  st.values[name] = v; setValue(input, v);
  input.closest('.field')?.querySelector('.reset')?.removeAttribute('hidden');
  syncChips(); schedulePreview();
}

/* ---------- variables ---------- */

function editVar(group, name) {
  const v = name == null ? null : xpVars(group).find((x) => x.name === name);
  const shared = name == null ? null : sharedOf(cfg, name);
  const groupNames = Object.fromEntries(GROUPS.map(([g, t]) => [g, t]));
  const xpName = st.xp;
  const was = v?.xp === 'edited' ? ` It is edited for ${xpName}: "every XP" puts back the shared variable here, with what you save.`
    : v?.xp === 'own' ? ` It exists in ${xpName} only: "every XP" makes it a shared variable.` : '';
  openDialog({
    title: name == null ? `Add variable (${groupNames[group]})` : `Edit ${name}`,
    wide: true,
    fields: [
      ...(xpName ? [{ key: 'scope', type: 'select', label: 'Applies to', value: 'xp', options: [
        { value: 'xp', label: `this XP only (${xpName}): the other XPs keep ${shared ? 'the shared variable' : 'what they have'}` },
        { value: 'all', label: 'every XP (the shared variable)' }] },
      { type: 'note', label: `What you save here (and Delete) changes ${xpName} only, unless "every XP" is chosen.${was}` }] : []),
      { key: 'group', type: 'select', label: 'Group', value: group, options: GROUPS.map(([g, t]) => ({ value: g, label: t })) },
      { key: 'name', label: 'Name', value: v?.name ?? '', mono: true, placeholder: group === 'process' ? `${prefix()}TASK__INIT_ARGS__BATCH_SIZE` : 'NB_RUNS' },
      { key: 'label', label: 'Label (optional)', value: v?.label ?? '' },
      { key: 'value', type: 'yaml', label: 'Default value', value: v?.pretty ?? v?.value ?? '', mono: true,
        hint: '$NAME refers to another script variable. A value that starts with $( is a shell command evaluated by the script.' },
      { key: 'options', type: 'area', rows: 3, label: 'Preset values (one per line)', value: (v?.options ?? []).join('\n'), mono: true },
      { key: 'env', type: 'check', label: 'Pass to the container (-e). Names starting with ' + prefix() + ' always are.', value: v?.env ?? false },
    ],
    onSubmit: async (f) => {
      const nv = { name: f.name.trim(), label: f.label.trim(), value: f.value, options: f.options.split('\n').map((s) => s.trim()).filter(Boolean), env: f.env };
      if (!nv.name) throw new Error('Name is required');
      const renamed = name != null && nv.name !== name;
      if ((name == null || renamed) && Object.values(xpVarsOf(cfg, xpName || '')).flat().some((x) => x.name === nv.name) && (f.scope !== 'all' || sharedOf(cfg, nv.name)))
        throw new Error(`${nv.name} exists already: edit it instead`);
      await commit((c) => {
        if (f.scope === 'all' || !xpName) {
          const own = c.xps.find((x) => x.name === xpName);
          if (own?.variables) { delete own.variables[name]; if (renamed) delete own.variables[nv.name]; if (!Object.keys(own.variables).length) delete own.variables; }
          const sh = name != null ? sharedOf(c, name) : null;
          if (sh) c.variables[sh.g].splice(c.variables[sh.g].findIndex((x) => x.name === name), 1);
          const list = c.variables[f.group];
          const at = sh && sh.g === f.group ? cfg.variables[sh.g].findIndex((x) => x.name === name) : list.length;
          list.splice(Math.min(at, list.length), 0, nv);
          if (renamed && sh) for (const x of c.xps) if (x.variables?.[name]) { x.variables[nv.name] = x.variables[name]; delete x.variables[name]; }
        } else {
          if (shared && f.group !== shared.g && !renamed) throw new Error('The group of a shared variable changes for every XP only');
          if (renamed) removeXpVar(c, xpName, name);
          const { name: _n, ...fields } = nv;
          setXpVar(c, xpName, nv.name, fields, f.group);
        }
      });
      delete st.values[name];
      if (renamed) delete st.values[nv.name];
      scheduleSaveValues();
    },
    remove: name == null ? null : async (f) => {
      if (f?.scope === 'all' || !xpName) {
        await commit((c) => {
          const sh = sharedOf(c, name);
          if (sh) c.variables[sh.g].splice(c.variables[sh.g].findIndex((x) => x.name === name), 1);
          for (const x of c.xps) {
            if (x.variables) { delete x.variables[name]; if (!Object.keys(x.variables).length) delete x.variables; }
            if (x.values) delete x.values[name];
          }
        }, { deleted: `variable ${name} (every XP)` });
      } else {
        await commit((c) => removeXpVar(c, xpName, name), { deleted: `variable ${name} from ${xpName}` });
      }
      delete st.values[name];
    },
  });
}

function editSettings() {
  const p = cfg.project;
  openDialog({
    title: 'Settings',
    wide: true,
    fields: [
      { key: 'root', label: 'Project root (the scripts run from here)', value: p.root, mono: true, placeholder: 'absolute, or relative to the settings file (e.g. ../..)' },
      { key: 'scripts_dir', label: 'Scripts folder (relative to the root)', value: p.scripts_dir, mono: true },
      { key: 'src_dir', label: 'Source folder (relative to the root)', value: p.src_dir, mono: true },
      { key: 'env_prefix', label: 'Environment variable prefix', value: p.env_prefix, mono: true },
      { key: 'env_sep', label: 'Separator between levels', value: p.env_sep, mono: true },
      { key: 'config_var', label: 'Variable holding the base config file', value: p.config_var, mono: true },
      { key: 'commit_var', label: 'Variable holding the commit the script checks out', value: p.commit_var, mono: true },
      { key: 'tracking_uri', label: 'MLflow tracking URI (Results and MLflow tabs)', value: p.tracking_uri, mono: true },
      { key: 'mlflow_image', label: 'Image of the launcher (start.sh) and of its MLflow server: its MLflow must match the databases (empty: the Docker image variable; local: no container)', value: p.mlflow_image, mono: true, placeholder: 'e.g. me/myproject:1.0' },
      { key: 'snapshot', label: 'What start.sh copies of the project for the launcher\'s container, relative to the root (empty: everything but what .gitignore files exclude; used at the next start)', value: p.snapshot, mono: true, placeholder: 'e.g. .git src configs submodules scripts' },
    ],
    onSubmit: async (v) => { await commit((c) => { Object.assign(c.project, v); }); },
  });
}

async function editTemplate() {
  let text;
  try { text = (await api('/api/template')).text; } catch (e) { status(e.message, true); return; }
  openDialog({
    title: 'Script template',
    wide: true,
    fields: [
      { type: 'note', label: 'Placeholders: @@HEADER@@ (comment lines), @@WORKDIR@@ (project root), @@SHELL_VARS@@ (script variables), '
        + '@@CONTAINER_ENV@@ (the CONTAINER_ENV array passed with -e), @@LOOP@@ (the selected command loop). Docker flags, git handling and docker cp lines live here.' },
      { key: 'text', type: 'area', rows: 28, mono: true, label: 'template.sh', value: text },
    ],
    onSubmit: async (v) => { await api('/api/template', { text: v.text }, 'PUT'); status('Template saved'); schedulePreview(); },
  });
}

/* ---------- drawing ---------- */

function pickerCard({ bid, label, tag, list, rows, selected, onPick, onAdd, onEdit, onBlock, onRemove, onDiff, footer, body }) {
  return h('div', { class: 'card', 'data-bid': bid ?? '' },
    h('div', { class: 'card-head' },
      bid ? h('span', { class: 'grip', draggable: 'true', tabindex: '0', role: 'button', 'data-grip': bid, 'aria-label': `Move the block ${label}`,
        title: 'Drag to move this block (or focus it and use the arrow keys). Order matters: a later block overrides the variables an earlier one sets.' }, '⠿') : '',
      h('h3', { title: label }, label),
      tag ? h('span', { class: 'tag', title: tag }, 'path') : '',
      list ? h('span', { class: 'tag list', title: 'A list block: one option per value, and "all" loops over them' }, 'list') : '',
      h('button', { class: 'tiny', type: 'button', title: list ? 'Edit the values of the list' : 'Add an option', onclick: onAdd }, list ? 'Values' : 'Add'),
      onDiff && rows.length > 1 ? h('button', { class: 'tiny', type: 'button', title: 'Compare two options of this block: the variables and the config they give', onclick: onDiff }, 'Diff') : '',
      onBlock ? h('button', { class: 'tiny', type: 'button', title: 'Rename the block or change its path', onclick: onBlock }, 'Block') : '',
      onRemove ? h('button', { class: 'tiny', type: 'button', title: 'Remove this block from the XP', onclick: onRemove }, 'Remove') : ''),
    body || h('div', { class: 'rows' },
      ...rows.map((r) => h('div', { class: 'item' + (r.loop ? ' loop-all' : ''), 'data-row': r.name, title: r.loop ? 'Runs the command loop once per value of the list' : null },
        h('button', { class: 'row', type: 'button', title: r.preview || '(empty)', 'aria-pressed': String(r.name === selected), onclick: () => onPick(r.name) },
          h('span', { class: 'name' }, r.name,
            r.norun ? h('span', { class: 'norun-badge', title: 'No run from the Results filter matches this option' + (r.norun.out ? ` (${r.norun.out} filtered out)` : '') + '. Delete it from Results > Runs, or with Edit.' }, 'no runs') : ''),
          h('code', {}, r.preview || '(empty)')),
        h('button', { class: 'tiny edit', type: 'button', 'aria-label': `Edit ${r.name}`, onclick: () => onEdit(r.name) }, 'Edit')))),
    footer || '');
}

/* ---------- difference between two selections (e.g. two options of a block) ---------- */

function diffDialog(bid) {
  const xp = xpDef();
  const a = { ...st.blocks };
  const b = { ...st.blocks };
  const rows = blk(bid).rows.map((r) => r.name);
  b[bid] = rows[(rows.indexOf(a[bid]) + 1) % rows.length];        // B: the next option of the block
  let dlg = $('#diffdlg');
  if (!dlg) { dlg = h('dialog', { id: 'diffdlg', class: 'wide diffdlg', 'aria-labelledby': 'diff-title' }); document.body.append(dlg); }
  const out = h('div', { class: 'df-out' }, h('p', { class: 'empty' }, 'Comparing...'));
  const grid = h('div', { class: 'df-grid' });
  let seq = 0;

  const selOf = (blocks) => ({ ...st, blocks });
  function pick(side, id, value) { (side === 'a' ? a : b)[id] = value; drawGrid(); run(); }
  function drawGrid() {
    const sel = (side, x) => {
      const s = h('select', { 'aria-label': `${side.toUpperCase()}: ${x.label}` }, ...x.rows.map((r) => h('option', { value: r.name }, r.name)));
      s.value = (side === 'a' ? a : b)[x.id];
      s.addEventListener('change', () => pick(side, x.id, s.value));
      return s;
    };
    grid.replaceChildren(h('span', { class: 'df-h' }, 'Block'), h('span', { class: 'df-h' }, 'A'), h('span', { class: 'df-h' }, 'B'),
      ...xp.blocks.filter((x) => x.rows.length).flatMap((x) => {
        const diff = a[x.id] !== b[x.id] ? ' differs' : '';
        return [h('span', { class: 'df-lab' + diff, title: x.id }, x.label), h('span', { class: 'df-cell' + diff }, sel('a', x)), h('span', { class: 'df-cell' + diff }, sel('b', x))];
      }));
  }
  const val = (v) => (v == null ? h('span', { class: 'hint' }, 'not set') : h('code', {}, v));
  async function run() {
    const my = ++seq;
    out.replaceChildren(h('p', { class: 'empty' }, 'Comparing...'));
    let r;
    try { r = await api('/api/diff', { a: selOf(a), b: selOf(b) }); } catch (e) { if (my === seq) out.replaceChildren(h('pre', { class: 'dlg-err' }, e.message)); return; }
    if (my !== seq) return;
    const kids = [];
    for (const [k, e] of Object.entries(r.errors || {})) kids.push(h('p', { class: 'dlg-err' }, `${k.toUpperCase()}: ${e}`));
    if (!r.blocks.length) kids.push(h('p', { class: 'empty' }, 'A and B are the same selection.'));
    kids.push(h('h4', {}, `Variables (${r.variables.length} differ)`));
    kids.push(r.variables.length ? h('table', { class: 'df-vars' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Variable'), h('th', {}, 'A'), h('th', {}, 'B'))),
      h('tbody', {}, ...r.variables.map((v) => h('tr', {},
        h('td', { title: v.name }, short2(v.name)),
        h('td', { title: v.a_by ? `set by ${v.a_by}` : '' }, val(v.a)),
        h('td', { title: v.b_by ? `set by ${v.b_by}` : '' }, val(v.b))))))
      : h('p', { class: 'hint' }, 'No variable differs.'));
    const n = r.config.reduce((k, hk) => k + hk.lines.filter((l) => l.s !== 'same').length, 0);
    kids.push(h('h4', {}, 'Config the program gets', h('span', { class: 'hint' }, `  ${n ? `${n} lines differ` : 'identical'}  (- A, + B; base config + variables, as in the Config preview)`)));
    for (const hk of r.config) {
      kids.push(h('div', { class: 'df-hunk' },
        h('div', { class: 'df-where' }, hk.where ? hk.where + ':' : '(top)', h('span', { class: 'hint' }, `  line ${hk.line} of A`)),
        h('pre', { class: 'df-lines' }, ...hk.lines.map((l) => h('span', { class: 'df-l ' + l.s }, (l.s === 'add' ? '+ ' : l.s === 'del' ? '- ' : '  ') + l.t + '\n')))));
    }
    out.replaceChildren(...kids);
  }
  const use = (blocks, label) => h('button', { type: 'button', class: 'ghost', title: `Select the options of ${label} in the launcher`,
    onclick: () => { Object.assign(st.blocks, blocks); dlg.close(); draw(); } }, `Select ${label}`);
  dlg.replaceChildren(h('div', { class: 'df-wrap' },
    h('div', { class: 'pk-head' }, h('h3', { id: 'diff-title' }, `Compare: ${blk(bid).label}`), h('span', { class: 'sp' }),
      h('button', { type: 'button', class: 'ghost', title: 'Swap A and B', onclick: () => { const t = { ...a }; Object.assign(a, b); Object.assign(b, t); drawGrid(); run(); } }, 'Swap'),
      use(a, 'A'), use(b, 'B'),
      h('button', { type: 'button', class: 'ghost', onclick: () => dlg.close() }, 'Close')),
    h('p', { class: 'hint' }, 'A starts as the current selection, B with the next option of the block. Any block can be changed on either side.'),
    grid, out));
  drawGrid();
  dlg.showModal();
  run();
}

/* move an XP in the side panel: before / after another one */
async function moveXp(name, target, after) {
  try {
    await commit((c) => {
      const i = c.xps.findIndex((x) => x.name === name);
      const [moved] = c.xps.splice(i, 1);
      const j = c.xps.findIndex((x) => x.name === target);
      c.xps.splice(after ? j + 1 : j, 0, moved);
    });
  } catch (e) { status(e.message, true); return; }
  document.querySelector(`.xp-grip[data-xp="${CSS.escape(name)}"]`)?.focus();
}

let movingXp = null;
function drawSide() {
  $('#xps').replaceChildren(...cfg.xps.map((x, i) => h('li', { class: 'xp', 'aria-current': String(x.name === st.xp), 'data-xp': x.name },
    h('span', { class: 'grip xp-grip', draggable: 'true', tabindex: '0', role: 'button', 'data-xp': x.name, 'aria-label': `Move the XP ${x.name}`,
      title: 'Drag to reorder the XPs (or focus it and use the up / down arrow keys)',
      ondragstart: (ev) => { movingXp = x.name; ev.dataTransfer.effectAllowed = 'move'; ev.dataTransfer.setData('text/plain', x.name); ev.currentTarget.closest('li').classList.add('moving'); },
      ondragend: () => { movingXp = null; document.querySelectorAll('#xps li').forEach((li) => li.classList.remove('moving', 'drop-before', 'drop-after')); },
      onkeydown: (ev) => {
        if (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown') return;
        ev.preventDefault();
        const j = ev.key === 'ArrowUp' ? i - 1 : i + 1;
        if (j >= 0 && j < cfg.xps.length) moveXp(x.name, cfg.xps[j].name, ev.key === 'ArrowDown');
      } }, '⠿'),
    h('button', {
      class: 'pick', type: 'button', title: x.name,
      onclick: () => { st.xp = x.name; syncSelection(); draw(); if (matchMedia('(max-width: 800px)').matches) setCollapsed(true); },
    }, x.name),
    h('span', { class: 'acts' },
      h('button', { class: 'tiny', type: 'button', onclick: () => editXp(x.name) }, 'Rename'),
      h('button', { class: 'tiny', type: 'button', onclick: () => deleteXp(x.name) }, 'Delete')))));
  $('#xpname').textContent = st.xp ?? '';
}

/* the body of a list block's card: loop over all the values, or one value */
function listBody(b) {
  const all = b.rows.find((r) => r.loop_all);
  const values = b.rows.filter((r) => !r.loop_all);
  const c = st.blocks[b.id];
  const looping = !!all && c === all.name;
  const picked = looping ? values.map((r) => r.name) : Array.isArray(c) ? c : [c];   // the values in use
  const name = fullName(b, b.var);
  const lv = name.toLowerCase();
  const set = (v) => { st.blocks[b.id] = v; draw(); };
  const toggle = (n) => {
    if (!all) { set(n); return; }                              // a list that does not loop: one value
    if (looping) { set([n]); return; }                         // from "all": only this one
    const now = picked.includes(n) ? picked.filter((x) => x !== n) : [...picked, n];
    if (!now.length) { status('Keep at least one value (or loop over all of them)', true); return; }
    set(values.map((r) => r.name).filter((x) => now.includes(x)));   // in the order of the list
  };
  const mode = all ? h('div', { class: 'seg lb-mode', role: 'group', 'aria-label': `How ${b.label} is used` },
    h('button', { type: 'button', 'aria-pressed': String(looping), title: `Run the command loop once per value ($${name} -> \${${lv}})`, onclick: () => set(all.name) }, `\u21bb Loop over all ${values.length}`),
    h('button', { type: 'button', 'aria-pressed': String(!looping), title: 'Tick the values to use: one sets the variable, several are looped over',
      onclick: () => { if (looping) set([values[0].name]); } }, looping ? 'Selected' : `Selected (${picked.length})`)) : '';
  const n = picked.length;
  const say = looping || n > 1
    ? h('p', { class: 'lb-say loop' }, `The whole command loop runs once per ${looping ? '' : 'ticked '}value (`, h('b', {}, `${n}\u00d7`), '), as the outermost loop: ',
      h('code', {}, `$${name}`), ' becomes ', h('code', {}, `\${${lv}}`), '. See "show the loop" in Run.')
    : h('p', { class: 'lb-say' }, h('code', {}, name), ' = ', h('b', {}, picked[0]), all ? '. Tick more values to loop over them.' : '.');
  const list = h('div', { class: 'rows lb-values' + (looping ? ' looping' : '') },
    ...values.map((r) => {
      const it = b.items.find((x) => (x.label || x.value) === r.name) || { value: r.name };
      const on = picked.includes(r.name);
      return h('div', { class: 'item', 'data-row': r.name },
        h('button', { class: 'row lb-row' + (on ? ' on' : ''), type: 'button', role: all ? 'checkbox' : 'radio', 'aria-checked': String(on),
          title: `${name}=${it.value}` + (looping ? '\nClick to use only this value' : all ? '\nClick to tick / untick it' : ''), onclick: () => toggle(r.name) },
          h('span', { class: 'lb-tick', 'aria-hidden': 'true' }, on ? '\u2713' : ''),
          h('span', { class: 'lb-text' }, h('span', { class: 'name' }, it.label || it.value), it.label ? h('code', {}, it.value) : '')));
    }));
  const tools = all && !looping ? h('div', { class: 'lb-tools' },
    h('button', { type: 'button', class: 'tiny', onclick: () => set(all.name) }, 'all'),
    h('button', { type: 'button', class: 'tiny', onclick: () => set([values[0].name]) }, 'only the first'),
    h('span', { class: 'hint' }, `${n} of ${values.length}`)) : '';
  return h('div', { class: 'lb' }, mode, say, tools, list);
}

function drawStrip() {
  const xp = xpDef();
  const cards = [];
  const noRunOf = new Map((window.XPResults?.noRuns() || []).map((x) => [`${x.bid}\u0000${x.name}`, x]));
  for (const b of xp?.blocks || []) {
    const bid = b.id;
    cards.push(pickerCard({
      bid, label: b.label, tag: b.path, list: b.kind === 'list', selected: st.blocks[bid],
      rows: b.rows.map((r) => ({ name: r.name, preview: Object.entries(r.vars).map(([k, v]) => `${k}=${v}`).join('\n'),
        norun: noRunOf.get(`${bid}\u0000${r.name}`), loop: !!r.loop_all })),
      onPick: (name) => { st.blocks[bid] = name; draw(); },
      onAdd: () => editOption(bid, null), onEdit: (name) => editOption(bid, name),
      onBlock: () => editBlock(bid), onRemove: () => removeBlock(bid), onDiff: () => diffDialog(bid),
      body: b.kind === 'list' ? listBody(b) : null,
      footer: h('div', { class: 'card-vars', 'data-bid': bid, 'aria-label': `Variables set by ${b.label}` }, ...blockVars(b)),
    }));
  }
  if (xp) cards.push(h('button', { class: 'addblock', type: 'button', onclick: addBlock }, '+ Add block'));
  else cards.push(h('div', { class: 'empty' }, 'No XP yet. Create one in the side panel.'));
  const strip = $('#strip');
  const left = strip.scrollLeft;
  strip.replaceChildren(...cards);
  strip.scrollLeft = left;
}

function syncChips() {
  // the launcher's value chips only (not the chips of the Results tab)
  document.querySelectorAll('.chip[data-var]').forEach((b) => {
    const input = document.getElementById('f-' + b.dataset.var);
    b.setAttribute('aria-pressed', String(!!input && input.value === b.dataset.value));
  });
}

function varField(group, v) {
  const input = autoGrow(h('textarea', { id: 'f-' + v.name, class: 'grow', spellcheck: 'false', autocomplete: 'off' }));
  input.value = st.values[v.name] ?? table[v.name]?.value ?? v.value;
  const reset = h('button', { class: 'tiny reset', type: 'button', title: 'Back to the value from the config / selected options', hidden: '', onclick: () => {
    delete st.values[v.name]; setValue(input, table[v.name]?.value ?? v.value); reset.hidden = true; syncChips(); schedulePreview();
  } }, 'reset');
  reset.hidden = st.values[v.name] === undefined;
  input.addEventListener('input', () => { st.values[v.name] = input.value; reset.hidden = false; syncChips(); schedulePreview(); });
  const chips = v.options.map((o) => h('button', {
    class: 'chip', type: 'button', 'data-var': v.name, 'data-value': o,
    onclick: () => { st.values[v.name] = o; setValue(input, o); reset.hidden = false; syncChips(); schedulePreview(); },
  }, o));
  const extra = v.name === cfg.project.commit_var ? h('button', { class: 'tiny', type: 'button', onclick: () => pickCommit(v.name, input) }, 'Pick')
    : v.name === cfg.project.config_var ? h('span', { class: 'cfg-btns' },
      h('button', { class: 'tiny', type: 'button', onclick: () => pickConfig(v.name, input) }, 'Browse'),
      h('button', { class: 'tiny', type: 'button', title: `A base config for this XP, written in ${genDir(st.xp)} (works whatever the commit)`, onclick: () => newConfig(input.value.trim()) }, 'New...'),
      h('button', { class: 'tiny', type: 'button', 'data-genedit': '', title: 'Edit the base config written by the launcher', onclick: () => editConfigFile(input.value.trim()) }, 'Edit file')) : '';
  // one line: scope | label (name under it) | value (+ where it comes from) | suggested values | buttons
  return h('div', { class: 'field vrow', 'data-var': v.name, 'data-search': `${v.label} ${v.name}`.toLowerCase() },
    h('span', { class: 'scope-badge', 'data-scope': v.name }),
    h('label', { for: 'f-' + v.name, class: 'vlabel', title: v.name }, v.label || short2(v.name), v.label ? h('code', {}, v.name) : '',
      v.xp ? h('span', { class: 'xp-tag', title: v.xp === 'own' ? `A variable of ${st.xp} only` : `Edited for ${st.xp} only (Edit: "every XP" to change the shared variable)` }, v.xp === 'own' ? 'this XP only' : 'edited for this XP') : ''),
    h('div', { class: 'vval' }, h('div', { class: 'field-row' }, input, reset), extra ? h('div', { class: 'vextra' }, extra) : '', h('span', { class: 'src', 'data-src': v.name })),
    h('div', { class: 'chips' }, ...chips),
    h('div', { class: 'vact' },
      h('button', { class: 'tiny', type: 'button', 'aria-label': `Edit ${v.name}`, title: 'Edit the variable (label, default value, suggested values, -e)', onclick: () => editVar(group, v.name) }, 'Edit')));
}

/* a variable worth showing for this selection: edited, with suggested values, set by a block or the loop, or the
   config / commit; the others (defaults never touched) are folded */
function important(v) {
  const e = table[v.name];
  return st.values[v.name] !== undefined || (v.options || []).length > 0 || [cfg.project.config_var, cfg.project.commit_var].includes(v.name)
    || (e && (!['default', 'edited'].includes(e.source) || e.loop));
}
let shownVars = '';
const varFilter = { text: '' };

function drawVars() {
  const all = GROUPS.flatMap(([g]) => xpVars(g));
  shownVars = all.filter(important).map((v) => v.name).join(',');
  const filter = h('input', { type: 'search', class: 'vfilter', placeholder: 'filter the variables (label, name)', value: varFilter.text, 'aria-label': 'Filter the variables' });
  filter.addEventListener('input', () => { varFilter.text = filter.value; applyFilter(); });
  $('#vars').replaceChildren(h('div', { class: 'vtools' }, filter, h('span', { class: 'hint' }, `${all.length} variables; the ones not used by this selection are folded`)),
    ...GROUPS.map(([g, title, note]) => {
      const vars = xpVars(g);
      const main = vars.filter(important), rest = vars.filter((v) => !important(v));
      const more = rest.length ? h('details', { class: 'vmore' }, h('summary', {}, `${rest.length} more (defaults)`, h('span', { class: 'hint' }, ` ${rest.map((v) => v.label || short2(v.name)).join(', ')}`)),
        h('div', { class: 'vtable' }, ...rest.map((v) => varField(g, v)))) : '';
      const d = h('details', { class: 'vgroup' },
        h('summary', {}, title, h('span', { class: 'hint' }, note)),
        g === 'run' ? h('div', { id: 'looppanel', class: 'looppanel' }) : '',
        h('div', { class: 'vtable' }, ...main.map((v) => varField(g, v))),
        more,
        h('button', { class: 'ghost', type: 'button', onclick: () => editVar(g, null) }, '+ Add variable'));
      if (!closedGroups.has(g)) d.setAttribute('open', '');
      d.addEventListener('toggle', () => { if (d.open) closedGroups.delete(g); else closedGroups.add(g); });
      return d;
    }));
  syncChips();
  drawLoopPanel();
  applyFilter();
}

/* the filter box: only the matching variables, folded ones included */
function applyFilter() {
  const q = varFilter.text.trim().toLowerCase();
  document.querySelectorAll('#vars .vrow').forEach((r) => {
    const val = (r.querySelector('textarea, input')?.value || '').toLowerCase();
    r.hidden = !!q && !r.dataset.search.includes(q) && !val.includes(q);
  });
  document.querySelectorAll('#vars .vmore').forEach((m) => {
    if (q) { m.dataset.wasOpen ??= String(m.open); m.open = [...m.querySelectorAll('.vrow')].some((r) => !r.hidden); } else if (m.dataset.wasOpen) { m.open = m.dataset.wasOpen === 'true'; delete m.dataset.wasOpen; }
  });
}

function applyTable() {
  // a variable that this selection now uses (set by a block...) comes out of the folded ones, and back
  const now = GROUPS.flatMap(([g]) => xpVars(g)).filter(important).map((v) => v.name).join(',');
  if (now !== shownVars && !document.querySelector('#vars textarea:focus')) drawVars();
  document.querySelectorAll('.field').forEach((f) => {
    const name = f.dataset.var, e = table[name], input = f.querySelector('textarea, input');
    if (e && st.values[name] === undefined && document.activeElement !== input && input.value !== e.value) setValue(input, e.value);
    const src = f.querySelector('.src');
    src.textContent = e && !['default', 'edited'].includes(e.source) ? `set by ${e.source}` : '';
    const ge = f.querySelector('[data-genedit]');
    if (ge) ge.hidden = !isGenerated(e?.value || input.value);
    const badge = f.querySelector('.scope-badge');
    if (badge && e) { badge.textContent = SCOPE_TEXT[e.scope]; badge.className = 'scope-badge ' + e.scope; badge.title = SCOPE_TITLE[e.scope]; }
  });
  syncChips();
}

function draw() {
  drawSide();
  drawStrip();
  drawVars();
  schedulePreview();
}

function setPvTab(tab) {
  $('#pv-yaml').hidden = tab !== 'yaml';
  $('#preview').hidden = tab !== 'script';
  $('#tab-yaml').setAttribute('aria-pressed', String(tab === 'yaml'));
  $('#tab-script').setAttribute('aria-pressed', String(tab === 'script'));
}

// The base config with the APP_* variables applied: changed lines and lines the file does not have are marked.
function drawYaml(r) {
  const box = $('#pv-yaml');
  if (r.error) {
    $('#pv-legend').textContent = '';
    box.replaceChildren(h('p', { class: 'empty pad' }, r.error));
    return;
  }
  $('#pv-legend').replaceChildren(`${r.source}: `,
    h('span', { class: 'yk changed', title: 'a variable replaced the value of the file (hover a line: which one; "was" shows the old value)' }, `${r.changed} changed`),
    h('span', { class: 'yk added', title: 'a variable set a key the file does not have (also what a misspelt name looks like)' }, `${r.added} added`),
    h('span', { class: 'yk same', title: 'as in the file and its subconfigs' }, 'unchanged'),
    r.missing ? h('span', { class: 'yk missing', title: 'required arguments with no default and no value: the program would stop. Set them with Arguments' }, `${r.missing} missing`) : '');
  box.replaceChildren(
    ...r.lines.map((ln) => h('div', { class: 'yl ' + ln.s, title: ln.by ? `${ln.s === 'added' ? 'added' : 'changed'} by ${ln.by}` : '' },
      h('span', { class: 'yt' }, ln.t),
      ln.was !== undefined ? h('span', { class: 'yw' }, `was ${ln.was}`) : '',
      ln.loop ? h('span', { class: 'yw' }, 'changes with the loop') : '')),
    ...r.warnings.map((w) => h('p', { class: 'dlg-note pad' }, w)));
}

let previewTimer;
function schedulePreview() {
  scheduleSaveValues();                           // an edited value changed (or not: nothing saved then)
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    if (!st.xp) { $('#preview').textContent = ''; return; }
    try {
      const [r, y] = await Promise.all([api('/api/render', st), api('/api/preview', st)]);
      $('#preview').textContent = r.script;
      table = Object.fromEntries(r.vars.map((e) => [e.name, e]));
      loopInfo = r.loop;
      checks = y.checks || {};
      applyTable();
      drawLoopPanel();
      drawBlockVars();
      drawYaml(y);
      if ($('#status').className === 'err') status('');
    } catch (e) {
      $('#preview').textContent = '';
      status(e.message, true);
    }
  }, 150);
}

/* ---------- where the scripts run: this machine, or a server (remote_launch.py) ---------- */

function runOn() { return $('#runon')?.value || ''; }

async function fillRunOn() {
  const sel = $('#runon');
  if (!sel) return;
  let list = [];
  try { list = (await api('/api/servers')).servers || []; } catch (e) { return; }
  let want = sel.value;
  try { if (!sel.dataset.filled) want = localStorage.getItem('xp-run-on') || ''; } catch (e) { /* storage unavailable */ }
  const servers = list.filter((s) => !s.disabled);
  sel.replaceChildren(h('option', { value: '' }, 'this machine'),
    ...servers.map((s) => h('option', { value: s.name }, s.name + (s.connected ? '' : ' (not connected)'))));
  sel.value = servers.some((s) => s.name === want) ? want : '';
  sel.dataset.filled = '1';
}

/* ---------- actions ---------- */

async function act(kind) {
  if (!st.xp) { status('Create an XP first', true); return; }
  const buttons = [$('#generate'), $('#launch')];
  buttons.forEach((b) => (b.disabled = true));
  $('#output').hidden = true;
  const server = runOn();
  const on = server ? ` on ${server}` : '';
  status(kind === 'launch' ? `Launching${on}...` + (server ? ' (the project is copied there first when it changed)' : '') : `Writing script${server ? ` for ${server}` : ''}...`);
  try {
    const r = await api('/api/' + kind, { ...st, server });
    if (kind === 'generate') status(`Wrote ${r.path}`);
    else if (r.running) status(server ? `Wrote ${r.path}; still launching on ${server}: its result will be in Servers > actions of ${server}`
      : `Wrote ${r.path}; still running (pid ${r.pid})`);
    else {
      status(`Wrote ${r.path}; ${server ? `launched on ${server}, ` : ''}exited with code ${r.returncode}` + (r.folder ? ` (there: ${r.folder})` : ''), r.returncode !== 0);
      if (r.output.trim()) { $('#output').textContent = r.output; $('#output').hidden = false; }
    }
  } catch (e) {
    status(e.message, true);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

function setCollapsed(on) {
  $('#app').classList.toggle('collapsed', on);
  $('#toggle').setAttribute('aria-expanded', String(!on));
  try { localStorage.setItem('xp-side-collapsed', on ? '1' : '0'); } catch (e) { /* storage unavailable */ }
}

/* ---------- drag a variable of a block's list onto an option of any block ---------- */

let dragged = null;                                     // {bid, row, full, value} of the variable being dragged

function clearDropMarks() {
  document.querySelectorAll('.drop-target, .drop-card').forEach((x) => x.classList.remove('drop-target', 'drop-card'));
}

/* the option a drop lands on: the option under the pointer, else the selected option of the card */
function dropSpot(ev) {
  const card = ev.target.closest?.('.card[data-bid]');
  if (!card || !card.dataset.bid) return null;
  const item = ev.target.closest('.item[data-row]');
  return { card, item, bid: card.dataset.bid, row: item ? item.dataset.row : st.blocks[card.dataset.bid] };
}

async function dropVar(src, bid, row, move) {
  if (src.bid === bid && src.row === row) return;
  if (blk(bid)?.kind === 'list' || blk(src.bid)?.kind === 'list') {
    status('The options of a list block are made from its values: edit the list (Values) instead', true);
    return;
  }
  const tb = blk(bid), sb = blk(src.bid);
  const target = tb.rows.find((r) => r.name === row);
  const rel = tb.path && src.full.startsWith(tb.path) && src.full.length > tb.path.length ? src.full.slice(tb.path.length) : src.full;
  const had = Object.entries(target.vars).find(([k]) => fullName(tb, k) === src.full);
  const what = short2(src.full);
  const note = had ? (had[1] === src.value ? ' (it already had this value)' : ` (replacing ${had[1].replace(/\s+/g, ' ').slice(0, 40)})`) : '';
  try {
    await commit((c) => {
      const t = blkIn(c, bid).rows.find((r) => r.name === row);
      if (had) delete t.vars[had[0]];
      t.vars[had ? had[0] : rel] = src.value;
      if (move) {
        const sr = blkIn(c, src.bid).rows.find((r) => r.name === src.row);
        for (const k of Object.keys(sr.vars)) if (fullName(sb, k) === src.full) delete sr.vars[k];
      }
    }, { done: `${move ? 'Moved' : 'Copied'} ${what} from "${src.row}" to "${row}" (${tb.label})${note}` });
  } catch (e) { status(e.message, true); }
}

/* ---------- moving blocks: drag the grip of a card (or arrow keys on it) ---------- */

let movingBlock = null;                                 // id of the block being dragged

async function moveBlock(bid, target, after) {
  const ids = xpDef().blocks.map((b) => b.id);
  const from = ids.indexOf(bid);
  if (from < 0 || bid === target) return;
  const rest = ids.filter((x) => x !== bid);
  let at = target == null ? Math.max(0, Math.min(rest.length, from + after)) : rest.indexOf(target) + (after ? 1 : 0);
  if (at === from && target == null) return;
  rest.splice(at, 0, bid);
  if (rest.join('\u0000') === ids.join('\u0000')) return;
  try {
    await commit((c) => {
      const xp = c.xps.find((x) => x.name === st.xp);
      xp.blocks = rest.map((id) => xp.blocks.find((b) => b.id === id));
    }, { done: `Moved the block "${blk(bid).label}" to position ${rest.indexOf(bid) + 1}` });
    document.querySelector(`.grip[data-grip="${CSS.escape(bid)}"]`)?.focus();
  } catch (e) { status(e.message, true); }
}

const strip = $('#strip');
strip.addEventListener('dragstart', (ev) => {
  const grip = ev.target.closest?.('.grip');
  if (!grip) return;
  movingBlock = grip.dataset.grip;
  ev.dataTransfer.setData('text/plain', movingBlock);
  ev.dataTransfer.effectAllowed = 'move';
  ev.dataTransfer.setDragImage(grip.closest('.card'), 20, 20);
  grip.closest('.card').classList.add('moving');
});
strip.addEventListener('dragend', () => {
  movingBlock = null;
  document.querySelectorAll('.card.moving, .card.drop-before, .card.drop-after').forEach((c) => c.classList.remove('moving', 'drop-before', 'drop-after'));
});
strip.addEventListener('keydown', (ev) => {
  const grip = ev.target.closest?.('.grip');
  if (!grip || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(ev.key)) return;
  ev.preventDefault();
  moveBlock(grip.dataset.grip, null, ev.key === 'ArrowLeft' || ev.key === 'ArrowUp' ? -1 : 1);
});
function blockSpot(ev) {
  const card = ev.target.closest?.('.card[data-bid]');
  if (!card || !card.dataset.bid) return null;
  const r = card.getBoundingClientRect();
  return { card, bid: card.dataset.bid, after: ev.clientX > r.left + r.width / 2 };
}
/* while a block or a variable is dragged, the strip scrolls when the pointer is near its left / right edge */
const autoScroll = { v: 0, raf: 0 };
function scrollStep() {
  if (!autoScroll.v) { autoScroll.raf = 0; return; }
  strip.scrollLeft += autoScroll.v;
  autoScroll.raf = requestAnimationFrame(scrollStep);
}
function stopAutoScroll() { autoScroll.v = 0; strip.classList.remove('autoscrolling'); }
document.addEventListener('dragover', (ev) => {
  if (!movingBlock && !dragged) return;
  const r = strip.getBoundingClientRect(), edge = Math.min(90, r.width / 4);
  let v = 0;
  if (ev.clientY > r.top - 40 && ev.clientY < r.bottom + 40) {
    if (ev.clientX < r.left + edge) v = -Math.ceil(((r.left + edge - ev.clientX) / edge) * 18);
    else if (ev.clientX > r.right - edge) v = Math.ceil(((ev.clientX - (r.right - edge)) / edge) * 18);
  }
  autoScroll.v = Math.max(-24, Math.min(24, v));
  strip.classList.toggle('autoscrolling', !!autoScroll.v);
  if (autoScroll.v && !autoScroll.raf) autoScroll.raf = requestAnimationFrame(scrollStep);
});
['dragend', 'drop'].forEach((t) => document.addEventListener(t, stopAutoScroll, true));

strip.addEventListener('dragover', (ev) => {
  if (movingBlock) {
    const spot = blockSpot(ev);
    document.querySelectorAll('.card.drop-before, .card.drop-after').forEach((c) => c.classList.remove('drop-before', 'drop-after'));
    if (!spot || spot.bid === movingBlock) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    spot.card.classList.add(spot.after ? 'drop-after' : 'drop-before');
    return;
  }
  if (!dragged) return;
  const spot = dropSpot(ev);
  clearDropMarks();
  if (!spot || (spot.bid === dragged.bid && spot.row === dragged.row)) { ev.dataTransfer.dropEffect = 'none'; return; }
  ev.preventDefault();
  ev.dataTransfer.dropEffect = ev.shiftKey ? 'move' : 'copy';
  if (spot.item) spot.item.classList.add('drop-target');
  else {
    spot.card.classList.add('drop-card');
    spot.card.querySelector(`.item[data-row="${CSS.escape(spot.row)}"]`)?.classList.add('drop-target');
  }
});
strip.addEventListener('dragleave', (ev) => { if (!strip.contains(ev.relatedTarget)) clearDropMarks(); });
strip.addEventListener('drop', (ev) => {
  if (movingBlock) {
    const spot = blockSpot(ev), bid = movingBlock;
    movingBlock = null;
    document.querySelectorAll('.card.moving, .card.drop-before, .card.drop-after').forEach((c) => c.classList.remove('moving', 'drop-before', 'drop-after'));
    if (spot && spot.bid !== bid) { ev.preventDefault(); moveBlock(bid, spot.bid, spot.after); }
    return;
  }
  const spot = dragged && dropSpot(ev);
  const src = dragged;
  clearDropMarks();
  if (!spot || !src) return;
  ev.preventDefault();
  dropVar(src, spot.bid, spot.row, ev.shiftKey);
});

$('#scope-legend').replaceChildren(...Object.entries(SCOPE_TEXT).map(([k, t]) => h('span', { class: 'bv-scope ' + k, title: SCOPE_TITLE[k] }, t)));
$('#toggle').addEventListener('click', () => setCollapsed(!$('#app').classList.contains('collapsed')));
$('#newxp').addEventListener('click', () => editXp(null));
$('#generate').addEventListener('click', () => act('generate'));
$('#launch').addEventListener('click', () => act('launch'));
$('#runon').addEventListener('focus', fillRunOn);                 // the connections may have changed
$('#runon').addEventListener('change', () => { try { localStorage.setItem('xp-run-on', runOn()); } catch (e) { /* storage unavailable */ } });
fillRunOn();
$('#reload').addEventListener('click', load);
$('#settings').addEventListener('click', editSettings);
$('#template').addEventListener('click', editTemplate);
$('#tab-yaml').addEventListener('click', () => setPvTab('yaml'));
$('#tab-script').addEventListener('click', () => setPvTab('script'));

// dropping an XP in the side panel
const xpList = $('#xps');
const xpSpot = (ev) => {
  const li = ev.target.closest('#xps li');
  if (!li || !movingXp) return null;
  const r = li.getBoundingClientRect();
  return { li, name: li.dataset.xp, after: ev.clientY > r.top + r.height / 2 };
};
xpList.addEventListener('dragover', (ev) => {
  const spot = xpSpot(ev);
  if (!spot) return;
  ev.preventDefault();
  xpList.querySelectorAll('li').forEach((li) => li.classList.toggle('drop-before', li === spot.li && !spot.after));
  xpList.querySelectorAll('li').forEach((li) => li.classList.toggle('drop-after', li === spot.li && spot.after));
});
xpList.addEventListener('drop', (ev) => {
  const spot = xpSpot(ev), name = movingXp;
  if (!spot || !name) return;
  ev.preventDefault();
  if (spot.name !== name) moveXp(name, spot.name, spot.after);
});

let stored = null;
try { stored = localStorage.getItem('xp-side-collapsed'); } catch (e) { /* storage unavailable */ }
setCollapsed(stored === null ? matchMedia('(max-width: 800px)').matches : stored === '1');
load();
