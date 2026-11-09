/* Small helpers shared by the page: DOM builder, API calls, status line and the edit dialog. */
const $ = (s) => document.querySelector(s);
const h = (tag, props = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);     // null / undefined: no attribute
  }
  e.append(...kids);
  return e;
};
/* a textarea that is as tall as its text (one line when the value is short). The browser does it with
   field-sizing: content (also when the value is set from code); elsewhere the height follows input events, value
   changes made with setValue(), and the box becoming visible (e.g. a closed group that opens). */
const NATIVE_FIT = typeof CSS !== 'undefined' && CSS.supports?.('field-sizing', 'content');
const fitObserver = !NATIVE_FIT && typeof ResizeObserver !== 'undefined'
  ? new ResizeObserver((entries) => entries.forEach((e) => e.target._fit?.())) : null;

function autoGrow(ta, min = 1) {
  ta.rows = min;
  ta.classList.add('grow');
  const fit = () => {
    if (NATIVE_FIT || !ta.isConnected || !ta.offsetParent) return;
    const before = ta.style.height;
    ta.style.height = 'auto';
    const want = ta.scrollHeight + 2 + 'px';
    ta.style.height = want === before ? before : want;
  };
  ta._fit = fit;
  if (!NATIVE_FIT) {
    ta.addEventListener('input', fit);
    fitObserver?.observe(ta);
    requestAnimationFrame(fit);
  }
  return ta;
}

/* set the value of an input or auto-growing textarea from code */
function setValue(el, v) {
  el.value = v;
  el._fit?.();
}

const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'x';

async function api(path, body, method) {
  const opts = body !== undefined
    ? { method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : undefined;
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function status(msg, isErr = false, action = null) {
  const s = $('#status');
  s.textContent = msg;
  s.title = msg;
  s.className = isErr ? 'err' : '';
  const b = $('#status-act');
  b.hidden = !action;
  if (action) { b.textContent = action.label; b.onclick = () => { b.hidden = true; action.fn(); }; }
}

/* ---------- dialog ----------
   fields: {key, label, type: text|area|yaml|select|check|pairs|note, value, mono, placeholder, hint, options, rows}
   yaml: a textarea as tall as its text (values that may hold a multi-line YAML dict)
   pairs: value = [[key, value], ...]; `resolve(key)` (optional) shows what the key expands to; `onChange(box)` is
   called after every edit; `actions`: [{label, title, onclick(box)}] extra buttons. The element has
   _get() -> pairs, _set(pairs) and _rows() -> [{key, el (the row's check details), status (its status button)}]. */
const dlg = $('#dlg');

function pairsField(f) {
  const box = h('div', { class: 'pairs' });
  box._open = new Set();                                 // keys whose check details are shown
  const changed = () => { if (f.onChange) f.onChange(box); };
  const add = (k = '', v = '') => {
    const hint = h('span', { class: 'pair-hint' });
    const check = h('div', { class: 'pair-check', hidden: '' });
    const status = h('button', { type: 'button', class: 'pair-status', disabled: '', 'aria-expanded': 'false', title: 'Not checked yet' }, '');
    status.addEventListener('click', () => {
      const open = check.hidden;
      check.hidden = !open;
      status.setAttribute('aria-expanded', String(open));
      const key = ki.value.trim();
      if (open) box._open.add(key); else box._open.delete(key);
    });
    const ki = h('input', { type: 'text', class: 'mono', placeholder: f.keyPlaceholder || 'NAME', spellcheck: 'false', autocomplete: 'off' });
    const vi = autoGrow(h('textarea', { class: 'mono grow', placeholder: f.valuePlaceholder || 'value', spellcheck: 'false', autocomplete: 'off' }));
    ki.value = k; vi.value = v; requestAnimationFrame(vi._fit);
    const upd = () => { hint.textContent = f.resolve && ki.value.trim() ? f.resolve(ki.value.trim()) : ''; };
    ki.addEventListener('input', () => { upd(); changed(); }); upd();
    vi.addEventListener('input', changed);
    const row = h('div', { class: 'pair' }, status, ki, vi,
      h('button', { type: 'button', class: 'tiny', 'aria-label': 'Remove variable', onclick: () => { row.remove(); changed(); } }, 'x'), hint, check);
    row._get = () => [ki.value.trim(), vi.value];
    row._check = check;
    row._status = status;
    if (box._open.has(k)) { check.hidden = false; status.setAttribute('aria-expanded', 'true'); }
    box.insertBefore(row, bar);
  };
  const addBtn = h('button', { type: 'button', class: 'ghost', onclick: () => add() }, '+ Variable');
  const bar = h('div', { class: 'pairs-bar' }, addBtn,
    ...(f.actions || []).map((a) => h('button', { type: 'button', class: 'ghost', title: a.title || '', onclick: () => a.onclick(box) }, a.label)));
  box.append(bar);
  (f.value || []).forEach(([k, v]) => add(k, v));
  if (!(f.value || []).length) add();
  box._get = () => [...box.querySelectorAll('.pair')].map((r) => r._get()).filter(([k]) => k);
  box._rows = () => [...box.querySelectorAll('.pair')].map((r) => ({ key: r._get()[0], el: r._check, status: r._status }));
  box._set = (pairs) => {
    box.querySelectorAll('.pair').forEach((r) => r.remove());
    pairs.forEach(([k, v]) => add(k, v));
    if (!pairs.length) add();
    changed();
  };
  return box;
}

function openDialog({ title, fields, onSubmit, remove, wide = false, submitLabel = 'Save' }) {
  const form = $('#dlgform');
  dlg.classList.toggle('wide', wide);
  const err = h('div', { class: 'dlg-err', role: 'alert' });
  const getters = {};
  const body = fields.map((f) => {
    if (f.type === 'note') return h('p', { class: 'dlg-note' }, f.label);
    let el;
    if (f.type === 'area') el = h('textarea', { spellcheck: 'false', rows: String(f.rows || 6) });
    else if (f.type === 'yaml') el = autoGrow(h('textarea', { spellcheck: 'false', class: 'grow' }), f.rows || 1);
    else if (f.type === 'select') el = h('select', {}, ...f.options.map((o) => h('option', { value: o.value }, o.label)));
    else if (f.type === 'check') el = h('input', { type: 'checkbox' });
    else if (f.type === 'pairs') el = pairsField(f);
    else el = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false' });
    if (f.type === 'check') el.checked = !!f.value;
    else if (f.type !== 'pairs') el.value = f.value ?? '';
    if (f.type === 'yaml') requestAnimationFrame(el._fit);
    if (f.mono) el.classList.add('mono');
    if (f.placeholder) el.placeholder = f.placeholder;
    getters[f.key] = f.type === 'pairs' ? () => el._get() : f.type === 'check' ? () => el.checked : () => el.value;
    const hint = f.hint ? h('small', { class: 'dlg-hint' }, f.hint) : '';
    return f.type === 'check'
      ? h('label', { class: 'dfield check' }, el, h('span', {}, f.label))
      : h('div', { class: 'dfield' }, h('label', {}, h('span', {}, f.label), f.type === 'pairs' ? '' : el), f.type === 'pairs' ? el : '', hint);
  });
  const fail = (e) => { err.textContent = e.message; };
  const vals = () => Object.fromEntries(Object.entries(getters).map(([k, g]) => [k, g()]));
  const actions = h('div', { class: 'dlg-actions' },
    remove ? h('button', { type: 'button', class: 'danger', onclick: () => remove(vals()).then((done) => done !== false && dlg.close()).catch(fail) }, 'Delete') : '',
    h('span', { class: 'sp' }),
    h('button', { type: 'button', onclick: () => dlg.close() }, 'Cancel'),
    h('button', { type: 'submit', class: 'primary' }, submitLabel));
  form.replaceChildren(h('h3', {}, title), ...body, err, actions);
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    err.textContent = '';
    try { await onSubmit(vals()); dlg.close(); } catch (e) { fail(e); }
  };
  dlg.showModal();
}

/* ---------- chooser: a filterable list in its own dialog ----------
   openChooser({title, load: async () => ({items, note}), current, onPick(value)})
   items: {value, label, sub, group, tags: [text], mark}. Enter picks the first visible item. */
let chooserDlg = null;

function openChooser({ title, load, current, onPick }) {
  if (!chooserDlg) {
    chooserDlg = h('dialog', { id: 'chooser', class: 'wide chooser', 'aria-label': 'Choose' });
    document.body.append(chooserDlg);
  }
  const filter = h('input', { type: 'text', placeholder: 'Filter', spellcheck: 'false', autocomplete: 'off' });
  const list = h('div', { class: 'ch-list', role: 'listbox' }, h('p', { class: 'empty' }, 'Loading...'));
  const note = h('p', { class: 'dlg-note' });
  let items = [];
  const pick = (v) => { chooserDlg.close(); onPick(v); };
  const paint = () => {
    const q = filter.value.trim().toLowerCase();
    const shown = items.filter((it) => !q || [it.value, it.label, it.sub, it.group, ...(it.tags || [])].join(' ').toLowerCase().includes(q));
    const out = [];
    let group = null;
    for (const it of shown) {
      if (it.group !== undefined && it.group !== group) { group = it.group; out.push(h('div', { class: 'ch-group' }, group || '.')); }
      out.push(h('button', { type: 'button', class: 'ch-item', role: 'option', 'aria-selected': String(it.value === current), title: it.value, onclick: () => pick(it.value) },
        h('span', { class: 'ch-label' }, it.label, it.mark ? h('span', { class: 'ch-mark' }, it.mark) : '', ...(it.tags || []).map((t) => h('span', { class: 'ch-tag' }, t))),
        it.sub ? h('span', { class: 'ch-sub' }, it.sub) : ''));
    }
    list.replaceChildren(...(out.length ? out : [h('p', { class: 'empty' }, q ? 'Nothing matches.' : 'Nothing found.')]));
  };
  filter.addEventListener('input', paint);
  filter.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); list.querySelector('.ch-item')?.click(); }
  });
  chooserDlg.replaceChildren(h('div', { class: 'ch' },
    h('div', { class: 'pk-head' }, h('h3', {}, title), h('span', { class: 'sp' }),
      h('button', { type: 'button', class: 'ghost', onclick: () => chooserDlg.close() }, 'Close')),
    filter, note, list));
  chooserDlg.showModal();
  filter.focus();
  load().then((r) => {
    items = r.items; note.textContent = r.note || ''; paint();
    list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'center' });
  }).catch((e) => { list.replaceChildren(h('p', { class: 'dlg-err' }, e.message)); });
}
