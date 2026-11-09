/* Argument picker: browse the program's arguments (classes found in src/) and set them as APP_* variables.
   Two modes: opened from the header it writes the variables directly (Save to); opened from an option dialog
   ("Browse arguments", draft mode) it fills that option's variables, which are saved with the dialog.
   Every write asks the server how to do it (/api/set, /api/switch), so that a value inside an object another
   variable sets as a whole is written inside that variable, and a class switch keeps what the new class accepts.
   Depends on editors.js ($, h, api, status) and app.js (cfg, st, commit, fullName, xpDef, blk, blkIn, draw). */
(() => {
  const P = { path: [], filter: '', target: 'auto', data: null, error: '', msg: null, draft: null };
  let pd = null;                                     // the <dialog>

  const short = (s) => String(s || '').split('.').pop();
  const absent = (f) => !f.has_value || f.value === 'NULL';          // no object in the config for this argument
  const btn = (label, onclick, props = {}) => h('button', { type: 'button', class: 'tiny', onclick, ...props }, label);
  const option = (value, label, props = {}) => h('option', { value, ...props }, label);

  function note(text, err = false) { P.msg = text ? { text, err } : null; paint(); }
  async function run(fn) {
    P.msg = null;
    try { await fn(); } catch (e) { note(e.message, true); }
  }

  /* ---------- where a variable is written ---------- */

  /* the base config of the XP when the launcher wrote it (scripts/<XP>/configs/...): values can go straight into it */
  const genConfig = () => {
    const v = (table[cfg.project.config_var]?.value || '').trim();
    return isGenerated(v) ? v : '';
  };

  function targets() {
    const gen = genConfig();
    const t = [['auto', `Where it is set now (new: ${gen ? 'the base config file' : 'a process variable of this XP'})`],
      ...(gen ? [['file', `The base config file (${gen})`]] : []),
      ['process', 'A process variable of this XP (the other XPs keep theirs)'], ['session', 'An edited value of this XP (saved with it; reset drops it)']];
    for (const b of xpDef()?.blocks || []) t.push([`block:${b.id}`, b.kind === 'fixed' ? `Block ${b.label} (every run)` : `Option "${st.blocks[b.id]}" of block ${b.label}`]);
    return t;
  }

  function resolveTarget(v) {                          // v: {source} of the variable that holds the value now, if any
    if (P.target !== 'auto') return P.target === 'file' && !genConfig() ? 'process' : P.target;
    if (!v) return genConfig() ? 'file' : 'process';
    if (v.source === 'edited') return 'session';
    const b = (xpDef()?.blocks || []).find((x) => v.source === `${x.label}: ${st.blocks[x.id]}`);
    return b ? `block:${b.id}` : 'process';
  }

  /* draft mode: P.draft = {block, row, label, get() -> pairs, set(pairs)}; pairs use the dialog's names */
  const draftBlock = () => blk(P.draft.block);
  const draftBody = () => (P.draft ? { block: P.draft.block, row: P.draft.row, vars: P.draft.get() } : null);
  const isDraft = (v) => !!(P.draft && v && P.data && v.source === P.data.draft_source);
  const rel = (b, n) => (b.path && n.startsWith(b.path) && n.length > b.path.length ? n.slice(b.path.length) : n);

  function draftApply(sets, removes) {
    const b = draftBlock();
    const gone = new Set(removes);
    const pairs = P.draft.get().filter(([k]) => !gone.has(fullName(b, k)));
    for (const [n, v] of sets) {
      const at = pairs.findIndex(([k]) => fullName(b, k) === n);
      if (at >= 0) pairs[at][1] = v; else pairs.push([rel(b, n), v]);
    }
    P.draft.set(pairs);
  }

  async function writeVar(name, value, v) {
    const target = resolveTarget(v);
    if (target === 'file') {
      await api('/api/xpconfig/set', { sel: st, name, value });
      schedulePreview();
      return;
    }
    if (target === 'session' && v) { st.values[name] = value; draw(); return; }
    if (target.startsWith('block:')) {
      const bid = target.slice(6), rowName = st.blocks[bid];
      await commit((c) => {
        const b = blkIn(c, bid), row = b.rows.find((r) => r.name === rowName);
        row.vars[Object.keys(row.vars).find((k) => fullName(b, k) === name) ?? rel(b, name)] = value;
      }, { after: () => { delete st.values[name]; } });
      return;
    }
    // the variable of this XP (the other XPs keep theirs: app.js setXpVar)
    await commit((c) => setXpVar(c, st.xp, name, { value }, 'process'), { after: () => { delete st.values[name]; } });
  }

  async function removeVar(v) {
    if (v.source === 'edited') { delete st.values[v.name]; draw(); return; }
    const bid = (xpDef()?.blocks || []).find((x) => v.source === `${x.label}: ${st.blocks[x.id]}`)?.id;
    if (bid) {
      const rowName = st.blocks[bid];
      await commit((c) => {
        const b = blkIn(c, bid), row = b.rows.find((r) => r.name === rowName);
        for (const k of Object.keys(row.vars)) if (fullName(b, k) === v.name) delete row.vars[k];
      }, { deleted: `${short(v.name)} from "${rowName}"` });
      return;
    }
    // from this XP only (the other XPs keep it: app.js removeXpVar)
    await commit((c) => removeXpVar(c, st.xp, v.name), { deleted: `variable ${v.name} from ${st.xp}` });
  }

  /* apply the server's answer {set: [[name, value]], remove: [{name, source}], message, source} */
  async function applyChange(r, v) {
    if (P.draft) {
      const mine = r.remove.filter((x) => x.source === P.data?.draft_source).map((x) => x.name);
      const others = r.remove.filter((x) => x.source !== P.data?.draft_source);
      draftApply(r.set, mine);
      note([r.message, others.length ? `Set elsewhere and now ignored: ${others.map((x) => `${x.name} (${x.source})`).join(', ')}` : '']
        .filter(Boolean).join('. '));
      return;
    }
    for (const x of r.remove) await removeVar(x);
    const holder = r.source ? { source: r.source } : v;
    for (const [n, val] of r.set) await writeVar(n, val, n === r.set[0][0] ? holder : v);
    if (r.message) note(r.message);
  }

  async function setVar(name, value, v) {
    if (!P.draft && resolveTarget(v) === 'file') { await writeVar(name, value, v); note(`Written into ${genConfig()}`); return; }
    const r = await api('/api/set', { sel: st, draft: draftBody(), name, value });
    await applyChange(r, v);
  }

  async function unsetVar(v) {
    if (P.draft) {
      if (isDraft(v)) draftApply([], [v.name]);
      else note(`${v.name} is set by ${v.source}, not by this option: change it there`, true);
      return;
    }
    await removeVar(v);
  }

  /* picking a class: the server says whether CLASS_PATH is enough or the object must be written as a whole */
  async function chooseClass(path, fqn, v) {
    const r = await api('/api/switch', { sel: st, draft: draftBody(), path, cls: fqn });
    await applyChange(r, v);
  }

  const refresh = () => load();

  /* ---------- loading ---------- */

  async function load(fresh = false) {
    try { P.data = await api('/api/args', { sel: st, draft: draftBody(), path: P.path, fresh }); P.error = ''; }
    catch (e) { P.data = null; P.error = e.message; }
    paint();
  }

  function go(path) { P.path = path; P.filter = ''; P.msg = null; return load(); }

  /* ---------- drawing ---------- */

  function valueLine(f) {
    const bits = [];
    const tag = (text, cls = '') => h('span', { class: 'pk-tag ' + cls }, text);
    if (f.linked) bits.push(tag(`set through ${f.linked}`, 'link'));
    else if (f.var) {
      const src = isDraft(f.var) ? 'this option' : f.var.source === 'default' ? 'variable default' : f.var.source === 'edited' ? 'session edit' : f.var.source;
      bits.push(h('code', {}, f.value ?? ''), tag(`set by ${src}`, 'set'));
    } else if (f.within) {
      bits.push(h('code', {}, f.value ?? ''), tag(`inside ${short(f.within.name)} (${isDraft(f.within) ? 'this option' : f.within.source})`, 'set'));
    } else if (f.has_value) bits.push(h('code', {}, f.value ?? ''), tag('config file'));
    else bits.push(h('span', { class: 'pk-def' }, f.required ? 'required, no default' : 'default'), f.required ? '' : h('code', {}, f.default || '""'));
    if (f.class_var) bits.push(tag(`class set by ${isDraft(f.class_var) ? 'this option' : f.class_var.source === 'default' ? 'variable default' : f.class_var.source}`, 'set'));
    if (f.below) bits.push(tag(`${f.below} set inside`, 'set'));
    return h('div', { class: 'pk-value' }, ...bits);
  }

  function textControl(f, locked) {
    const input = h('input', { type: 'text', class: 'mono', spellcheck: 'false', autocomplete: 'off',
      placeholder: f.default_yaml ?? f.default ?? '', title: 'YAML value as written in the config: NULL for none, [1, 2] for a list' });
    input.value = f.edit || '';
    if (f.flag && f.choices === null) input.placeholder = 'A | B';
    const apply = () => {
      const v = input.value.trim();
      if (!v) { note('Type a value (NULL for none).', true); return; }
      return run(async () => { await setVar(f.env, v, f.var); await refresh(); });
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); apply(); } });
    const set = btn('Set', apply, { class: 'tiny primary' });
    input.disabled = set.disabled = locked;
    return [input, set];
  }

  function selectControl(items, onPick, current, locked, placeholder, reload = true) {
    const sel = h('select', {}, option('', placeholder), ...items);
    sel.value = current ?? '';
    if (sel.value !== (current ?? '')) sel.value = '';
    sel.disabled = locked;
    sel.addEventListener('change', () => {
      if (sel.value) run(async () => { await onPick(sel.value); if (reload) await refresh(); else sel.value = ''; });
    });
    return sel;
  }

  function classControl(f, locked, onPick) {
    const items = [...f.classes.map((c) => option(c.fqn, c.name + (c.module ? `   ${c.module}` : ''), { title: c.doc || c.fqn }))];
    if (f.cls && !f.classes.some((c) => c.fqn === f.cls)) items.unshift(option(f.cls, short(f.cls) + '   (not listed)'));
    if (f.nullable) items.push(option('__null__', 'NULL (none)'));
    items.push(option('__other__', 'Other class...'));
    const other = h('input', { type: 'text', class: 'mono', placeholder: 'package.module.Class', hidden: '', spellcheck: 'false' });
    other.hidden = true;
    const sel = h('select', {}, option('', f.cls ? short(f.cls) : f.cls === '' ? '(class from the type)' : 'not set - pick a class'), ...items);
    sel.disabled = locked;
    sel.addEventListener('change', () => { if (sel.value === '__other__') { other.hidden = false; other.focus(); } else if (sel.value) run(async () => { await onPick(sel.value); await refresh(); }); });
    other.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && other.value.trim()) { e.preventDefault(); run(async () => { await onPick(other.value.trim()); await refresh(); }); }
    });
    return [sel, other];
  }

  function controls(f, locked) {
    const box = h('div', { class: 'pk-ctl' });
    if (f.linked) { box.append(h('span', { class: 'pk-def' }, 'linked: set the source instead')); return box; }
    const choose = (v) => run(async () => { await setVar(f.env, v, f.var); await refresh(); });
    if (f.kind === 'class') box.append(...classControl(f, locked, (v) => chooseClass([...P.path, f.name], v, f.class_var || f.var)));
    else if (f.kind === 'classpath') box.append(...classControl(f, locked, (v) => setVar(f.env, v, f.var)));
    else if (f.kind === 'group') box.append(h('span', { class: 'pk-def' }, 'group of arguments'));
    else if (f.choices && !f.flag) {
      box.append(selectControl(f.choices.map((c) => option(c, c)), (v) => setVar(f.env, v, f.var), f.edit, locked, 'choose...'));
    } else {
      const [input, set] = textControl(f, locked);
      if (f.flag && f.choices) {
        const dl = h('datalist', { id: 'pk-dl-' + f.name }, ...f.choices.map((c) => option(c, c)));
        input.setAttribute('list', dl.id);
        box.append(dl);
      }
      box.append(input, set);
    }
    if (f.kind === 'classlist' && f.classes?.length) {
      const input = box.querySelector('input');
      box.append(selectControl(f.classes.map((c) => option(c.fqn, c.name, { title: c.fqn })), (fqn) => {
        const cur = input.value.trim();
        const item = `{class_path: ${fqn}}`;
        input.value = cur && cur !== 'NULL' && cur.endsWith(']') ? `${cur.slice(0, -1)}${cur === '[]' ? '' : ', '}${item}]` : `[${item}]`;
        input.focus();
      }, '', locked, 'append a class...', false));
    }
    if (f.open) box.append(btn('Open >', () => go([...P.path, f.name]), { title: 'See its arguments' }));
    if (f.var && (!P.draft || isDraft(f.var))) box.append(btn('Unset', () => run(async () => { await unsetVar(f.var); await refresh(); }), { title: `Remove ${f.var.name}` }));
    else if (!P.draft && f.in_config && genConfig() && resolveTarget(null) === 'file') {
      box.append(btn('Remove from file', () => run(async () => { await api('/api/xpconfig/set', { sel: st, name: f.env, unset: true }); schedulePreview(); await refresh(); }),
        { title: `Remove ${f.name} from ${genConfig()}` }));
    }
    return box;
  }

  /* problems of the variables behind a field (only warnings and errors: the info notes are in the option dialog) */
  function checkLines(names) {
    const vars = P.data?.checks?.vars || {};
    const out = [];
    for (const n of names) {
      for (const x of vars[n]?.notes || []) if (x.level !== 'info') out.push(h('div', { class: 'pk-check ' + x.level }, x.text));
    }
    return out;
  }

  function fieldRow(f, locked) {
    const mine = isDraft(f.var) || isDraft(f.class_var) || isDraft(f.within);
    const missing = f.required && !f.linked && !f.var && !f.within && (!f.has_value || (f.value === 'NULL' && !f.nullable));
    return h('div', { class: 'pk-row' + (f.var || f.class_var || f.within ? ' set' : f.has_value ? ' cfg' : '') + (mine ? ' draft' : '') + (missing ? ' missing' : ''),
      'data-name': f.name, title: missing ? 'Required: no default and no value yet' : '' },
      h('div', { class: 'pk-main' },
        h('div', { class: 'pk-name' }, h('b', {}, f.name), f.type ? h('span', { class: 'pk-type' }, f.type) : '',
          f.origin && f.origin !== short(P.data.node.cls) ? h('span', { class: 'pk-origin', title: 'declared in this base class' }, f.origin) : ''),
        f.doc ? h('div', { class: 'pk-doc', title: f.doc }, f.doc) : '',
        valueLine(f),
        ...checkLines([f.var?.name, f.class_var?.name].filter(Boolean))),
      controls(f, locked));
  }

  /* draft mode: the variables of the option being edited, with what the program does with each */
  function draftPanel() {
    const b = draftBlock(), vars = P.data?.checks?.vars || {};
    const pairs = P.draft.get();
    const rows = pairs.map(([k, v]) => {
      const n = fullName(b, k), res = vars[n];
      const lvl = res?.level || 'ok';
      return h('div', { class: 'pk-drow ' + lvl },
        h('div', { class: 'pk-dname' }, h('code', { title: n }, k), h('code', { class: 'pk-dval', title: v }, v),
          btn('x', () => run(async () => { draftApply([], [n]); await refresh(); }), { title: `Remove ${n}` })),
        ...(res?.notes || []).map((x) => h('div', { class: 'pk-check ' + x.level }, x.text)),
        res?.fix ? btn('Fix: ' + res.fix.label, () => run(async () => { draftApply([[res.fix.name, res.fix.value]], [...res.fix.drop, n]); await refresh(); }), { title: res.fix.value }) : '');
    });
    const missing = (P.data?.checks?.missing || []).map((m) => h('p', { class: 'pk-warn' }, `${m.cls} (${m.path}) needs ${m.args.join(', ')}: no default and nothing sets it`));
    return h('details', { class: 'pk-draft', open: '' },
      h('summary', {}, `Option "${P.draft.label() || 'new option'}" of block ${b.label}: ${pairs.length} variable${pairs.length === 1 ? '' : 's'}`,
        h('span', { class: 'hint' }, ' saved with the option dialog')),
      ...(rows.length ? rows : [h('p', { class: 'empty' }, 'Nothing yet: set arguments below, they are added here.')]), ...missing);
  }

  function customRow(node, locked) {
    const name = h('input', { type: 'text', class: 'mono', placeholder: 'argument name', spellcheck: 'false' });
    const val = h('input', { type: 'text', class: 'mono', placeholder: 'value', spellcheck: 'false' });
    const add = () => {
      const n = name.value.trim(), v = val.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(n) || !v) { note('Give a name (letters, digits, _) and a value.', true); return; }
      run(async () => { await setVar(node.child_prefix + n.toUpperCase(), v, null); await refresh(); });
    };
    val.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } });
    const set = btn('Add', add, { class: 'tiny primary' });
    name.disabled = val.disabled = set.disabled = locked;
    return h('div', { class: 'pk-row custom' },
      h('div', { class: 'pk-main' }, h('div', { class: 'pk-name' }, h('b', {}, 'Other argument'), h('span', { class: 'pk-type' }, 'not in the list')),
        h('div', { class: 'pk-doc' }, 'For arguments the source analysis cannot see (library classes, **kwargs passed on).')),
      h('div', { class: 'pk-ctl' }, name, val, set));
  }

  function paint() {
    const root = $('#pk');
    if (!root) return;
    const keep = root.querySelector('.pk-list')?.scrollTop ?? 0;
    const d = P.data;
    const kids = [h('div', { class: 'pk-head' }, h('h3', {}, P.draft ? 'Arguments for this option' : 'Arguments'), h('span', { class: 'hint pk-src' }, d ? d.source : ''),
      btn('Rescan', () => load(true), { title: 'Read src/ again (changes are also picked up automatically)' }),
      h('button', { type: 'button', class: 'ghost', onclick: () => pd.close() }, P.draft ? 'Done' : 'Close'))];
    if (P.error) {
      kids.push(h('p', { class: 'pk-warn' }, P.error), h('p', { class: 'dlg-note' }, 'Settings (header) holds the project root and the source folder; the base config comes from the CONFIG variable.'));
      root.replaceChildren(...kids);
      return;
    }
    if (!d) { kids.push(h('p', { class: 'empty' }, 'Reading the sources...')); root.replaceChildren(...kids); return; }
    const node = d.node;
    const locked = node.mode === 'class' && !node.exists;
    kids.push(h('div', { class: 'pk-crumbs' }, ...d.crumbs.flatMap((c, i) => [i ? h('span', { class: 'pk-sep' }, '>') : '',
      h('button', { type: 'button', class: 'tiny' + (i === d.crumbs.length - 1 ? ' here' : ''), onclick: () => go(c.path) }, c.name)])));

    const nodeBox = h('div', { class: 'pk-node' });
    if (node.mode === 'class') {
      const f = { env: node.env, var: node.var, class_var: node.class_var, has_value: node.exists, value: node.exists ? 'x' : 'NULL',
        classes: node.classes, cls: node.cls, nullable: false };
      nodeBox.append(h('div', { class: 'pk-nodehead' }, h('b', {}, node.cls_name || 'no class yet'), ...classControl(f, false, (v) => chooseClass(P.path, v, node.class_var || node.var))));
    } else if (node.mode === 'group') nodeBox.append(h('div', { class: 'pk-nodehead' }, h('b', {}, node.cls_name)));
    if (node.doc) nodeBox.append(h('div', { class: 'pk-doc' }, node.doc));
    nodeBox.append(h('div', { class: 'pk-env' }, node.mode === 'root' ? 'top level of the program config' : node.env));
    if (locked) nodeBox.append(h('p', { class: 'pk-warn' }, 'This argument is not in the config (null or absent). Pick a class: it is created as a whole object. '
      + 'Arguments inside an absent object would be ignored by the program, so they stay locked until then.'));
    for (const w of node.warnings) nodeBox.append(h('p', { class: 'pk-warn' }, w));
    kids.push(nodeBox);

    const filter = h('input', { type: 'text', id: 'pk-filter', placeholder: 'filter arguments', value: P.filter, spellcheck: 'false' });
    filter.addEventListener('input', () => { P.filter = filter.value; paintList(); });
    if (P.draft) {
      kids.push(draftPanel(), h('div', { class: 'pk-bar' }, filter));
    } else {
      const sel = h('select', { id: 'pk-target', title: 'Where the variable is stored' }, ...targets().map(([v, l]) => option(v, l)));
      sel.value = P.target;
      if (sel.value !== P.target) { P.target = 'auto'; sel.value = 'auto'; }
      sel.addEventListener('change', () => { P.target = sel.value; });
      kids.push(h('div', { class: 'pk-bar' }, h('label', {}, 'Save to ', sel), filter));
      const bad = Object.entries(P.data?.checks?.vars || {}).filter(([, r]) => r.level === 'error');
      if (bad.length) kids.push(h('details', { class: 'pk-draft' }, h('summary', {}, `${bad.length} variable(s) of this selection the program ignores or rejects`),
        ...bad.map(([n, r]) => h('div', { class: 'pk-drow error' }, h('code', {}, n), ...r.notes.filter((x) => x.level === 'error').map((x) => h('div', { class: 'pk-check error' }, x.text))))));
    }
    kids.push(P.msg ? h('p', { class: 'pk-msg' + (P.msg.err ? ' err' : ''), role: 'status' }, P.msg.text) : '');
    kids.push(h('div', { class: 'pk-list' }));
    const s = d.index;
    kids.push(h('div', { class: 'pk-foot', title: s.errors.map((e) => `${e.file}: ${e.error}`).join('\n') },
      `${s.classes} classes in ${s.files} files from ${s.src}` + (s.errors.length ? `; ${s.errors.length} file(s) could not be read (hover)` : '')));
    root.replaceChildren(...kids);
    paintList();
    root.querySelector('.pk-list').scrollTop = keep;
  }

  function paintList() {
    const d = P.data, list = $('#pk .pk-list');
    if (!d || !list) return;
    const locked = d.node.mode === 'class' && !d.node.exists;
    const q = P.filter.trim().toLowerCase();
    const rows = d.fields.filter((f) => !q || f.name.toLowerCase().includes(q) || (f.doc || '').toLowerCase().includes(q)).map((f) => fieldRow(f, locked));
    if (!d.fields.length && d.node.mode !== 'root') rows.push(h('p', { class: 'empty' }, d.node.cls ? 'No arguments found for this class.' : 'No class yet.'));
    if (d.node.mode !== 'root' && (d.node.cls || d.node.mode === 'group')) rows.push(customRow(d.node, locked));
    else if (d.node.mode === 'class' && !d.node.cls) rows.push(customRow(d.node, true));
    list.replaceChildren(...rows);
  }

  /* ---------- opening ---------- */

  function open(draft = null) {
    if (!st.xp) { status('Create an XP first', true); return; }
    if (!pd) {
      pd = h('dialog', { id: 'picker', class: 'wide picker', 'aria-label': 'Arguments' }, h('div', { id: 'pk' }));
      document.body.append(pd);
    }
    P.path = []; P.filter = ''; P.msg = null; P.data = null; P.error = ''; P.draft = draft;
    paint();
    pd.showModal();
    load();
  }

  window.argPicker = { open };

  const button = h('button', { id: 'args', type: 'button', title: 'Browse the program arguments (from the classes in src/) and set them' }, 'Arguments');
  $('#settings').before(button);
  button.addEventListener('click', () => open());
})();
