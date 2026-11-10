/* Results tab. The runs of the XP come from an MLflow query (the XP's filter, in MLflow syntax) and are labelled by
   dimensions: named lists of values, each defined by an MLflow filter (first match wins), or the values of one
   parameter. Dimensions start from the blocks of the XP and can be edited (Dimensions...). Everything is saved per XP
   in experiments.yaml.
   Left: a preview computed by the launcher. Right: the report script written for these settings, shown before it
   runs, then what it produced (the LaTeX table compiled, or the figures). The script queries MLflow itself: nothing in
   it is precomputed, it can be run again later. The server writes and runs it; the page never sends code.
   Depends on editors.js ($, h, api, status, openDialog, openChooser, autoGrow) and app.js (cfg, st, blk, commit, draw). */
(() => {
  const R = { data: null, error: '', loading: false, tab: 'launcher', out: {}, view: 'table', sel: null, source: null, switching: false, switchError: '' };
  const selq = () => ({ ...st, selection: R.sel });     // the launcher's selection + the results selection shown
  const OTHER = '(other)';
  try { R.view = localStorage.getItem('xp-results-view') || 'table'; } catch (e) { /* storage unavailable */ }

  const conf = () => R.data.config;
  const lay = () => conf().layout;
  const fmt = (x) => (x == null ? '' : x.toFixed(lay().digits));
  const metricList = () => Object.entries(conf().metrics).map(([key, label]) => ({ key, label }));

  /* ---------- saving the settings of the XP ---------- */

  /* experiment, dimensions and links belong to the XP; the rest (filter, layout, metrics, views, readme) to the
     selection shown */
  const TOP = ['experiment', 'dimensions', 'links'];
  function selectionIn(x) {
    x.results = x.results || {};
    const sels = x.results.selections || (x.results.selections = []);
    let s = sels.find((y) => y.id === conf().selection);
    if (!s) { s = { id: conf().selection, title: conf().selection_title }; sels.push(s); }
    return s;
  }
  async function saveResults(patch) {
    try {
      await commit((c) => {
        const x = c.xps.find((y) => y.name === st.xp);
        x.results = x.results || {};
        for (const [k, v] of Object.entries(patch)) {
          if (TOP.includes(k)) { if (v === undefined) delete x.results[k]; else x.results[k] = v; } else selectionIn(x)[k] = v;
        }
      });
      R.out = {};
      await load(false);
    } catch (e) { status(e.message, true); }
  }

  const setLayout = (patch) => saveResults({ layout: { ...lay(), ...patch } });
  const saveMetrics = (list) => saveResults({ metrics: list });

  /* ---------- controls ---------- */

  /* where a dimension comes from: a block of the XP, a link (what differs between the linked runs), or else */
  function dimGroup(d) {
    for (const l of conf().links || []) {
      const pre = l.name + '.';
      if ((d.param || '').startsWith(pre) || (d.values || []).some((v) => (v.rule || '').includes(pre))) return `Linked by ${l.name}: what differs between the linked runs`;
    }
    return d.block ? 'Blocks of this XP' : 'Other dimensions';
  }
  function dimSelect(value, onChange, exclude = []) {
    const dims = conf().dimensions.filter((d) => !exclude.includes(d.name) || d.name === value);
    const groups = [...new Set(dims.map(dimGroup))];
    const sel = h('select', {}, h('option', { value: '' }, '-'),
      ...groups.map((g) => h('optgroup', { label: g }, ...dims.filter((d) => dimGroup(d) === g).map((d) => h('option', { value: d.name }, d.name)))));
    sel.value = value || '';
    sel.addEventListener('change', () => onChange(sel.value));
    return sel;
  }

  function controls() {
    const L = lay(), C = conf();
    const filter = h('input', { type: 'text', class: 'mono rs-filter-in', spellcheck: 'false', value: C.filter,
      placeholder: "attributes.status = 'FINISHED' and params.settings.seed = '42'" });
    filter.value = C.filter;
    const applyFilter = () => saveResults({ filter: filter.value.trim() });
    filter.addEventListener('keydown', (e) => { if (e.key === 'Enter') applyFilter(); });
    const rows = h('span', { class: 'rs-rows' }, ...L.rows.map((name, i) => h('span', { class: 'rs-chip' }, name,
      h('button', { type: 'button', class: 'x', 'aria-label': `Remove ${name}`, onclick: () => setLayout({ rows: L.rows.filter((_, j) => j !== i) }) }, 'x'))),
    dimSelect('', (v) => v && setLayout({ rows: [...L.rows, v] }), [L.panel, L.column, ...L.rows]));
    rows.lastChild.firstChild.textContent = '+ row';
    const opt = (key, label, choices) => {
      const s = h('select', {}, ...choices.map(([v, t]) => h('option', { value: String(v) }, t)));
      s.value = String(L[key]);
      s.addEventListener('change', () => setLayout({ [key]: typeof L[key] === 'number' ? Number(s.value) : s.value === 'true' }));
      return h('label', {}, label, s);
    };
    const metrics = metricList().map((m, i) => h('span', { class: 'rs-chip metric', title: m.key },
      h('span', { class: 'sw m' + (i + 1) }), h('button', { type: 'button', class: 'lbl', title: 'Rename', onclick: () => renameMetric(i) }, m.label),
      h('button', { type: 'button', class: 'x', 'aria-label': `Remove ${m.label}`, onclick: () => saveMetrics(metricList().filter((_, j) => j !== i)) }, 'x')));
    const links = C.links || [];
    const data = h('div', { class: 'rs-controls rs-box' },
      h('div', { class: 'rs-box-head' }, h('b', {}, 'Data'), h('span', { class: 'hint' }, 'The runs of this selection: the MLflow filter, the values kept below, and what each run is (dimensions, links)')),
      h('div', { class: 'rs-line' },
        h('label', { class: 'grow', title: 'MLflow search filter, applied by MLflow to the runs of the experiment' }, 'Filter', filter),
        h('button', { type: 'button', class: 'ghost', onclick: applyFilter }, 'Apply'),
        h('button', { type: 'button', class: 'ghost', onclick: linksDialog, title: 'Runs that use another run (a checkpoint): which parameter names it, and what differs between those runs' },
          `Links${links.length ? ` (${links.map((l) => l.name).join(', ')})` : ''}...`),
        h('button', { type: 'button', class: 'ghost', onclick: dimensionsDialog, title: 'What each dimension is, and the MLflow filter of each of its values' }, 'Dimensions...')),
      valueChips());
    const shows = h('div', { class: 'rs-show' },
      h('span', { class: 'rs-key' }, 'Show'),
      h('span', { class: 'seg' }, ...[['table', 'Table'], ['plot', 'Plot'], ['runs', 'Runs']].map(([v, t]) =>
        h('button', { type: 'button', 'aria-pressed': String(R.view === v), onclick: () => setView(v) }, t))),
      h('span', { class: 'rs-key rs-views-k', title: 'Other figures of the same runs, each with its own settings' }, 'Views'),
      h('span', { class: 'seg' }, ...C.views.map((v) => h('button', { type: 'button', 'aria-pressed': String(R.view === 'v:' + v.id), onclick: () => setView('v:' + v.id) }, v.title))),
      h('button', { type: 'button', class: 'tiny', title: 'Add a view: another figure of the same runs (training curves), with its own settings', onclick: addView }, '+ View'));
    if (R.view.startsWith('v:')) return h('div', {}, data, shows);
    return h('div', {}, data, h('div', { class: 'rs-controls rs-box' },
      h('div', { class: 'rs-box-head' }, h('b', {}, 'Arrangement'), h('span', { class: 'hint' }, 'The table and the plot: panels, columns and rows are dimensions')),
      h('div', { class: 'rs-line' },
        h('label', {}, 'Panels', dimSelect(L.panel, (v) => setLayout({ panel: v }), [L.column, ...L.rows])),
        h('label', {}, 'Columns', dimSelect(L.column, (v) => setLayout({ column: v }), [L.panel, ...L.rows])),
        h('label', {}, 'Rows', rows),
        h('span', { class: 'sp' }),
        opt('scale', 'Scale ', [[100, 'x100'], [1, 'x1']]),
        opt('digits', 'Digits ', [[0, '0'], [1, '1'], [2, '2'], [3, '3']]),
        opt('std', '', [[true, 'Mean ± std'], [false, 'Mean']]),
        opt('missing', '', [[true, 'All values'], [false, 'Values with runs']])),
      h('div', { class: 'rs-line' }, h('span', { class: 'rs-key' }, 'Metrics'), ...metrics,
        h('button', { type: 'button', class: 'tiny', onclick: addMetric }, '+ Metric'))), shows);
  }

  /* ---------- selections: several ways to look at the runs of the XP ---------- */

  function selectionStrip() {
    const C = conf();
    const go = (id) => { R.sel = id; R.data = null; R.out = {}; load(false); };
    const edit = (copy) => openDialog({
      title: copy === null ? `Rename the Selection "${C.selection_title}"` : 'New Selection',
      fields: [{ key: 'title', label: 'Title', value: copy === null ? C.selection_title : '' },
        ...(copy === null ? [] : [{ key: 'from', type: 'select', label: 'Start from', value: 'copy',
          options: [{ value: 'copy', label: `A copy of "${C.selection_title}" (filter, values kept, rows/columns, metrics, views)` }, { value: 'empty', label: 'The defaults' }] }])],
      submitLabel: copy === null ? 'Rename' : 'Add',
      onSubmit: async (v) => {
        const title = v.title.trim();
        if (!title) throw new Error('A title is needed');
        let newId = null;
        await commit((c) => {
          const x = c.xps.find((y) => y.name === st.xp);
          const cur = selectionIn(x);
          if (copy === null) { cur.title = title; return; }
          const sels = x.results.selections;
          let id = slug(title), i = 2;
          while (sels.some((y) => y.id === id)) id = `${slug(title)}_${i++}`;
          newId = id;
          sels.push(v.from === 'copy' ? { ...clone(cur), id, title } : { id, title });
        });
        if (newId) go(newId); else load(false);
      },
    });
    const del = () => commit((c) => {
      const x = c.xps.find((y) => y.name === st.xp);
      x.results.selections = (x.results.selections || []).filter((y) => y.id !== C.selection);
    }, { deleted: `selection "${C.selection_title}"` }).then(() => go(null)).catch((e) => status(e.message, true));
    return h('div', { class: 'rs-sels' }, h('span', { class: 'rs-key' }, 'Selection'),
      ...C.selections.map((x) => h('button', { type: 'button', class: 'rs-sel', 'aria-pressed': String(x.id === C.selection), onclick: () => go(x.id) }, x.title)),
      h('button', { type: 'button', class: 'tiny', title: 'Another way to look at the runs: its own filter, values, rows/columns, metrics and views', onclick: () => edit(true) }, '+ Selection'),
      h('span', { class: 'sp' }),
      h('button', { type: 'button', class: 'tiny', onclick: () => edit(null) }, 'Rename'),
      C.selections.length > 1 ? h('button', { type: 'button', class: 'tiny danger', onclick: del }, 'Delete') : '');
  }

  /* ---------- links: runs that use another run (a checkpoint) ---------- */

  function useAxis(name, column, where) {
    const C = conf(), L = lay();
    const dims = C.dimensions.some((d) => d.name === name) ? C.dimensions : [...C.dimensions, { name, param: column }];
    const layout = { ...L, rows: L.rows.filter((r) => r !== name), panel: L.panel === name ? '' : L.panel, column: L.column === name ? '' : L.column };
    if (where === 'rows') layout.rows = [...layout.rows, name]; else layout[where] = name;
    const patch = { layout };
    if (dims !== C.dimensions) patch.dimensions = dims;
    if (R.view.startsWith('v:') || R.view === 'runs') R.view = 'plot';
    return saveResults(patch);
  }

  function linksDialog() {
    const C = conf(), D = R.data;
    let draft = clone(C.links || []);
    let d = $('#linksdlg');
    if (!d) { d = h('dialog', { id: 'linksdlg', class: 'wide linksdlg', 'aria-labelledby': 'links-title' }); document.body.append(d); }
    const params = [...new Set([...(C.id_params || []), ...(D.params || [])])];
    const body = h('div', {});
    const info = (name) => (D.link_info || []).find((x) => x.name === name);
    const paintBody = () => {
      body.replaceChildren(
        ...draft.map((l, i) => {
          const name = h('input', { type: 'text', value: l.name, class: 'mono', 'aria-label': 'Link name', size: '8' });
          name.addEventListener('input', () => { l.name = name.value.trim(); });
          const param = h('select', { 'aria-label': 'Parameter holding the run id' },
            h('optgroup', { label: 'Parameters that hold run ids' }, ...(C.id_params || []).map((x) => h('option', { value: x }, x))),
            h('optgroup', { label: 'Other parameters' }, ...params.filter((x) => !(C.id_params || []).includes(x)).map((x) => h('option', { value: x }, x))));
          param.value = l.param;
          param.addEventListener('change', () => { l.param = param.value; });
          const inf = info(l.name);
          const stat = inf && inf.param === l.param
            ? `${inf.found}/${inf.runs} runs linked, to ${inf.parents} run${inf.parents > 1 ? 's' : ''} of ${Object.keys(inf.experiments).join(', ') || '?'}`
              + (inf.with_id > inf.found ? ` (${inf.with_id - inf.found} run ids not found in MLflow)` : '')
            : 'Saved and read again when you Save';
          const axes = inf && inf.param === l.param ? inf.axes : [];
          return h('div', { class: 'lk-item' },
            h('div', { class: 'lk-row' }, h('label', {}, 'Name', name), h('label', { class: 'grow' }, 'Parameter that holds the run id', param),
              h('button', { type: 'button', class: 'tiny danger', onclick: () => { draft.splice(i, 1); paintBody(); } }, 'Remove')),
            h('p', { class: 'hint' }, stat),
            axes.length ? h('div', { class: 'lk-axes' }, h('div', { class: 'lk-axes-k' }, `What differs between the linked runs (columns ${l.name}.axis.*): put one in the table and the plot`),
              ...axes.map((a) => h('div', { class: 'lk-axis' }, h('b', {}, `${l.name} ${a.name}`),
                h('span', { class: 'lk-vals' }, Object.entries(a.values).map(([v, n]) => `${v} (${n})`).join(' · ')),
                ...[['rows', '+ Rows'], ['column', 'Columns'], ['panel', 'Panels']].map(([w, t]) => h('button', { type: 'button', class: 'tiny',
                  onclick: async () => { d.close(); await useAxis(`${l.name} ${a.name}`, a.column, w); } }, t))))) : '');
        }),
        h('button', { type: 'button', class: 'tiny', onclick: () => { draft.push({ name: draft.some((x) => x.name === 'ckpt') ? `link${draft.length + 1}` : 'ckpt', param: (C.id_params || [])[0] || params[0] || '' }); paintBody(); } }, '+ Link'));
    };
    paintBody();
    d.replaceChildren(h('div', { class: 'df-wrap' },
      h('div', { class: 'pk-head' }, h('h3', { id: 'links-title' }, 'Links Between Runs'), h('span', { class: 'sp' }),
        C.links_saved ? h('button', { type: 'button', class: 'ghost', title: 'Forget the saved links: detect them from the runs again', onclick: async () => { d.close(); await saveResults({ links: undefined }); } }, 'Detect Again') : '',
        h('button', { type: 'button', class: 'primary', onclick: async () => { d.close(); await saveResults({ links: draft.filter((l) => l.name && l.param) }); } }, 'Save'),
        h('button', { type: 'button', class: 'ghost', onclick: () => d.close() }, 'Close')),
      h('p', { class: 'dlg-note' }, 'A link joins a run with the run it uses, e.g. the training run of its checkpoint: a parameter of the run holds the MLflow run id of the other run '
        + '(…/checkpoints/<experiment>/<run id>/last.ckpt). The linked run\'s params, metrics and tags become columns <name>.params.…, <name>.metrics.…, usable in Dimensions and rules '
        + '(ckpt.params.task.epochs = \'10\'), and what differs between the linked runs becomes the dimensions "<name> <what>". '
        + (C.links_saved ? 'These links are saved for this XP.' : 'These links were detected from the runs (a parameter that holds run ids); Save keeps them as they are.')),
      body));
    d.showModal();
  }

  function setView(v) {
    R.view = v;
    try { localStorage.setItem('xp-results-view', v); } catch (e) { /* storage unavailable */ }
    paint();
  }

  /* ---------- the text that explains each view (editable, saved per XP) ---------- */

  const README = {
    table: 'Mean ± standard deviation of each metric over the runs of each configuration: rows, columns and panels are '
      + 'dimensions (Dimensions...). n is the number of runs of a cell (hover it); bold: the best mean of the column '
      + 'within a panel. Runs: those of the MLflow filter, without the values left out with the chips below.',
    plot: 'One dot per metric for each row: the mean over the runs of that configuration, and its line ± one standard '
      + 'deviation (when n > 1). One figure per panel value, one plot per column value. The script draws the same with '
      + 'matplotlib (PDF for the paper).',
    runs: 'Every run behind the table and the plots, grouped by configuration (its value on each dimension). Unmatched: '
      + 'runs that match no value of a dimension. Filtered out: runs of the experiment the Filter leaves out, grouped by '
      + 'the condition they fail. Block options that no run matches are listed and can be deleted.',
  };

  function curvesReadme(v) {
    const src = v.source ? `of the runs linked by "${v.source}" (e.g. the training of the checkpoint each run uses; a linked run counts once per group)` : 'of the runs';
    return `History of ${v.metrics.length ? v.metrics.join(', ') : '(no metric yet)'} along the ${v.x}, ${src}. `
      + `One line per value of ${v.color || '(one group)'}${v.panel ? `, one column per value of ${v.panel}` : ''}: the mean over the runs of the group at each ${v.x}`
      + `${v.smooth ? `, after an exponential moving average (${v.smooth})` : ''}${v.std ? '; the band is ± one standard deviation' : ''}.`;
  }

  function readmeBox() {
    const C = conf(), key = R.view;
    const view = curView();
    const custom = C.readme?.[key];
    const text = custom || (view ? (view.type === 'artifact' ? artifactReadme(view) : curvesReadme(view)) : README[key]) || '';
    const edit = () => openDialog({
      title: `What This View Shows (${view ? view.title : key})`,
      wide: true,
      fields: [{ key: 'text', type: 'area', rows: 8, label: 'Explanation shown above the view (empty: the default text)', value: custom || text }],
      onSubmit: async (v) => {
        const readme = { ...(C.readme || {}) };
        if (v.text.trim() && v.text.trim() !== (view ? curvesReadme(view) : README[key])) readme[key] = v.text.trim(); else delete readme[key];
        await saveResults({ readme });
      },
    });
    return h('div', { class: 'rs-readme' }, h('span', { class: 'rs-readme-k' }, 'About this view'),
      h('p', {}, text), h('button', { type: 'button', class: 'tiny', title: custom ? 'Your text' : 'Default text: edit to write your own', onclick: edit }, custom ? 'Edit' : 'Edit (default text)'));
  }

  /* ---------- views (more figures): training curves ---------- */

  const curView = () => (R.view.startsWith('v:') ? conf().views.find((v) => 'v:' + v.id === R.view) : null);
  const metricsOf = (source) => (source ? (R.data.link_metrics || {})[source] || [] : R.data.available_metrics || []);

  function addView() {
    const C = conf();
    const links = C.links || [];
    const own = metricsOf('').filter((k) => /^train\//.test(k));
    const linked = links.map((l) => [l.name, metricsOf(l.name).filter((k) => /^train\//.test(k))]).find(([, ks]) => ks.length);
    const source = own.length || !linked ? '' : linked[0];
    openDialog({
      title: 'Add a View',
      fields: [
        { key: 'title', label: 'Title', value: 'Training curves' },
        { key: 'type', type: 'select', label: 'Kind', value: 'curves', options: [{ value: 'curves', label: 'Training curves: metric histories, averaged per group' },
          { value: 'artifact', label: 'An artifact of each run (a PDF plot, an image, a config...), side by side' }] },
        { key: 'source', type: 'select', label: 'Runs', value: source,
          options: [{ value: '', label: `The runs of ${C.experiment}` }, ...links.map((l) => ({ value: l.name, label: `The runs linked by ${l.name} (${l.param})` }))] },
        { type: 'note', label: 'Training curves: the metrics, groups (color, panels) and smoothing are set in the view. Artifact: the file is chosen in the view, among the artifacts of the runs (Runs: the runs of the selection). Each view has its own text explaining it, shown above it.' },
      ],
      submitLabel: 'Add',
      onSubmit: async (v) => {
        const firstTrain = metricsOf(v.source).find((k) => /^train\//.test(k));
        let id = slug(v.title || 'view'), i = 2;
        while (C.views.some((x) => x.id === id)) id = `${slug(v.title || 'view')}_${i++}`;
        const multi = C.dimensions.filter((d) => (R.data.order[d.name] || []).length > 1).map((d) => d.name);
        const title = v.title && !(v.type === 'artifact' && v.title === 'Training curves') ? v.title : (v.type === 'artifact' ? 'Artifact' : id);
        if (v.type === 'artifact') {
          id = slug(title); i = 2;
          while (C.views.some((x) => x.id === id)) id = `${slug(title)}_${i++}`;
          await saveResults({ views: [...C.views, { id, type: 'artifact', title, path: '', group: multi[0] || '', size: 'm' }] });
        } else {
          await saveResults({ views: [...C.views, { id, type: v.type, title: v.title || id, metrics: firstTrain ? [firstTrain] : [], x: 'step', source: v.source,
            color: multi[0] || '', panel: '', smooth: 0.6, std: true, logy: false }] });
        }
        setView('v:' + id);
      },
    });
  }

  const saveView = (patch) => {
    const v = curView();
    return saveResults({ views: conf().views.map((x) => (x.id === v.id ? { ...x, ...patch } : x)) });
  };

  function curvesControls(v) {
    const C = conf();
    const sel = (value, choices, onChange) => {
      const s = h('select', {}, ...choices.map(([x, t]) => h('option', { value: String(x) }, t)));
      s.value = String(value);
      s.addEventListener('change', () => onChange(s.value));
      return s;
    };
    const title = h('input', { type: 'text', value: v.title, class: 'rs-title-in' });
    title.addEventListener('change', () => saveView({ title: title.value.trim() || v.id }));
    const metrics = v.metrics.map((k, i) => h('span', { class: 'rs-chip metric' }, h('span', { class: 'sw m' + ((i % 6) + 1) }), k,
      h('button', { type: 'button', class: 'x', 'aria-label': `Remove ${k}`, onclick: () => saveView({ metrics: v.metrics.filter((_, j) => j !== i) }) }, 'x')));
    const add = () => openChooser({
      title: 'Add a Metric (its history is plotted)',
      load: async () => ({ note: v.source ? `Metrics of the runs linked by ${v.source}` : `Metrics logged by the runs of ${C.experiment}`,
        items: metricsOf(v.source).filter((k) => !k.startsWith('system/')).map((k) => ({ value: k, label: k, group: k.split('/')[0] })) }),
      onPick: (k) => saveView({ metrics: [...v.metrics, k] }),
    });
    const del = () => commit((c) => {
      const s = selectionIn(c.xps.find((y) => y.name === st.xp));
      s.views = (s.views || []).filter((w) => w.id !== v.id);
    }, { deleted: `view "${v.title}"` }).then(() => { R.view = 'table'; load(false); }).catch((e) => status(e.message, true));
    return h('div', { class: 'rs-controls' },
      h('div', { class: 'rs-line' },
        h('label', {}, 'Title', title),
        h('label', {}, 'Runs', sel(v.source, [['', 'The runs'], ...(C.links || []).map((l) => [l.name, `Linked by ${l.name}`])], (x) => saveView({ source: x }))),
        h('label', {}, 'x', sel(v.x, [['step', 'step'], ['epoch', 'epoch']], (x) => saveView({ x }))),
        h('label', {}, 'Color', dimSelect(v.color, (x) => saveView({ color: x }), [v.panel])),
        h('label', {}, 'Panels', dimSelect(v.panel, (x) => saveView({ panel: x }), [v.color])),
        h('span', { class: 'sp' }),
        h('label', {}, 'Smooth ', sel(v.smooth, [[0, 'none'], [0.5, '0.5'], [0.6, '0.6'], [0.8, '0.8'], [0.9, '0.9'], [0.95, '0.95']], (x) => saveView({ smooth: Number(x) }))),
        sel(v.std, [[true, 'Mean ± std'], [false, 'Mean']], (x) => saveView({ std: x === 'true' })),
        sel(v.logy, [[false, 'linear y'], [true, 'log y']], (x) => saveView({ logy: x === 'true' })),
        h('button', { type: 'button', class: 'tiny danger', onclick: del }, 'Delete View')),
      h('div', { class: 'rs-line' }, h('span', { class: 'rs-key' }, 'Metrics'), ...metrics, h('button', { type: 'button', class: 'tiny', onclick: add }, '+ Metric')));
  }

  /* ---------- artifact views: one file of each run (a PDF plot, an image, a text), side by side ---------- */

  const ART = { lists: {}, texts: {} };            // selection key -> the artifacts of its runs; text artifacts read
  const selKey = () => JSON.stringify(selq());
  const artUrl = (run, path, png) => `/api/report/artifact?sel=${encodeURIComponent(JSON.stringify(selq()))}&run=${encodeURIComponent(run)}`
    + `&path=${encodeURIComponent(path)}${png ? '&as=png' : ''}`;
  const extOf = (p) => (p.match(/\.([A-Za-z0-9]+)$/) || [, ''])[1].toLowerCase();
  const IMG = ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'];
  const TEXT = ['yaml', 'yml', 'json', 'csv', 'tsv', 'txt', 'log', 'md', 'py', 'tex'];
  const fmtSize = (n) => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);

  function artifactList(fresh = false) {
    const key = selKey();
    if (!ART.lists[key] || fresh) {
      ART.lists[key] = { pending: true };
      api('/api/report/artifacts', { sel: selq(), fresh }).then((r) => { ART.lists[key] = r; }).catch((e) => { ART.lists[key] = { error: e.message }; })
        .finally(() => { if (selKey() === key) paint(); });
    }
    return ART.lists[key];
  }

  function artifactReadme(v) {
    return v.path ? `The artifact ${v.path} of each run, ${v.group ? `one row per ${v.group}, ` : ''}labelled by the other dimensions. A PDF shows its first page: click it for the whole file.`
      : 'Choose the artifact to show (File), among the files the runs of this selection logged.';
  }

  function artifactControls(v) {
    const list = artifactList();
    const title = h('input', { type: 'text', value: v.title, class: 'rs-title-in' });
    title.addEventListener('change', () => saveView({ title: title.value.trim() || v.id }));
    const size = h('select', {}, ...[['s', 'small'], ['m', 'medium'], ['l', 'large']].map(([x, t]) => h('option', { value: x }, t)));
    size.value = v.size || 'm';
    size.addEventListener('change', () => saveView({ size: size.value }));
    const pick = () => openChooser({
      title: 'Artifact to Show for Each Run',
      current: v.path,
      load: async () => {
        const r = await api('/api/report/artifacts', { sel: selq() });
        ART.lists[selKey()] = r;
        return { note: `The files of ${r.runs} run(s)` + (r.total_runs > r.runs ? ` (the first ${r.runs} of ${r.total_runs})` : '')
            + (r.unreadable ? `; ${r.unreadable} run(s) whose files are not reachable from here` : '') + '. The number: how many runs have it.',
          items: r.paths.map((x) => ({ value: x.path, label: x.path, sub: `${x.runs} run(s) · up to ${fmtSize(x.size)}`, group: x.path.includes('/') ? x.path.split('/').slice(0, -1).join('/') : '(top)', tags: [extOf(x.path) || 'file'] })) };
      },
      onPick: (path) => saveView({ path }),
    });
    const del = () => commit((c) => {
      const s = selectionIn(c.xps.find((y) => y.name === st.xp));
      s.views = (s.views || []).filter((w) => w.id !== v.id);
    }, { deleted: `view "${v.title}"` }).then(() => { R.view = 'table'; load(false); }).catch((e) => status(e.message, true));
    const found = list.paths?.find((x) => x.path === v.path);
    return h('div', { class: 'rs-controls' },
      h('div', { class: 'rs-line' },
        h('label', {}, 'Title', title),
        h('span', { class: 'rs-key' }, 'File'), v.path ? h('code', {}, v.path) : h('span', { class: 'hint' }, 'None yet'),
        h('button', { type: 'button', class: 'tiny' + (v.path ? '' : ' primary'), onclick: pick }, v.path ? 'Change' : 'Choose a file'),
        found ? h('span', { class: 'hint' }, `${found.runs} of ${list.runs} run(s) have it`) : '',
        h('label', {}, 'Rows', dimSelect(v.group, (x) => saveView({ group: x }))),
        h('label', {}, 'Size', size),
        h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'tiny', title: 'List the files of the runs again (new artifacts)', onclick: () => { ART.texts = {}; artifactList(true); paint(); } }, 'Reload Files'),
        h('button', { type: 'button', class: 'tiny danger', onclick: del }, 'Delete View')));
  }

  function artifactCell(v, r) {
    const ext = extOf(v.path);
    const open = artUrl(r.id, v.path, false);
    if (ext === 'pdf' || IMG.includes(ext)) {
      const img = h('img', { src: artUrl(r.id, v.path, ext === 'pdf'), alt: `${v.path} of ${r.name || r.id}`, loading: 'lazy' });
      const box = h('a', { href: open, target: '_blank', rel: 'noopener', class: 'rs-art-img', title: 'Open the whole file' }, img);
      img.addEventListener('error', () => fetch(img.src).then((x) => x.text()).then((t) => box.replaceWith(h('p', { class: 'dlg-err' }, t.slice(0, 300)))).catch(() => {}));
      return box;
    }
    if (TEXT.includes(ext)) {
      const key = r.id + '\u0000' + v.path;
      const pre = h('pre', { class: 'rs-art-text' }, ART.texts[key] ?? 'reading...');
      if (ART.texts[key] === undefined) {
        fetch(open).then(async (x) => { const t = await x.text(); return x.ok ? t : Promise.reject(new Error(t)); })
          .then((t) => { ART.texts[key] = t.length > 20000 ? t.slice(0, 20000) + '\n[...]' : t; pre.textContent = ART.texts[key]; })
          .catch((e) => { pre.textContent = e.message; pre.classList.add('dlg-err'); });
      }
      return pre;
    }
    return h('a', { href: open, target: '_blank', rel: 'noopener', class: 'tiny btnlink' }, `Open ${v.path.split('/').pop()}`);
  }

  function artifactGrid(v) {
    if (!v.path) return h('p', { class: 'empty' }, 'Choose the file to show for each run: File > Choose a file.');
    const list = artifactList();
    if (list.pending) return h('p', { class: 'empty' }, 'Listing the files of the runs...');
    if (list.error) return h('pre', { class: 'dlg-err' }, list.error);
    const have = new Set(list.paths.find((x) => x.path === v.path)?.run_ids || []);
    const dims = conf().dimensions, D = R.data;
    const others = dims.filter((d) => d.name !== v.group);
    const rank = (d, x) => { const i = (D.order[d.name] || []).indexOf(x); return i < 0 ? 1e9 : i; };
    const runs = [...D.runs].sort((a, b) => {
      for (const d of [...dims.filter((x) => x.name === v.group), ...others]) { const x = rank(d, a.labels[d.name]) - rank(d, b.labels[d.name]); if (x) return x; }
      return 0;
    });
    const groups = new Map();
    for (const r of runs) {
      const g = v.group ? r.labels[v.group] ?? '' : '';
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(r);
    }
    const missing = runs.filter((r) => !have.has(r.id)).length;
    const card = (r) => h('figure', { class: 'rs-art-card' + (have.has(r.id) ? '' : ' missing') },
      h('figcaption', {}, ...others.map((d) => h('span', { class: 'rs-art-lab', title: d.name }, String(r.labels[d.name] ?? ''))),
        h('a', { class: 'hint', href: window.XPRouter?.href({ tab: 'mlflow', mlflow: `/experiments/${D.experiment_id}/runs/${r.id}` }) || '#', title: 'Open the run in the MLflow tab' }, r.name || r.id.slice(0, 8))),
      have.has(r.id) ? artifactCell(v, r) : h('p', { class: 'hint' }, `No ${v.path.split('/').pop()} in this run`));
    return h('div', { class: 'rs-art ' + (v.size || 'm') },
      missing ? h('p', { class: 'hint' }, `${runs.length - missing} of ${runs.length} run(s) have ${v.path}` + (list.total_runs > list.runs ? ` (files listed for the first ${list.runs} runs)` : '') + '.') : '',
      ...[...groups.entries()].map(([g, rs]) => h('section', { class: 'rs-art-group' },
        v.group ? h('h4', {}, h('small', {}, v.group + ' '), String(g)) : '',
        h('div', { class: 'rs-art-grid' }, ...rs.map(card)))));
  }

  /* the curves of the preview, drawn here from the data the script also computes */
  const PALETTE = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'];
  function ticks(lo, hi, n = 5) {
    if (!(hi > lo)) return [lo];
    const step0 = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(step0));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || step0;
    const out = [];
    for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-12; t += step) out.push(+t.toPrecision(10));
    return out;
  }

  function curvesPreview(v) {
    const key = JSON.stringify(v) + R.data.script;
    let e = R.out['curves:' + v.id];
    if (!e || e.key !== key) {
      e = { key, pending: true };
      R.out['curves:' + v.id] = e;
      api('/api/report/curves', { sel: selq(), view: v.id }).then((r) => Object.assign(e, { pending: false, data: r }))
        .catch((x) => Object.assign(e, { pending: false, error: x.message })).finally(() => { if (R.out['curves:' + v.id] === e) paint(); });
    }
    if (!v.metrics.length) return h('p', { class: 'empty' }, 'Add a metric (+ metric) to draw its history.');
    if (e.pending) return h('p', { class: 'empty' }, 'Reading the metric histories from MLflow...');
    if (e.error) return h('pre', { class: 'dlg-err' }, e.error);
    const d = e.data;
    const labels = [...new Set(d.panels.flatMap((p) => p.charts.flatMap((c) => c.series.map((s) => s.label))))];
    const color = (l) => PALETTE[labels.indexOf(l) % PALETTE.length];
    const out = [h('div', { class: 'rs-legend' }, ...labels.map((l) => h('span', {}, h('span', { class: 'sw', style: `background:${color(l)}` }), l)))];
    const total = $('#results')?.clientWidth || 1200;
    const pane = (window.matchMedia('(max-width: 1200px)').matches ? total : (total - 16) / 2) - 26;
    const tip = $('#rs-tip') || document.body.appendChild(h('div', { id: 'rs-tip', class: 'rs-tip', role: 'tooltip', hidden: '' }));
    for (const p of d.panels) {
      if (p.value !== '') out.push(h('h3', {}, `${v.panel}: ${p.value}`));
      for (const c of p.charts) {
        const W = Math.max(320, pane), H = 230, L = 56, T = 10, B = 30, Rm = 10;
        const pts = c.series.flatMap((s) => s.points);
        if (!pts.length) { out.push(h('p', { class: 'hint' }, `${c.metric}: no history for these runs`)); continue; }
        const ys = pts.flatMap(([, m, sd]) => (v.std && sd != null ? [m - sd, m + sd] : [m])).filter((y) => !v.logy || y > 0);
        const xs = pts.map(([x]) => x);
        let [y0, y1] = [Math.min(...ys), Math.max(...ys)];
        if (y0 === y1) { y0 -= 1; y1 += 1; }
        const [x0, x1] = [Math.min(...xs), Math.max(...xs) || 1];
        const ly = (y) => (v.logy ? Math.log10(Math.max(y, 1e-12)) : y);
        const X = (x) => L + ((x - x0) / ((x1 - x0) || 1)) * (W - L - Rm);
        const Y = (y) => T + (1 - (ly(y) - ly(y0)) / ((ly(y1) - ly(y0)) || 1)) * (H - T - B);
        const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, width: String(W), height: String(H), class: 'rs-plot rs-curves', role: 'img', 'aria-label': `${c.metric} along the ${d.x}` });
        const yt = v.logy ? ticks(ly(y0), ly(y1)).map((t) => 10 ** t) : ticks(y0, y1);
        for (const t of yt) {
          svg.append(sv('line', { x1: String(L), x2: String(W - Rm), y1: String(Y(t)), y2: String(Y(t)), class: 'grid' }));
          svg.append(sv('text', { x: String(L - 6), y: String(Y(t) + 4), 'text-anchor': 'end', class: 'tick' }, String(+t.toPrecision(3))));
        }
        for (const t of ticks(x0, x1, 6)) svg.append(sv('text', { x: String(X(t)), y: String(H - B + 16), 'text-anchor': 'middle', class: 'tick' }, String(t)));
        svg.append(sv('text', { x: String(W - Rm), y: String(H - 2), 'text-anchor': 'end', class: 'tick' }, d.x));
        svg.append(sv('text', { x: String(L + 4), y: String(T + 10), class: 'facet', 'text-anchor': 'start' }, c.metric));
        for (const s of c.series) {
          const col = color(s.label);
          if (v.std && s.points.some((q) => q[2] != null)) {
            const up = s.points.map(([x, m, sd]) => `${X(x)},${Y(m + (sd || 0))}`), down = s.points.map(([x, m, sd]) => `${X(x)},${Y(m - (sd || 0))}`).reverse();
            svg.append(sv('polygon', { points: [...up, ...down].join(' '), fill: col, 'fill-opacity': '0.15', stroke: 'none' }));
          }
          const line = sv('polyline', { points: s.points.map(([x, m]) => `${X(x)},${Y(m)}`).join(' '), fill: 'none', stroke: col, 'stroke-width': '1.6', tabindex: '0' });
          const last = s.points[s.points.length - 1];
          const text = `${s.label} (n=${s.runs})\n${c.metric} at ${d.x} ${last[0]}: ${last[1].toPrecision(4)}${last[2] != null ? ' ± ' + last[2].toPrecision(3) : ''}`;
          const show = (ev) => { tip.textContent = text; tip.hidden = false; tip.style.left = `${ev.pageX ?? 0}px`; tip.style.top = `${(ev.pageY ?? 0) - 10}px`; };
          line.addEventListener('mousemove', show); line.addEventListener('mouseleave', () => { tip.hidden = true; });
          svg.append(line);
        }
        const wrap = h('div', { class: 'rs-scroll zoomable', title: 'Click to see it large' }, svg);
        wrap.addEventListener('click', () => zoom(svg.cloneNode(true), `${v.title}: ${c.metric}${p.value !== '' ? ' - ' + p.value : ''}`));
        out.push(wrap);
      }
    }
    return h('div', { class: 'rs-figs' }, ...out);
  }

  /* chips of the values of each dimension: run counts; click to leave a value out */
  function valueChips() {
    const L = lay(), D = R.data;
    const groups = [...new Set(conf().dimensions.map(dimGroup))];
    const kids = conf().dimensions.flatMap((d, i, all) => {
      const hidden = new Set(L.hide[d.name] || []);
      const vals = D.order[d.name] || [];
      const head = groups.length > 1 && (i === 0 || dimGroup(all[i - 1]) !== dimGroup(d)) ? [h('div', { class: 'rs-group-k' }, dimGroup(d))] : [];
      return [...head, h('div', { class: 'rs-filter' }, h('span', { class: 'rs-key', title: d.block ? `From the block ${blk(d.block)?.label || d.block}` : d.param || 'rules' }, d.name),
        ...vals.map((v) => {
          const n = D.counts[d.name]?.[v] || 0;
          return h('button', { type: 'button', class: 'chip' + (n ? '' : ' empty') + (v === OTHER ? ' other' : ''), 'aria-pressed': String(!hidden.has(v)),
            title: v === OTHER ? 'Runs that match no value of this dimension' : `${n} run(s); click to leave out / bring back`,
            onclick: () => {
              const now = new Set(hidden);
              if (now.has(v)) now.delete(v); else now.add(v);
              setLayout({ hide: { ...L.hide, [d.name]: [...now] } });
            } }, `${v} `, h('small', {}, String(n)));
        }))];
    });
    return h('details', { class: 'rs-filters', open: '' }, h('summary', {}, 'Values Kept (run counts; click a value to leave it out)'), ...kids);
  }

  /* ---------- metrics ---------- */

  function addMetric() {
    openChooser({
      title: 'Add a Metric',
      load: async () => ({ note: `Metrics logged by the runs of ${conf().experiment}`,
        items: R.data.available_metrics.map((k) => ({ value: k, label: k, group: k.split('/').slice(0, -2).join('/') })) }),
      onPick: (k) => {
        const parts = k.split('/');
        const name = (parts.find((x) => !['untrained', 'test', 'validation', 'train', 'max', 'last'].includes(x)) || k).toUpperCase();
        saveMetrics([...metricList(), { key: k, label: name }]);
      },
    });
  }

  function renameMetric(i) {
    const m = metricList()[i];
    openDialog({ title: `Metric ${m.key}`, fields: [{ key: 'label', label: 'Label (LaTeX allowed when it contains $ or \\, e.g. F$_1$)', value: m.label }],
      onSubmit: async (v) => { if (!v.label.trim()) throw new Error('A label is needed'); await saveMetrics(metricList().map((x, j) => (j === i ? { ...x, label: v.label.trim() } : x))); } });
  }

  /* ---------- dimensions editor ---------- */

  function dimensionsDialog() {
    let draft = JSON.parse(JSON.stringify(conf().dimensions)).map((d) => ({ ...d, _orig: d.name }));
    let test = { counts: R.data.counts, errors: {} };
    const dlgEl = $('#rs-dims') || document.body.appendChild(h('dialog', { id: 'rs-dims', class: 'wide chooser rs-dims-dlg' }));
    const list = h('div', { class: 'rs-dims' });
    const note = h('p', { class: 'hint' });
    let timer = null, seq = 0;
    const retest = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const r = await api('/api/report/preview', { sel: selq(), dimensions: clean(draft) });
          if (mine === seq) { test = { counts: r.counts || {}, errors: r.errors || {} }; paintCounts(); note.textContent = `${r.runs.length} runs from the filter`; }
        } catch (e) { if (mine === seq) note.textContent = e.message; }
      }, 350);
    };
    const clean = (ds) => ds.map(({ _orig, ...d }) => d);
    const paintCounts = () => {
      list.querySelectorAll('[data-count]').forEach((el) => {
        const [dn, vl] = el.dataset.count.split('\u0000');
        const d = draft.find((x) => x.name === dn);
        const n = test.counts[dn]?.[vl];
        el.textContent = n == null ? '' : `${n} run${n === 1 ? '' : 's'}`;
        el.classList.toggle('zero', n === 0);
        const err = test.errors[`${dn}\u0000${vl}`];
        const errEl = el.parentElement.querySelector('.rs-rule-err');
        if (errEl) errEl.textContent = err || '';
        if (d && vl === OTHER) el.textContent = n ? `${n} run${n === 1 ? '' : 's'} match no value` : 'Every run has a value';
      });
    };
    const move = (arr, i, j) => { if (j < 0 || j >= arr.length) return; [arr[i], arr[j]] = [arr[j], arr[i]]; };
    const paintList = () => {
      list.replaceChildren(...draft.map((d, di) => {
        const name = h('input', { type: 'text', class: 'rs-dname', value: d.name, 'aria-label': 'Dimension name' });
        name.value = d.name;
        name.addEventListener('change', () => { d.name = name.value.trim() || d.name; setTimeout(() => { paintList(); retest(); }, 0); });
        const head = h('div', { class: 'rs-dhead' }, name,
          h('span', { class: 'hint' }, d.block ? `From the block ${blk(d.block)?.label || d.block}` : d.param ? 'Values of a parameter' : 'Values defined by MLflow filters'),
          h('span', { class: 'sp' }),
          h('button', { type: 'button', class: 'tiny', 'aria-label': 'Move up', onclick: () => { move(draft, di, di - 1); paintList(); } }, '↑'),
          h('button', { type: 'button', class: 'tiny', 'aria-label': 'Move down', onclick: () => { move(draft, di, di + 1); paintList(); } }, '↓'),
          h('button', { type: 'button', class: 'tiny', onclick: () => { draft.splice(di, 1); paintList(); retest(); } }, 'Remove'));
        if (d.param) {
          const param = h('select', {}, ...R.data.params.map((k) => h('option', { value: k }, k)));
          param.value = d.param.startsWith('params.') ? d.param : 'params.' + d.param;
          param.addEventListener('change', () => { d.param = param.value; retest(); });
          const labels = autoGrow(h('textarea', { class: 'mono', spellcheck: 'false', placeholder: 'Raw value = label (one per line, optional)' }), 2);
          labels.value = Object.entries(d.labels || {}).map(([k, v]) => `${k} = ${v}`).join('\n');
          labels.addEventListener('input', () => {
            d.labels = Object.fromEntries(labels.value.split('\n').map((l) => l.split(' = ')).filter((x) => x.length === 2 && x[0].trim()).map(([k, v]) => [k.trim(), v.trim()]));
            retest();
          });
          return h('div', { class: 'rs-dim' }, head, h('label', { class: 'cert-like' }, 'Parameter', param), h('label', { class: 'cert-like' }, 'Labels', labels),
            h('p', { class: 'hint', 'data-count': `${d.name}\u0000${OTHER}` }));
        }
        const rows = d.values.map((v, vi) => {
          const lab = h('input', { type: 'text', value: v.label, 'aria-label': 'Label' });
          lab.value = v.label;
          lab.addEventListener('change', () => { v.label = lab.value.trim() || v.label; setTimeout(() => { paintList(); retest(); }, 0); });
          const rule = autoGrow(h('textarea', { class: 'mono', spellcheck: 'false', 'aria-label': `MLflow filter for ${v.label}`,
            placeholder: 'Empty: every run (put it last)' }));
          rule.value = v.rule;
          rule.addEventListener('input', () => { v.rule = rule.value; retest(); });
          return h('div', { class: 'rs-val' }, lab, h('div', {}, rule, h('div', { class: 'rs-rule-err' })),
            h('span', { class: 'rs-count', 'data-count': `${d.name}\u0000${v.label}` }),
            h('span', { class: 'rs-val-acts' },
              h('button', { type: 'button', class: 'tiny', 'aria-label': 'Move up', onclick: () => { move(d.values, vi, vi - 1); paintList(); retest(); } }, '↑'),
              h('button', { type: 'button', class: 'tiny', 'aria-label': 'Move down', onclick: () => { move(d.values, vi, vi + 1); paintList(); retest(); } }, '↓'),
              h('button', { type: 'button', class: 'tiny', 'aria-label': 'Remove', onclick: () => { d.values.splice(vi, 1); paintList(); retest(); } }, 'x')));
        });
        return h('div', { class: 'rs-dim' }, head, ...rows,
          h('div', { class: 'rs-dfoot' },
            h('button', { type: 'button', class: 'tiny', onclick: () => { let i = d.values.length + 1; while (d.values.some((x) => x.label === `value ${i}`)) i += 1; d.values.push({ label: `Value ${i}`, rule: '' }); paintList(); retest(); } }, '+ Value'),
            h('span', { class: 'hint', 'data-count': `${d.name}\u0000${OTHER}` })));
      }));
      paintCounts();
    };
    const uniqueName = (base) => { let n = base, i = 2; while (draft.some((d) => d.name === n)) n = `${base} ${i++}`; return n; };
    const save = async () => {
      const L = { ...lay() };
      for (const d of draft) {                          // renamed dimensions: keep their place in the layout
        if (d._orig && d._orig !== d.name) {
          if (L.panel === d._orig) L.panel = d.name;
          if (L.column === d._orig) L.column = d.name;
          L.rows = L.rows.map((r) => (r === d._orig ? d.name : r));
          if (L.hide[d._orig]) { L.hide = { ...L.hide, [d.name]: L.hide[d._orig] }; delete L.hide[d._orig]; }
        }
      }
      dlgEl.close();
      await saveResults({ dimensions: clean(draft), layout: L });
    };
    dlgEl.replaceChildren(h('div', { class: 'ch' },
      h('div', { class: 'pk-head' }, h('h3', {}, 'Dimensions'), note, h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'ghost', onclick: () => dlgEl.close() }, 'Cancel'),
        h('button', { type: 'button', class: 'primary', onclick: save }, 'Save')),
      h('p', { class: 'dlg-note' }, 'A run gets the first value whose filter it matches (MLflow search syntax: params.KEY = \'v\', LIKE \'%x%\', IN (...), IS NULL, >, joined with and). '
        + 'Counts are updated as you type, on the runs of the filter. Dimensions start from the blocks; saved ones no longer follow block changes (Reset to go back).'),
      list,
      h('div', { class: 'dlg-actions' },
        h('button', { type: 'button', class: 'ghost', onclick: () => { draft.push({ name: uniqueName('New dimension'), values: [{ label: 'Value 1', rule: '' }] }); paintList(); retest(); } }, '+ Dimension (filters)'),
        h('button', { type: 'button', class: 'ghost', onclick: () => openChooser({ title: 'Dimension from a Parameter', load: async () => ({ items: R.data.params.map((k) => ({ value: k, label: k })) }),
          onPick: (k) => { draft.push({ name: uniqueName(k.split('.').slice(-1)[0]), param: k, labels: {} }); paintList(); retest(); } }) }, '+ Dimension (parameter values)'),
        h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'danger', title: 'Forget the saved dimensions: use (and follow) the blocks of the XP',
          onclick: async () => { dlgEl.close(); await saveResults({ dimensions: [] }); } }, 'Reset to the Blocks'))));
    paintList();
    dlgEl.showModal();
  }

  /* ---------- left: preview ---------- */

  function previewTable() {
    const s = R.data.summary, M = metricList();
    if (!s || !s.panels.length) return h('p', { class: 'empty' }, 'No run to show.');
    const out = [];
    for (const p of s.panels) {
      const head1 = [...s.rows.map((d) => h('th', { rowspan: '2', class: 'rd' }, d)),
        ...s.columns.map((c) => h('th', { colspan: String(M.length), class: 'grp' }, s.column ? c : ''))];
      const head2 = s.columns.flatMap(() => M.map((m) => h('th', { class: 'num' }, m.label)));
      const body = p.rows.map((row, ri) => h('tr', {},
        ...row.values.map((v, i) => h('td', { class: 'rd' + (ri && i < row.values.length - 1 && p.rows[ri - 1].values.slice(0, i + 1).join('\u0000') === row.values.slice(0, i + 1).join('\u0000') ? ' rep' : '') }, v)),
        ...s.columns.flatMap((c) => M.map((m, mi) => {
          const x = row.cells[c]?.[m.key];
          const b = p.best[`${c}\u0000${m.key}`];
          const best = x && b != null && Math.abs(x.mean - b) < 1e-9;
          return h('td', { class: 'num' + (mi === 0 ? ' first' : '') + (best ? ' best' : '') + (x ? '' : ' missing'), title: x ? `n=${x.n}` : 'No run' },
            ...(x ? [fmt(x.mean), lay().std && x.sd != null ? h('small', {}, ' ±' + fmt(x.sd)) : '', x.n > 1 ? h('sup', {}, String(x.n)) : ''] : ['—']));
        }))));
      out.push(h('div', { class: 'rs-panel' }, s.panel ? h('h3', {}, `${s.panel}: ${p.value}`) : '',
        h('div', { class: 'rs-scroll' }, h('table', { class: 'rs-table' }, h('thead', {}, h('tr', {}, ...head1), h('tr', {}, ...head2)), h('tbody', {}, ...body)))));
    }
    return h('div', {}, ...out);
  }

  /* ---------- right: the script, then what it made ---------- */

  const b64blob = (b64, type) => new Blob([Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))], { type });
  function saveBlob(name, blob) { const a = h('a', { href: URL.createObjectURL(blob), download: name }); document.body.append(a); a.click(); a.remove(); }
  const saveText = (name, text, type) => saveBlob(name, new Blob([text], { type }));

  /* run the script for `what` once per script text; repaint when it is done */
  function runFor(what) {
    const key = R.data.script;
    const cur = R.out[what];
    if (cur && cur.key === key) return cur;
    const entry = { key, pending: true };
    R.out[what] = entry;
    api('/api/report/run', { sel: selq(), what }).then((r) => Object.assign(entry, { pending: false, res: r }))
      .catch((e) => Object.assign(entry, { pending: false, error: e.message })).finally(() => { if (R.out[what] === entry) paint(); });
    return entry;
  }

  function figures(res, pdfs) {
    return res.files.filter((f) => f.name.endsWith('.png')).map((f) => {
      const stem = f.name.replace(/\.png$/, '');
      const pdf = res.files.find((x) => x.name === stem + '.pdf');
      const src = `data:image/png;base64,${f.b64}`;
      const img = h('img', { src, alt: stem, title: 'Click to see it large' });
      const dl = [pdfs && pdf ? ['PDF', () => saveBlob(pdf.name, b64blob(pdf.b64, 'application/pdf'))] : null,
        ['PNG', () => saveBlob(f.name, b64blob(f.b64, 'image/png'))]].filter(Boolean);
      img.addEventListener('click', () => zoom(h('img', { src, alt: stem }), stem, dl));
      return h('figure', { class: 'rs-fig' }, img,
        h('figcaption', {}, stem, ...dl.map(([t, fn]) => h('button', { type: 'button', class: 'tiny', onclick: fn }, t))));
    });
  }

  /* a big view of a table or a figure (click anywhere or Esc to close) */
  function zoom(node, title, downloads = []) {
    const d = $('#rs-zoom') || document.body.appendChild(h('dialog', { id: 'rs-zoom', class: 'rs-zoom', 'aria-label': 'Large view' }));
    const stage = h('div', { class: 'rs-zoom-stage' }, node);
    stage.addEventListener('click', () => d.close());
    d.replaceChildren(h('div', { class: 'rs-zoom-bar' }, h('b', {}, title), h('span', { class: 'sp' }),
      ...downloads.map(([t, fn]) => h('button', { type: 'button', class: 'ghost', onclick: fn }, t)),
      h('button', { type: 'button', class: 'ghost', onclick: () => d.close() }, 'Close')), stage);
    d.showModal();
  }

  function scriptPane(what) {
    const e = runFor(what);
    const stem = conf().stem;
    const kids = [];
    if (e.pending) kids.push(h('p', { class: 'empty rs-running' }, what === 'plot' ? 'Running the script: querying MLflow, then the figures (LaTeX text: about half a minute)...' : 'Running the script: querying MLflow, then the table...'));
    else if (e.error) kids.push(h('pre', { class: 'dlg-err' }, e.error));
    else if (what === 'table') {
      const box = h('div', { class: 'rs-render zoomable', role: 'img', 'aria-label': 'Table made by the script, compiled by LaTeX', title: 'Click to see it large' });
      box.innerHTML = e.res.tex_svg;                 // SVG made by dvisvgm on this server
      box.addEventListener('click', () => {
        const big = h('div', { class: 'rs-zoom-tex' });
        big.innerHTML = e.res.tex_svg;
        zoom(big, `${stem}-table.tex`, [['Download .tex', () => saveText(`${stem}-table.tex`, e.res.tex, 'text/x-tex')]]);
      });
      kids.push(box, h('div', { class: 'rs-actions' },
        h('button', { type: 'button', class: 'ghost', onclick: async () => { try { await navigator.clipboard.writeText(e.res.tex); status('LaTeX copied'); } catch (x) { status('Copy failed: use Download', true); } } }, 'Copy LaTeX'),
        h('button', { type: 'button', class: 'ghost', onclick: () => saveText(`${stem}-table.tex`, e.res.tex, 'text/x-tex') }, 'Download .tex')),
      h('details', { class: 'rs-code' }, h('summary', {}, 'LaTeX Written by the Script'), h('pre', { class: 'rs-script' }, e.res.tex)));
    } else {
      const all = e.res.files.find((f) => f.name.endsWith('-all.pdf'));
      kids.push(h('div', { class: 'rs-actions' }, all ? h('button', { type: 'button', class: 'ghost', onclick: () => saveBlob(all.name, b64blob(all.b64, 'application/pdf')) }, 'Download All (PDF)') : ''),
        ...figures(e.res, true));
    }
    if (e.res?.log) kids.push(h('details', { class: 'rs-code' }, h('summary', {}, 'Output of the Script'), h('pre', { class: 'rs-script' }, e.res.log)));
    const script = h('details', { class: 'rs-code' }, h('summary', {}, `${stem}-report.py (queries MLflow; python ${stem}-report.py [folder] [--table] [--plot])`),
      h('pre', { class: 'rs-script' }, R.data.script));
    script.open = !!e.pending;                       // shown while it runs
    return h('div', { class: 'rs-side' },
      h('div', { class: 'rs-side-head' }, h('h3', {}, what === 'table' ? 'Table made by the script' : 'Figures made by the script'),
        what.startsWith('view:') ? h('span', { class: 'hint' }, `python ${stem}-report.py out --view ${what.slice(5)}`) : '',
        h('span', { class: 'sp' }), h('button', { type: 'button', class: 'ghost', onclick: () => saveText(`${stem}-report.py`, R.data.script, 'text/x-python') }, 'Download the Script')),
      ...kids, script);
  }

  /* the dot plot of the preview, drawn here from the summary (the script draws the same with matplotlib) */
  const NS = 'http://www.w3.org/2000/svg';
  const sv = (tag, attrs = {}, ...kids) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    e.append(...kids);
    return e;
  };

  function previewPlotPane() {
    const s = R.data.summary, M = metricList(), L = lay();
    if (!s || !s.panels.length) return h('p', { class: 'empty' }, 'No run to show.');
    const tip = $('#rs-tip') || document.body.appendChild(h('div', { id: 'rs-tip', class: 'rs-tip', role: 'tooltip', hidden: '' }));
    const max = 1.08 * L.scale, ticks = L.scale === 100 ? [0, 20, 40, 60, 80, 100] : [0, 0.2, 0.4, 0.6, 0.8, 1];
    const label = (vals) => (vals.length ? vals[0] + (vals.length > 1 ? ` [${vals.slice(1).join(', ')}]` : '') : '');
    const out = [h('div', { class: 'rs-legend' }, ...M.map((m, i) => h('span', {}, h('span', { class: 'sw m' + (i + 1) }), m.label.replace(/[$\\{}]/g, ''))),
      h('span', { class: 'hint' }, L.std ? 'line: ± one standard deviation (n > 1)' : ''))];
    for (const p of s.panels) {
      const labels = p.rows.map((r) => label(r.values));
      const cols = s.columns.length ? s.columns : [''];
      const rowH = 22, top = 26, gap = 16;
      // labels: up to 40% of the pane; longer ones are cut at the end (the full text on hover)
      const paneW = (window.matchMedia('(max-width: 1200px)').matches ? ($('#results')?.clientWidth || 1200) : (($('#results')?.clientWidth || 1200) - 16) / 2) - 26;
      const labW = Math.min(Math.max(300, paneW * 0.4), Math.max(70, Math.max(0, ...labels.map((l) => l.length)) * 6.2 + 10));
      const fit = Math.floor((labW - 12) / 6.2);
      const cut = (l) => (l.length > fit ? l.slice(0, Math.max(1, fit - 1)) + '…' : l);
      // fit the preview pane: half of the results width (the script pane takes the other half), or all of it when stacked
      const total = $('#results')?.clientWidth || 1200;
      const pane = (window.matchMedia('(max-width: 1200px)').matches ? total : (total - 16) / 2) - 26;
      const facetW = Math.max(100, (pane - labW - cols.length * gap) / cols.length);
      const W = labW + cols.length * (facetW + gap), H = top + p.rows.length * rowH + 30;
      const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, width: String(W), height: String(H), class: 'rs-plot', role: 'img',
        'aria-label': `${s.panel ? s.panel + ' ' + p.value + ': ' : ''}${M.map((m) => m.label).join(', ')} per configuration` });
      const empty = p.rows.map((r) => !Object.values(r.cells).some((c) => Object.keys(c).length));
      labels.forEach((l, i) => svg.append(sv('text', { x: String(labW - 8), y: String(top + i * rowH + rowH / 2 + 4), 'text-anchor': 'end', class: 'lab' + (empty[i] ? ' none' : '') },
        cut(l), sv('title', {}, l + (empty[i] ? ' (no run)' : '')))));
      cols.forEach((c, ci) => {
        const x0 = labW + ci * (facetW + gap), X = (v) => x0 + (Math.max(0, Math.min(max, v)) / max) * facetW;
        const g = sv('g');
        p.rows.forEach((_, ri) => { if (ri % 2 === 0) g.append(sv('rect', { x: String(x0), y: String(top + ri * rowH), width: String(facetW), height: String(rowH), class: 'band' })); });
        const room = Math.floor(facetW / 7);                    // column titles: cut to the width of the column
        g.append(sv('text', { x: String(x0 + facetW / 2), y: '14', 'text-anchor': 'middle', class: 'facet' },
          s.column ? (c.length > room ? c.slice(0, Math.max(1, room - 1)) + '…' : c) : '', sv('title', {}, c)));
        for (const t of ticks) {
          g.append(sv('line', { x1: String(X(t)), x2: String(X(t)), y1: String(top), y2: String(top + p.rows.length * rowH), class: 'grid' }));
          g.append(sv('text', { x: String(X(t)), y: String(top + p.rows.length * rowH + 14), 'text-anchor': 'middle', class: 'tick' }, String(t)));
        }
        p.rows.forEach((row, ri) => {
          M.forEach((m, mi) => {
            const x = row.cells[c]?.[m.key];
            if (!x) return;
            const y = top + ri * rowH + rowH / 2 + (M.length > 1 ? (mi - (M.length - 1) / 2) * 5 : 0);
            if (L.std && x.sd != null) g.append(sv('line', { x1: String(X(x.mean - x.sd)), x2: String(X(x.mean + x.sd)), y1: String(y), y2: String(y), class: 'err m' + (mi + 1) }));
            const dot = sv('circle', { cx: String(X(x.mean)), cy: String(y), r: '4', class: 'dot m' + (mi + 1), tabindex: '0' });
            const text = `${labels[ri]}${s.column ? ' · ' + c : ''}\n${m.label.replace(/[$\\{}]/g, '')}: ${fmt(x.mean)}${x.sd != null ? ' ± ' + fmt(x.sd) : ''} (n=${x.n})`;
            const show = () => { const b = dot.getBoundingClientRect(); tip.textContent = text; tip.hidden = false; tip.style.left = `${b.left + b.width / 2 + window.scrollX}px`; tip.style.top = `${b.top + window.scrollY - 8}px`; };
            dot.addEventListener('mouseenter', show); dot.addEventListener('focus', show);
            dot.addEventListener('mouseleave', () => { tip.hidden = true; }); dot.addEventListener('blur', () => { tip.hidden = true; });
            g.append(dot);
          });
        });
        svg.append(g);
      });
      const wrap = h('div', { class: 'rs-scroll zoomable', title: 'Click to see it large' }, svg);
      wrap.addEventListener('click', (ev) => { if (ev.target.tagName !== 'circle') zoom(svg.cloneNode(true), `Preview${s.panel ? ' - ' + s.panel + ': ' + p.value : ''}`); });
      out.push(h('div', { class: 'rs-panel' }, s.panel ? h('h3', {}, `${s.panel}: ${p.value}`) : '', wrap));
    }
    return h('div', { class: 'rs-figs' }, ...out);
  }

  /* ---------- runs ---------- */

  let runsShow = 'all';                         // all | unmatched | repeated | excluded

  function runsView() {
    const D = R.data, dims = conf().dimensions, M = metricList();
    const keyOf = (r) => dims.map((d) => r.labels[d.name]).join('\u0000');
    const groupsOf = (list) => {
      const out = new Map();
      for (const r of list) {
        const k = keyOf(r);
        if (!out.has(k)) out.set(k, []);
        out.get(k).push(r);
      }
      return out;
    };
    const excluded = D.excluded || [];
    if (runsShow === 'excluded' && !excluded.length) runsShow = 'all';
    const groups = new Map();
    for (const r of D.runs) {
      const k = keyOf(r);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    const rank = (d, v) => { const i = (D.order[d.name] || []).indexOf(v); return i < 0 ? 1e9 : i; };
    const sorted = [...groups.entries()].sort(([, a], [, b]) => {
      for (const d of dims) { const x = rank(d, a[0].labels[d.name]) - rank(d, b[0].labels[d.name]); if (x) return x; }
      return 0;
    });
    const unmatched = D.runs.filter((r) => dims.some((d) => r.labels[d.name] === OTHER));
    const byDim = dims.map((d) => [d.name, D.runs.filter((r) => r.labels[d.name] === OTHER).length]).filter(([, n]) => n);
    const repeated = sorted.filter(([, rs]) => rs.length > 1);
    const most = Math.max(0, ...sorted.map(([, rs]) => rs.length));
    const stat = (key, value, label, sub, tone = '') => h('button', { type: 'button', class: `rs-stat ${tone}`, 'aria-pressed': String(runsShow === key),
      onclick: () => { runsShow = runsShow === key ? 'all' : key; paint(); } }, h('b', {}, String(value)), h('span', {}, label), sub ? h('small', {}, sub) : '');
    const filter = (conf().filter || '').trim();
    const stats = h('div', { class: 'rs-stats' },
      stat('all', D.runs.length, 'runs', 'From the filter'),
      stat('excluded', excluded.length, 'filtered out', D.excluded_error ? `Could not list them: ${D.excluded_error}`
        : filter ? `Runs of the experiment the filter leaves out (${filter})` : 'No filter: every run of the experiment is used', excluded.length ? 'info' : 'ok'),
      stat('unmatched', unmatched.length, 'unmatched', byDim.map(([n, k]) => `${n}: ${k}`).join(', ') || 'Every run has a value everywhere', unmatched.length ? 'warn' : 'ok'),
      stat('all', sorted.length, 'configurations', 'Distinct combinations of the dimensions'),
      stat('repeated', repeated.length, 'repeated', repeated.length ? `Up to ${most} runs for one configuration` : 'One run per configuration', repeated.length ? 'info' : 'ok'));
    const sortGroups = (m) => [...m.entries()].sort(([, a], [, b]) => {
      for (const d of dims) { const x = rank(d, a[0].labels[d.name]) - rank(d, b[0].labels[d.name]); if (x) return x; }
      return 0;
    });
    // filtered out: grouped by reason (the conditions of the filter a run fails, and its values there), then configuration
    const whyKey = (r) => (r.why || []).map((w) => `${w.cond}\u0001${w.got}`).join('\u0002');
    const reasons = new Map();
    for (const r of excluded) reasons.set(whyKey(r), [...(reasons.get(whyKey(r)) || []), r]);
    // one group per reason; inside, the runs in the order of their configuration
    const byReason = () => [...reasons.values()].sort((x, y) => y.length - x.length)
      .map((rs) => [whyKey(rs[0]), sortGroups(groupsOf(rs)).flatMap(([, g]) => g)]);
    const whyChips = (r) => h('span', { class: 'rs-why' },
      ...(r.why || []).map((w) => h('span', { class: 'rs-why-c', title: `The run fails this condition of the filter; its value: ${w.got}` },
        h('code', {}, w.cond), ' ', h('b', {}, w.got === 'not set' ? 'not set' : `is ${w.got}`))));
    const comboLine = (r) => h('span', { class: 'rs-combo-line' }, dims.map((d) => (r.labels[d.name] === OTHER ? `${d.name}: no value` : r.labels[d.name])).join(' · '));
    const reasonList = runsShow === 'excluded' ? h('div', { class: 'rs-reasons' }, h('span', { class: 'rs-key' }, 'Why'),
      ...[...reasons.values()].sort((x, y) => y.length - x.length).map((rs) => h('span', { class: 'rs-reason' },
        h('b', {}, String(rs.length)), ' ', ...(rs[0].why || []).flatMap((w, i) => [i ? ' and ' : '', h('code', {}, w.cond), ` (${w.got === 'not set' ? 'not set' : 'is ' + w.got})`])))) : '';
    const shown = runsShow === 'excluded' ? byReason() : sorted.filter(([, rs]) => runsShow === 'all' || (runsShow === 'repeated' ? rs.length > 1 : rs.some((r) => dims.some((d) => r.labels[d.name] === OTHER))));
    const status = (x) => h('span', { class: 'rs-status ' + String(x || '').toLowerCase() }, x || '?');
    const body = [];
    for (const [, rs] of shown) {
      const first = rs[0];
      if (body.length) body.push(h('tr', { class: 'rs-gap', 'aria-hidden': 'true' }, h('td', { colspan: String(6 + M.length) })));
      if (runsShow === 'excluded') {
        body.push(h('tr', { class: 'rs-group out' }, h('td', { colspan: String(6 + M.length) }, whyChips(first),
          h('span', { class: 'rs-count-badge' + (rs.length > 1 ? ' many' : '') }, rs.length > 1 ? `${rs.length} runs` : '1 run'))));
      } else body.push(h('tr', { class: 'rs-group' + (rs.length > 1 ? ' rep' : '') },
        h('td', { colspan: String(6 + M.length) },
          h('span', { class: 'rs-combo' }, ...dims.map((d) => h('span', { class: 'rs-lab' + (first.labels[d.name] === OTHER ? ' other' : ''), title: d.name },
            first.labels[d.name] === OTHER ? `${d.name}: no value` : first.labels[d.name]))),
          h('span', { class: 'rs-count-badge' + (rs.length > 1 ? ' many' : '') }, rs.length > 1 ? `${rs.length} runs` : '1 run'),
          dims.some((d) => d.block) ? h('button', { type: 'button', class: 'tiny', title: 'Select these options in the Launcher tab', onclick: () => selectInLauncher(first) }, 'Select in Launcher') : '')));
      for (const r of rs) {
        const open = () => openMlflow(`#/experiments/${D.experiment_id}/runs/${r.id}`);
        const idChip = h('button', { type: 'button', class: 'rs-id', title: `${r.id}\nclick to copy the run id` }, r.id.slice(0, 8));
        idChip.addEventListener('click', async () => {
          try { await navigator.clipboard.writeText(r.id); idChip.textContent = 'copied'; setTimeout(() => { idChip.textContent = r.id.slice(0, 8); }, 1200); } catch (e) { status(r.id); }
        });
        body.push(h('tr', {},
          h('td', { class: 'rs-run-name' }, r.name || '—', runsShow === 'excluded' ? comboLine(r) : ''), h('td', { class: 'rs-id-cell' }, idChip),
          h('td', {}, status(r.status)), h('td', { class: 'mono hint' }, r.start || ''), h('td', { class: 'mono hint' }, r.commit || ''),
          ...M.map((m) => h('td', { class: 'num mono' }, r.metrics[m.key] == null ? '—' : fmt(r.metrics[m.key] * lay().scale))),
          h('td', { class: 'rs-open' }, h('a', { class: 'tiny btnlink', href: window.XPRouter?.href({ tab: 'mlflow', mlflow: `/experiments/${D.experiment_id}/runs/${r.id}` }) || '#',
            title: 'Open this run in the MLflow tab (middle click: in a new tab)', onclick: (ev) => { if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button) return; ev.preventDefault(); open(); } }, 'MLflow ↗'))));
      }
    }
    return h('div', { class: 'rs-runs-view' }, stats, noRunsPanel(), reasonList,
      h('div', { class: 'rs-scroll' }, h('table', { class: 'rs-runlist' },
        h('thead', {}, h('tr', {}, ...['run', 'id', 'status', 'started', 'commit', ...M.map((m) => m.label.replace(/[$\\{}]/g, '')), ''].map((t, i) => h('th', { class: i >= 5 && i < 5 + M.length ? 'num' : '' }, t)))),
        h('tbody', {}, ...(body.length ? body : [h('tr', {}, h('td', { colspan: String(6 + M.length), class: 'hint' }, 'Nothing to show.'))])))));
  }

  /* ---------- block options without runs ---------- */

  /* [{bid, block, name, out}] the options of the XP's blocks that no run (from the filter) matches; `out`: how many of
     the filtered-out runs match it. Only for dimensions that come from a block and list its options. */
  function noRuns() {
    const D = R.data;
    if (!D || D.errors && Object.keys(D.errors).length || loadedFor !== st.xp) return [];
    const out = [];
    for (const d of conf().dimensions) {
      const b = d.block && blk(d.block);
      if (!b || !d.values) continue;
      const listed = new Set(d.values.map((v) => v.label));
      for (const r of b.rows) {
        if (!listed.has(r.name) || D.counts[d.name]?.[r.name]) continue;
        out.push({ bid: b.id, block: b.label, name: r.name, out: (D.excluded || []).filter((x) => x.labels[d.name] === r.name).length });
      }
    }
    return out;
  }

  async function deleteOptions(list) {
    const left = (b) => b.rows.length - list.filter((x) => x.bid === b.id).length;
    const keep = list.filter((x) => left(blk(x.bid)) >= 1);
    const skipped = list.length - keep.length;
    if (!keep.length) { status('A block needs at least one option: nothing deleted', true); return; }
    try {
      await commit((c) => {
        const xp = c.xps.find((x) => x.name === st.xp);
        for (const b of xp.blocks) {
          const gone = new Set(keep.filter((x) => x.bid === b.id).map((x) => x.name));
          if (!gone.size || b.rows.length - gone.size < 1) continue;
          b.rows = b.rows.filter((r) => !gone.has(r.name));
          // the saved dimensions list the options too
          for (const d of xp.results?.dimensions || []) if (d.block === b.id && d.values) d.values = d.values.filter((v) => !gone.has(v.label));
        }
      }, { deleted: keep.length === 1 ? `option "${keep[0].name}" of ${keep[0].block}` : `${keep.length} options without runs`
          + (skipped ? ` (${skipped} kept: a block needs one option)` : '') });
    } catch (e) { status(e.message, true); return; }
    load(false);
  }

  function noRunsPanel() {
    const list = noRuns();
    if (!list.length) return '';
    const blocks = [...new Set(list.map((x) => x.bid))];
    return h('div', { class: 'rs-noruns' },
      h('div', { class: 'rs-noruns-head' }, h('b', {}, `${list.length} block option${list.length > 1 ? 's' : ''} without runs`),
        h('span', { class: 'hint' }, ' no run from the filter matches them (the filter of each option is in Dimensions...)'),
        h('span', { class: 'sp' }),
        h('button', { type: 'button', class: 'tiny danger', onclick: () => deleteOptions(list) }, `Delete All ${list.length}`)),
      ...blocks.map((bid) => h('div', { class: 'rs-noruns-row' }, h('span', { class: 'rs-key' }, list.find((x) => x.bid === bid).block),
        ...list.filter((x) => x.bid === bid).map((x) => h('span', { class: 'rs-norun' }, x.name,
          x.out ? h('small', { title: 'Runs that match this option but that the filter leaves out' }, ` ${x.out} filtered out`) : '',
          h('button', { type: 'button', class: 'x', 'aria-label': `Delete the option ${x.name} of ${x.block}`, title: 'Delete this option', onclick: () => deleteOptions([x]) }, '×'))))));
  }

  function selectInLauncher(run) {
    for (const d of conf().dimensions) {
      const b = d.block && blk(d.block);
      if (b && b.rows.some((r) => r.name === run.labels[d.name])) st.blocks[b.id] = run.labels[d.name];
    }
    switchTab('launcher');
    draw();
    status('Options selected: check the blocks, then Generate or Launch');
  }

  /* ---------- page ---------- */

  function paint() {
    const box = $('#results');
    if (!box || R.tab !== 'results') return;
    const head = h('div', { class: 'rs-head' }, h('h2', {}, 'Results'),
      R.data ? h('span', { class: 'hint' }, `${R.data.config.experiment}: ${R.data.runs.length} runs from the filter`) : '',
      h('span', { class: 'sp' }), sourceSwitch(),
      h('button', { type: 'button', class: 'ghost', onclick: () => { R.out = {}; load(true); } }, R.loading ? 'Loading...' : 'Refresh'));
    if (R.switchError) {
      box.replaceChildren(head, switchNote(), h('button', { type: 'button', class: 'ghost', onclick: () => { R.switchError = ''; load(true); } },
        `Show the Runs of the ${R.source?.label || 'database'} Instead`));
      return;
    }
    if (R.error) { box.replaceChildren(head, h('pre', { class: 'dlg-err' }, R.error)); return; }
    if (!R.data) { box.replaceChildren(head, h('p', { class: 'empty' }, 'Reading the runs from MLflow...')); return; }
    const errs = Object.entries(R.data.errors || {});
    if (errs.length) {
      box.replaceChildren(head, ...errs.map(([k, e]) => h('p', { class: 'dlg-err' }, `${k.replace('\u0000', ' / ')}: ${e}`)),
        h('button', { type: 'button', class: 'ghost', onclick: dimensionsDialog }, 'Dimensions...'));
      return;
    }
    const s = R.data.summary;
    const others = conf().dimensions.filter((d) => R.data.counts[d.name]?.[OTHER]).map((d) => `${d.name}: ${R.data.counts[d.name][OTHER]} run(s) match no value`);
    const note = h('p', { class: 'hint' }, `${s.n_runs} runs shown` + (others.length ? `; ${others.join('; ')}` : '') + '.');
    let body;
    if (R.view.startsWith('v:') && !curView()) R.view = 'table';           // a view deleted meanwhile
    const view = curView();
    if (view?.type === 'artifact') body = h('div', {}, artifactControls(view), artifactGrid(view));
    else if (view) {
      const left = h('div', { class: 'rs-side' }, h('div', { class: 'rs-side-head' }, h('h3', {}, 'Preview'), h('span', { class: 'hint' }, 'Computed by the launcher')),
        curvesPreview(view));
      body = h('div', {}, curvesControls(view), h('div', { class: 'rs-two' }, left, scriptPane('view:' + view.id)));
    } else if (R.view === 'runs') body = runsView();
    else {
      const left = h('div', { class: 'rs-side' }, h('div', { class: 'rs-side-head' }, h('h3', {}, 'Preview'), h('span', { class: 'hint' }, 'Computed by the launcher, instantly')),
        R.view === 'plot' ? previewPlotPane() : previewTable());
      body = h('div', { class: 'rs-two' }, left, scriptPane(R.view));
    }
    box.replaceChildren(head, selectionStrip(), controls(), readmeBox(), note, body);
  }

  let loadGen = 0;                                // a newer load (or another database) drops the older answer
  async function load(fresh) {
    if (!st.xp) return;
    const gen = ++loadGen;
    R.loading = true; R.error = ''; paint();
    let data = null, error = '';
    try { data = await api('/api/report/preview', { sel: selq(), fresh }); } catch (e) { error = e.message; }
    if (gen !== loadGen) return;
    R.data = data; R.error = error;
    R.loading = false;
    paint();
    drawStrip();                                  // the launcher flags the options without runs
    window.dispatchEvent(new Event('xp-state'));  // router.js: the selection or view may have changed
  }

  /* ---------- the database read by the Results and MLflow tabs: local, or the synchronised one of the central
     server (through a tunnel the launcher opens when you choose it: sources.py) ---------- */

  function sourceSwitch() {
    const s = R.source;
    const b = (which, label, title) => h('button', {
      type: 'button', 'aria-pressed': String(s?.which === which), title,
      // h() sets every attribute it is given (disabled="false" disables): null leaves it out
      disabled: R.switching || (which === 'sync' && s && !s.central) ? '' : null, onclick: () => pickSource(which),
    }, label);
    return h('span', { class: 'seg src-switch', role: 'group', 'aria-label': 'Database', title: s ? `Reading: ${s.label}` : '' },
      b('local', 'Local', 'The MLflow tracking URI of the Settings'),
      b('sync', 'Synchronised', s?.central ? `The synchronised database on ${s.central} (connect it in Servers first)` : 'No central server in the settings'));
  }

  async function pickSource(which) {
    if (!R.source) await fetchSource();
    if (!R.source || R.source.which === which || R.switching) return;
    R.switching = true;
    status(which === 'sync' ? `Opening a tunnel to the synchronised database on ${R.source.central}...` : 'Back to the local database...');
    if (R.tab === 'mlflow') $('#mlflowtab').replaceChildren(h('p', { class: 'empty' }, 'Changing the database...'));
    else paint();
    let ok = true;
    R.switchError = '';
    try { R.source = await api('/api/source', { which }); status(`Results and MLflow tabs: ${R.source.label}`); } catch (e) {
      ok = false;
      R.switchError = `Could not switch to the ${which === 'sync' ? `synchronised database on ${R.source.central}` : 'local database'}: ${e.message}`;
      status(R.switchError, true);
    }
    R.switching = false;
    if (ok) {                                     // everything shown came from the other database
      loadGen += 1; mlflowGen += 1; R.loading = false; mlflowReady = false; $('#mlflowtab').replaceChildren(); R.out = {}; R.data = null;
    }
    if (R.tab === 'mlflow') { if (ok) startMlflow(); else switchFailed(); }
    else if (R.tab === 'results') { if (ok) load(true); else paint(); }
  }

  /* the switch failed: say why (and nothing from the database still chosen, which may not answer either) */
  function switchFailed() {
    $('#mlflowtab').replaceChildren(h('div', { class: 'mlflow-bar' }, h('span', { class: 'sp' }), sourceSwitch()), switchNote(),
      h('button', { type: 'button', class: 'ghost', onclick: () => { R.switchError = ''; startMlflow(); } }, `Open MLflow on the ${R.source?.label || 'database'} Instead`));
  }
  function switchNote() {
    return h('p', { class: 'dlg-err' }, R.switchError, ' ',
      /not connected/.test(R.switchError) ? h('button', { type: 'button', class: 'tiny', onclick: () => switchTab('servers') }, 'Servers') : '');
  }

  async function fetchSource() {
    try { R.source = await api('/api/source'); } catch (e) { status(`Which database is read: ${e.message}`, true); return; }
    for (const el of document.querySelectorAll('.src-switch')) el.replaceWith(sourceSwitch());
  }
  fetchSource();

  /* ---------- MLflow tab: the UI of an MLflow server started by the launcher ---------- */

  let mlflowReady = false, pendingHash = null, mlflowGen = 0, mlflowImage = '';

  async function startMlflow() {
    const box = $('#mlflowtab');
    if (mlflowReady) { showFrame('/mlflow/'); return; }
    const gen = ++mlflowGen;                      // another database chosen meanwhile: this start is dropped
    box.replaceChildren(h('div', { class: 'mlflow-bar' }, h('span', { class: 'sp' }), sourceSwitch()),
      h('p', { class: 'empty' }, `Starting the MLflow server${cfg.project.mlflow_image === 'local' ? '' : ' in a container'} on the ${R.source?.label || 'database'} (a few seconds)...`));
    try {
      let s = await api('/api/mlflow/start', {});
      for (let i = 0; i < 90 && s.state === 'starting' && gen === mlflowGen; i += 1) {
        await new Promise((ok) => setTimeout(ok, 1000));
        s = await api('/api/mlflow/status');
      }
      if (gen !== mlflowGen) return;
      if (s.state !== 'running') throw new Error(`MLflow server ${s.state}` + (s.log ? `:\n${s.log}` : ''));
      mlflowReady = true; mlflowImage = s.image || '';
      showFrame('/mlflow/');
    } catch (e) {
      if (gen !== mlflowGen) return;
      box.replaceChildren(h('div', { class: 'mlflow-bar' }, h('span', { class: 'sp' }), sourceSwitch()),
        h('pre', { class: 'dlg-err' }, e.message), h('button', { type: 'button', class: 'ghost', onclick: startMlflow }, 'Try Again'));
    }
  }

  function showFrame(base) {
    const box = $('#mlflowtab');
    let frame = box.querySelector('iframe');
    const url = base + (pendingHash || '');
    pendingHash = null;
    if (!frame) {
      frame = h('iframe', { src: url, title: 'MLflow', class: 'mlflow-frame' });
      // pages of the MLflow UI: kept in the address of the launcher (router.js)
      frame.addEventListener('load', () => {
        try { frame.contentWindow.addEventListener('hashchange', () => window.dispatchEvent(new Event('xp-state'))); } catch (e) { /* not same origin */ }
        window.dispatchEvent(new Event('xp-state'));
      });
      box.replaceChildren(h('div', { class: 'mlflow-bar' },
        h('span', { class: 'hint' }, `MLflow server started by the launcher (${R.source?.label || cfg.project.tracking_uri.split('?')[0]}${mlflowImage ? `, in ${mlflowImage}` : ''})`),
        h('span', { class: 'sp' }), sourceSwitch(),
        h('button', { type: 'button', class: 'tiny', onclick: () => { frame.src = base + '#/experiments'; } }, 'Experiments'),
        h('a', { class: 'tiny', href: base, target: '_blank', rel: 'noopener' }, 'Open in a New Tab')), frame);
    } else if (url !== base) frame.src = url;
  }

  function openMlflow(hash) { pendingHash = hash; switchTab('mlflow'); }

  let loadedFor = null;
  function switchTab(tab) {
    R.tab = tab;
    for (const t of ['results', 'mlflow', 'servers', 'databases', 'sync', 'datasets']) document.body.classList.toggle('tab-' + t, tab === t);
    for (const t of ['launcher', 'results', 'mlflow', 'servers', 'databases', 'sync', 'datasets']) $('#tab-' + t)?.setAttribute('aria-pressed', String(tab === t));
    if (['servers', 'databases', 'sync', 'datasets'].includes(tab)) { window.XPServers?.show(tab); return; }
    window.XPServers?.hide();
    if (tab === 'mlflow') { startMlflow(); return; }
    if (tab === 'results' && (loadedFor !== st.xp || !R.data)) {
      if (loadedFor !== st.xp && loadedFor !== null) R.sel = null;
      loadedFor = st.xp; R.data = null; R.out = {}; load(false);
    } else paint();
  }

  const tab = (id, label) => h('button', { type: 'button', id: 'tab-' + id, 'aria-pressed': String(id === 'launcher'), onclick: () => switchTab(id) }, label);
  const tabs = h('span', { class: 'tabs' },
    h('span', { class: 'seg', role: 'group', 'aria-label': 'Experiment' }, tab('launcher', 'Launcher'), tab('results', 'Results'), tab('mlflow', 'MLflow')),
    h('span', { class: 'seg', role: 'group', 'aria-label': 'Servers' },
    h('button', { type: 'button', id: 'tab-servers', 'aria-pressed': 'false', onclick: () => switchTab('servers') }, 'Servers'),
    h('button', { type: 'button', id: 'tab-databases', 'aria-pressed': 'false', onclick: () => switchTab('databases') }, 'Databases'),
    h('button', { type: 'button', id: 'tab-sync', 'aria-pressed': 'false', onclick: () => switchTab('sync') }, 'Sync'),
    h('button', { type: 'button', id: 'tab-datasets', 'aria-pressed': 'false', onclick: () => switchTab('datasets') }, 'Datasets')));
  $('#xpname').after(tabs);
  // for router.js (the address of the page)
  window.XPResults = {
    get tab() { return R.tab; },
    get view() { return R.view; },
    get selection() { return R.data && loadedFor === st.xp ? R.data.config.selection : R.sel; },
    setSelection(id) { if (id !== R.sel) { R.sel = id || null; R.data = null; R.out = {}; loadedFor = null; } },
    setView(v) { if (['table', 'plot', 'runs'].includes(v) || /^v:[a-z0-9_]+$/.test(v)) R.view = v; },
    switchTab, openMlflow,
    noRuns: () => noRuns(),
    frameHash() { try { return $('#mlflowtab iframe')?.contentWindow.location.hash.replace(/^#/, '') || ''; } catch (e) { return ''; } },
  };
  $('main').append(h('section', { id: 'results', 'aria-label': 'Results' }), h('section', { id: 'mlflowtab', 'aria-label': 'MLflow' }));
  // a different XP picked in the side panel: reload its results when the tab is shown
  $('#xps').addEventListener('click', () => setTimeout(() => { if (R.tab === 'results' && loadedFor !== st.xp) switchTab('results'); }, 0));
})();
