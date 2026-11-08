"""Results of an XP from MLflow: the runs labelled by dimensions, a LaTeX table and dot plots.

Written by the XP launcher: CONFIG (between the two CONFIG markers) holds the settings of the XP, the rest is the same
code the launcher uses for its preview. Nothing is precomputed: the runs are read from MLflow each time.

    python THIS_FILE.py [output folder] [--table] [--plot] [--view ID] [--preview]   (figures: PDF, and PNG to look at)
        --table     only the LaTeX table   --plot  only the figures   --preview  quick figures (no LaTeX text)
        --view ID   only the view ID of CONFIG["views"] (training curves); --views: all of them

Views (CONFIG["views"]): more figures from the same runs. {"id", "type": "curves", "metrics": [keys], "x": "step" |
"epoch", "source": "" (the runs) or a link name (the linked runs: e.g. the training of the checkpoint), "color":
dimension, "panel": dimension, "smooth": 0..0.99 (exponential moving average), "std": band of one standard deviation,
"logy"}: the history of each metric, averaged over the runs of each group (color x panel).

Dimensions: a dimension names what varies between runs. Either
    {"name": ..., "values": [{"label": ..., "rule": <MLflow filter>}, ...]}
        a run gets the label of the first rule it matches ("(other)" when none does). Rules use the syntax of MLflow
        search filters: params.KEY = 'value' and metrics.KEY > 0.5 and params.KEY LIKE '%Pooling%' ...
        (=, !=, <, <=, >, >=, LIKE, ILIKE, NOT LIKE, IN (...), NOT IN (...), IS NULL, IS NOT NULL; joined with "and";
        keys: params.*, metrics.*, tags.*, attributes.status / run_name / run_id; `backquotes` for unusual keys).
    {"name": ..., "param": "params.KEY", "labels": {raw value: label}}
        the value of a parameter, renamed by `labels` when given.

Links: a run can use what another run produced (a checkpoint). CONFIG["links"] = [{"name": "ckpt", "param":
"params.task.checkpoint_path"}]: the MLflow run id found in that parameter names the parent run, whose columns are joined
in as ckpt.params.*, ckpt.metrics.*, ckpt.tags.*, ckpt.run_id, ckpt.status, ckpt.experiment, and ckpt.label (what
differs between the parents) and ckpt.axis.NAME (one column per thing that differs, e.g. ckpt.axis.adapters =
"WaveletAdapter + PerceiverAdapter (num_tokens 16)"). Dimensions and rules use them like any column:
ckpt.params.KEY = 'value'.
"""
# <CONFIG>
CONFIG = {
    "tracking_uri": "",
    "experiment": "",
    "filter": "attributes.status = 'FINISHED'",
    "metrics": {},
    "links": [],
    "views": [],
    "dimensions": [],
    "layout": {"panel": "", "column": "", "rows": [], "hide": {}, "scale": 100, "digits": 1, "std": True,
               "missing": True},
    "stem": "results",
}
# </CONFIG>

import json
import math
import re
import sys
from pathlib import Path

OTHER = "(other)"
UNSET = "(unset)"
COLORS = ["#277da1", "#f8961e", "#43aa8b", "#f94144", "#577590", "#90be6d"]


# --------------------------------------------------------------------------- MLflow filter syntax (subset)

_TOKEN = re.compile(r"""\s*(?:
    (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<num>-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)
  | (?P<op>!=|<=|>=|=|<|>|\(|\)|,)
  | (?P<word>(?:[A-Za-z_][\w]*\.)?(?:`[^`]+`|[\w./:\-\[\]]+)|[A-Za-z_]\w*)
)""", re.X)
ATTRIBUTES = {"status": "status", "run_name": "tags.mlflow.runName", "run_id": "run_id", "start_time": "start_time",
              "end_time": "end_time", "user_id": "tags.mlflow.user"}


def _tokens(text):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read the filter near: {text[pos:pos + 30]!r}")
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "str":
            val = re.sub(r"\\(.)", r"\1", val[1:-1])
        elif kind == "num":
            val = float(val)
        out.append((kind, val))
        pos = m.end()
    return out


_HEADS = {"params", "param", "parameter", "parameters", "metrics", "metric", "tags", "tag", "attributes", "attribute",
          "attr", "run"}


def column_of(key):
    """MLflow filter key -> column of mlflow.search_runs(): params.x -> params.x, attributes.status -> status;
    ckpt.params.x -> ckpt.params.x (the columns of a linked run)."""
    key = key.replace("`", "")
    head, _, rest = key.partition(".")
    if head.lower() not in _HEADS and rest.partition(".")[0].lower() in _HEADS:
        return head + "." + column_of(rest)
    head = head.lower()
    if head in ("params", "param", "parameter", "parameters"):
        return "params." + rest
    if head in ("metrics", "metric"):
        return "metrics." + rest
    if head in ("tags", "tag"):
        return "tags." + rest
    if head in ("attributes", "attribute", "attr", "run"):
        return ATTRIBUTES.get(rest.lower(), rest)
    return ATTRIBUTES.get(key.lower(), key)


def parse_rule(text):
    """[(column, op, value)] of a filter; an empty filter matches every run."""
    toks = _tokens(text or "")
    conds, i = [], 0
    while i < len(toks):
        kind, key = toks[i]
        if kind != "word":
            raise ValueError(f"expected a key such as params.NAME, got {key!r}")
        i += 1
        words = []
        while i < len(toks) and toks[i][0] == "word" and str(toks[i][1]).upper() in ("NOT", "LIKE", "ILIKE", "IN", "IS", "NULL"):
            words.append(str(toks[i][1]).upper())
            i += 1
        if words:
            op = " ".join(words)
            if op in ("IS NULL", "IS NOT NULL"):
                conds.append((column_of(key), op, None))
                i = _skip_and(toks, i)
                continue
        else:
            if i >= len(toks) or toks[i][0] != "op":
                raise ValueError(f"expected an operator after {key}")
            op = toks[i][1]
            i += 1
        if op in ("IN", "NOT IN"):
            if i >= len(toks) or toks[i] != ("op", "("):
                raise ValueError(f"{op} needs a list: ('a', 'b')")
            i += 1
            vals = []
            while i < len(toks) and toks[i] != ("op", ")"):
                if toks[i][0] in ("str", "num"):
                    vals.append(toks[i][1])
                i += 1
            i += 1
            conds.append((column_of(key), op, vals))
        else:
            if i >= len(toks) or toks[i][0] not in ("str", "num"):
                raise ValueError(f"expected a value after {key} {op}")
            conds.append((column_of(key), op, toks[i][1]))
            i += 1
        i = _skip_and(toks, i)
    return conds


def _skip_and(toks, i):
    if i < len(toks):
        if toks[i][0] == "word" and str(toks[i][1]).lower() == "and":
            return i + 1
        raise ValueError(f"expected 'and', got {toks[i][1]!r} (MLflow filters only join conditions with 'and')")
    return i


def _like(pattern, text, flags=0):
    rx = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(rx, text, flags | re.S) is not None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def matches(run, conds):
    """Does a run (a dict: column -> value) satisfy every condition?"""
    for col, op, want in conds:
        got = run.get(col)
        missing = got is None or (isinstance(got, float) and math.isnan(got))
        if op == "IS NULL":
            ok = missing
        elif op == "IS NOT NULL":
            ok = not missing
        elif missing:                    # = 'None': MLflow logs nothing for a null object (feature_loss: null)
            ok = op in ("!=", "NOT LIKE", "NOT IN") or (op == "=" and str(want) == "None")
        elif op in ("IN", "NOT IN"):
            ok = (str(got) in [str(w) if not isinstance(w, float) or not w.is_integer() else str(int(w)) for w in want]
                  or any(_num(got) is not None and _num(got) == _num(w) for w in want)) == (op == "IN")
        elif op in ("LIKE", "ILIKE", "NOT LIKE"):
            ok = _like(str(want), str(got), re.I if op == "ILIKE" else 0) == (op != "NOT LIKE")
        else:
            a, b = _num(got), _num(want)
            if isinstance(want, float) and a is not None:
                x, y = a, b
            else:
                x, y = str(got), str(want)
            ok = {"=": x == y, "!=": x != y, "<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]
        if not ok:
            return False
    return True


# --------------------------------------------------------------------------- runs and their labels

def load_runs(cfg):
    """The runs of the experiment that pass the filter (MLflow does the filtering), as a pandas DataFrame, with the
    columns of their linked runs (CONFIG["links"])."""
    import mlflow
    mlflow.set_tracking_uri(cfg["tracking_uri"])
    df = mlflow.search_runs(experiment_names=[cfg["experiment"]], filter_string=cfg.get("filter") or "",
                            max_results=50000, output_format="pandas")
    return join_links(df, cfg.get("links") or [], cfg["tracking_uri"]) if len(df) else df


# --------------------------------------------------------------------------- linked runs

RUN_ID = re.compile(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])")
# parameters that differ between runs without saying what the run is
NOISE = re.compile(r"(^|\.)(seed|experiment|log_dir|devices|num_workers|dataset_dir|.*_path|.*_dir|checkpoint\w*)$")
_PARENTS = {}


def run_id_in(value):
    m = RUN_ID.search(str(value)) if value is not None else None
    return m.group(1) if m else None


def _short(value):
    s = str(value)
    return s.rsplit(".", 1)[-1] if re.fullmatch(r"[A-Za-z_][\w]*(\.[A-Za-z_][\w]*){2,}", s) else s


def _value(text):
    import ast
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return text


def _leaves(value, path=()):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, path + (str(k),))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _leaves(v, path + (f"[{i}]",))
    else:
        yield path, value


def _classes(value):
    """Class names in a value: 'Bias + Reverse + Amplify'."""
    return " + ".join(_short(v) for p, v in _leaves(value) if p and p[-1] in ("class_path", "cls"))


def describe(runs):
    """(labels, axes) of linked runs (MLflow Run objects): what differs between them.
    axes: {name: {run id: text}}, one per thing that varies (a parameter whose classes differ: its class names, plus
    the values that still differ between runs with the same classes; else each value that differs); parameters that
    vary together (batch_size with acc_grad) make one axis. labels: {run id: "axis: text, ..."}."""
    keys = sorted({k for r in runs for k in r.data.params if not NOISE.search(k)})
    ids = [r.info.run_id for r in runs]
    parsed = {r.info.run_id: {k: _value(r.data.params[k]) for k in keys if k in r.data.params} for r in runs}
    axes, partitions = {}, set()

    def add(name, texts):
        part = tuple(sorted({tuple(sorted(i for i in ids if texts[i] == t)) for t in texts.values()}))
        if len(set(texts.values())) < 2 or part in partitions:
            return                                   # no difference, or the same split as an axis already there
        partitions.add(part)
        n, k = name, 2
        while n in axes:
            n, k = f"{name} {k}", k + 1
        axes[n] = texts

    for key in keys:
        vals = {i: parsed[i].get(key, "-") for i in ids}
        if len({repr(v) for v in vals.values()}) < 2:
            continue
        name = key.rsplit(".", 1)[-1]
        classes = {i: _classes(v) for i, v in vals.items()}
        flat = {i: dict(_leaves(v)) for i, v in vals.items()}
        if len(set(classes.values())) > 1:
            # values that differ between runs with the same classes
            extra = {i: [] for i in ids}
            for c in set(classes.values()):
                same = [i for i in ids if classes[i] == c]
                for path in sorted({p for i in same for p in flat[i]}, key=str):
                    if path and path[-1] not in ("class_path", "cls") and len({repr(flat[i].get(path, "-")) for i in same}) > 1:
                        leaf = next((x for x in reversed(path) if not x.startswith("[") and x != "init_args"), name)
                        for i in same:
                            extra[i].append(f"{leaf} {_short(flat[i].get(path, '-'))}")
            add(name, {i: (classes[i] or "-") + (f" ({', '.join(extra[i])})" if extra[i] else "") for i in ids})
            continue
        for path in sorted({p for f in flat.values() for p in f}, key=str):
            if len({repr(flat[i].get(path, "-")) for i in ids}) > 1:
                leaf = next((x for x in reversed(path) if not x.startswith("[") and x != "init_args"), name) if path else name
                add(leaf, {i: _short(flat[i].get(path, "-")) for i in ids})
    # the label: only what differs from the most common value of each axis ("base": the most common everywhere)
    common = {a: max(set(t.values()), key=lambda v, t=t: (list(t.values()).count(v), v)) for a, t in axes.items()}
    labels = {}
    for r in runs:
        i = r.info.run_id
        diff = [f"{a}: {t[i]}" for a, t in axes.items() if t[i] != common[a]]
        labels[i] = ", ".join(diff) if diff else ("base" if axes else r.info.run_name or i[:8])
    return labels, axes


def join_links(df, links, tracking_uri):
    """df with the columns of the linked runs: <name>.params.*, <name>.metrics.*, ... (one MLflow call per parent)."""
    import mlflow
    import pandas as pd
    client = mlflow.tracking.MlflowClient(tracking_uri)
    names = {}
    for link in links:
        col = column_of(link["param"])
        if col not in df.columns:
            continue
        ids = [run_id_in(v) for v in df[col]]
        runs = {}
        for i in sorted({x for x in ids if x}):
            if i not in _PARENTS:
                try:
                    _PARENTS[i] = client.get_run(i)
                except Exception:  # pylint: disable=broad-except  # deleted, or another tracking server
                    _PARENTS[i] = None
            if _PARENTS[i] is not None:
                runs[i] = _PARENTS[i]
        labels, axes = describe(list(runs.values())) if runs else ({}, {})
        n, rows = link["name"], []
        for i in ids:
            r = runs.get(i)
            row = {f"{n}.run_id": i, f"{n}.label": labels.get(i) if r else ("(no link)" if not i else "(parent run not found)")}
            row.update({f"{n}.axis.{a}": (t.get(i) if r else None) for a, t in axes.items()})
            if r is not None:
                eid = r.info.experiment_id
                if eid not in names:
                    try:
                        names[eid] = client.get_experiment(eid).name
                    except Exception:  # pylint: disable=broad-except
                        names[eid] = eid
                row.update({f"{n}.status": r.info.status, f"{n}.experiment": names[eid],
                            f"{n}.tags.mlflow.runName": r.info.run_name})
                row.update({f"{n}.params.{k}": v for k, v in r.data.params.items()})
                row.update({f"{n}.metrics.{k}": v for k, v in r.data.metrics.items()})
                row.update({f"{n}.tags.{k}": v for k, v in r.data.tags.items()})
            rows.append(row)
        df = pd.concat([df.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    return df


def _records(df):
    return [{k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in row.items()}
            for row in df.to_dict("records")]


def dimension_values(dim, labelled=None):
    """Values of a dimension in display order."""
    if dim.get("values") is not None:
        out = [v["label"] for v in dim["values"]]
        if labelled is not None and any(x == OTHER for x in labelled):
            out.append(OTHER)
        return list(dict.fromkeys(out))
    named = list(dict.fromkeys((dim.get("labels") or {}).values()))
    rest = sorted({x for x in labelled or [] if x not in named}, key=lambda s: (_num(s) is None, _num(s) or 0, str(s)))
    return named + rest


def label_runs(runs, dims):
    """Add the label of every dimension to each run (a list of dicts); returns {dimension: [values in order]}."""
    compiled = []
    for d in dims:
        if d.get("values") is not None:
            compiled.append((d, [(v["label"], parse_rule(v.get("rule") or "")) for v in d["values"]]))
        else:
            compiled.append((d, None))
    for r in runs:
        for d, rules in compiled:
            if rules is not None:
                r[d["name"]] = next((label for label, conds in rules if matches(r, conds)), OTHER)
            else:
                raw = r.get(column_of(d["param"]))
                raw = UNSET if raw is None else str(raw)
                r[d["name"]] = (d.get("labels") or {}).get(raw, raw)
    return {d["name"]: dimension_values(d, [r[d["name"]] for r in runs]) for d in dims}


# --------------------------------------------------------------------------- summary: mean / std / n per cell

def summary(runs, order, cfg):
    """{panels: [{value, rows: [{values, cells: {column: {metric: {mean, sd, n}}}}], best}], columns, rows, metrics}."""
    lay, metrics = cfg["layout"], cfg["metrics"] or {}
    hide = {k: set(v) for k, v in (lay.get("hide") or {}).items()}
    scale = lay.get("scale", 100)
    runs = [r for r in runs if all(r.get(d) not in vals for d, vals in hide.items())]

    def shown(dim):
        return [v for v in order.get(dim, [""]) if v not in hide.get(dim, set())] if dim else [""]

    def get(r, dim):
        return r.get(dim, "") if dim else ""

    panel, column, rows = lay.get("panel") or "", lay.get("column") or "", [d for d in lay.get("rows") or [] if d]
    missing = lay.get("missing", True)
    panels_v = [p for p in shown(panel) if missing or any(get(r, panel) == p for r in runs)]
    cols_v = [c for c in shown(column) if missing or any(get(r, column) == c for r in runs)]
    out = []
    for p in panels_v:
        in_p = [r for r in runs if get(r, panel) == p]
        combos = [[]]
        for d in rows:
            combos = [c + [v] for c in combos for v in shown(d)]
        prow = []
        for combo in combos:
            match = [r for r in in_p if all(r.get(d) == v for d, v in zip(rows, combo))]
            if not match and not missing:
                continue
            cells = {}
            for c in cols_v:
                cr = [r for r in match if get(r, column) == c]
                cells[c] = {}
                for key in metrics:
                    vals = [r.get("metrics." + key) for r in cr]
                    vals = [float(v) * scale for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
                    if vals:
                        mean = sum(vals) / len(vals)
                        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else None
                        cells[c][key] = {"mean": mean, "sd": sd, "n": len(vals), "runs": [r.get("run_id") for r in cr]}
            prow.append({"values": combo, "cells": cells})
        best = {}
        for c in cols_v:
            for key in metrics:
                vals = [r["cells"][c][key]["mean"] for r in prow if key in r["cells"][c]]
                best[f"{c}\u0000{key}"] = max(vals) if len(vals) > 1 else None
        if prow or missing:
            out.append({"value": p, "rows": prow, "best": best})
    return {"panels": out, "columns": cols_v, "panel": panel, "column": column, "rows": rows,
            "metrics": metrics, "n_runs": len(runs)}


# --------------------------------------------------------------------------- LaTeX table

_TEX = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex(text):
    """Plain text for LaTeX; a label that already holds $ or a backslash is used as it is."""
    text = str(text)
    return text if re.search(r"[$\\]", text) else "".join(_TEX.get(c, c) for c in text)


def latex_table(s, cfg):
    lay = cfg["layout"]
    digits, std = int(lay.get("digits", 1)), lay.get("std", True)
    metrics = s["metrics"]
    lead = ([tex(s["panel"])] if s["panel"] else []) + [tex(d) for d in s["rows"]]
    n_lead, nm = len(lead), len(metrics)
    cols = s["columns"]
    lines = [f"% {cfg['experiment']}: {s['n_runs']} runs (filter: {cfg.get('filter') or 'none'})",
             "% needs \\usepackage{booktabs,multirow}",
             "\\begin{tabular}{" + "l" * max(n_lead, 1) + "c" * (nm * len(cols)) + "}", "\\toprule"]
    if s["column"]:
        lines.append(" & ".join([""] * max(n_lead, 1) + [f"\\multicolumn{{{nm}}}{{c}}{{{tex(c)}}}" for c in cols]) + " \\\\")
        lines.append(" ".join(f"\\cmidrule(lr){{{max(n_lead, 1) + 1 + i * nm}-{max(n_lead, 1) + (i + 1) * nm}}}"
                              for i in range(len(cols))))
    lines.append(" & ".join((lead or [""]) + [tex(lbl) for _ in cols for lbl in metrics.values()]) + " \\\\")
    lines.append("\\midrule")
    for pi, p in enumerate(s["panels"]):
        for ri, row in enumerate(p["rows"]):
            prev = p["rows"][ri - 1] if ri else None
            cells = []
            if s["panel"]:
                cells.append(f"\\multirow{{{len(p['rows'])}}}{{*}}{{{tex(p['value'])}}}" if ri == 0 else "")
            for i, v in enumerate(row["values"]):
                same = prev and i < len(row["values"]) - 1 and prev["values"][:i + 1] == row["values"][:i + 1]
                cells.append("" if same else tex(v))
            if not s["panel"] and not row["values"]:
                cells.append("")
            for c in cols:
                for key in metrics:
                    x = row["cells"][c].get(key)
                    if not x:
                        cells.append("--")
                        continue
                    b = p["best"].get(f"{c}\u0000{key}")
                    v = f"{x['mean']:.{digits}f}"
                    v = f"\\mathbf{{{v}}}" if b is not None and abs(x["mean"] - b) < 1e-9 else v
                    cells.append(f"${v} \\pm {x['sd']:.{digits}f}$" if std and x["sd"] is not None else f"${v}$")
            lines.append(" & ".join(cells) + " \\\\")
        if pi < len(s["panels"]) - 1:
            lines.append("\\midrule")
    lines += ["\\bottomrule", "\\end{tabular}"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- dot plots (like the notebook)

def row_label(values):
    values = [tex(v) for v in values]
    return (values[0] + (" [" + ", ".join(values[1:]) + "]" if len(values) > 1 else "")) if values else ""


def slug(text):
    return re.sub(r"[^A-Za-z0-9.+-]+", "_", str(text)).strip("_") or "all"


def plot(s, cfg, out, preview=False):
    """One figure per panel, one plot per column, a row per row combination, a dot per metric. Returns the files."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import seaborn as sns
    from matplotlib.backends.backend_pdf import PdfPages
    lay = cfg["layout"]
    digits, std, scale = max(int(lay.get("digits", 1)), 2), lay.get("std", True), lay.get("scale", 100)
    rc = {"axes.formatter.use_mathtext": True}
    if not preview:
        rc.update({"text.usetex": True, "font.family": "LM Roman 10"})
    sns.set(font_scale=1.5, style="whitegrid", rc=rc)
    lbl = (lambda t: tex(t)) if not preview else (lambda t: str(t))
    metrics = s["metrics"]
    colors = {k: COLORS[i % len(COLORS)] for i, k in enumerate(metrics)}
    stem, files = cfg.get("stem") or "results", []
    out.mkdir(parents=True, exist_ok=True)
    book = None if preview else PdfPages(out / f"{stem}-all.pdf")
    for p in s["panels"]:
        labels = [(row_label(r["values"]) if not preview else " [".join([r["values"][0], ", ".join(r["values"][1:])]) + "]"
                   if len(r["values"]) > 1 else (r["values"][0] if r["values"] else "")) for r in p["rows"]]
        cols = s["columns"] or [""]
        fig, axes = plt.subplots(nrows=1, ncols=len(cols), squeeze=False, sharex=True, sharey=True,
                                 figsize=(7 * len(cols), max(4, 0.48 * len(labels))))
        offsets = np.linspace(-0.18, 0.18, len(metrics)) if len(metrics) > 1 else [0.0]
        y = np.arange(len(labels))
        for ci, c in enumerate(cols):
            ax = axes[0, ci]
            drawn = False
            for offset, key in zip(offsets, metrics):
                for yi, row in zip(y, p["rows"]):
                    x = row["cells"].get(c, {}).get(key)
                    if not x:
                        continue
                    drawn = True
                    if std and x["sd"] is not None:
                        ax.errorbar(x["mean"], yi + offset, xerr=x["sd"], fmt="none", ecolor=colors[key], elinewidth=1.2,
                                    alpha=0.6, zorder=2)
                    ax.scatter(x["mean"], yi + offset, color=colors[key], s=48, zorder=3)
                    ax.annotate(f"{x['mean']:.{digits}f}", (x["mean"], yi + offset), xytext=(5, 0),
                                textcoords="offset points", va="center", fontsize=7)
            if not drawn:
                ax.text(0.5, 0.5, "No runs", ha="center", va="center", transform=ax.transAxes)
            ax.set_yticks(y)
            if ci == 0:
                ax.set_yticklabels(labels, fontsize=8)
                ax.invert_yaxis()                            # the plots share the y axis: invert it once
            else:
                ax.tick_params(axis="y", labelleft=False)
            ax.set_xlim(0, 1.08 * scale)
            ax.set_title(lbl(c))
            ax.set_xlabel(r"Score (\%)" if scale == 100 and not preview else "Score (%)" if scale == 100 else "Score")
            ax.grid(axis="x", alpha=0.3)
        handles = [plt.Line2D([0], [0], marker="o", linestyle="", color=colors[k], label=v if not preview else re.sub(r"[$\\{}]", "", v))
                   for k, v in metrics.items()]
        fig.suptitle(lbl(cfg["experiment"]) + (" -- " + lbl(p["value"]) if s["panel"] else ""), y=0.95)
        if s["rows"]:
            fig.text(0.025, 0.94, row_label(s["rows"]) if not preview else s["rows"][0] + (" [" + ", ".join(s["rows"][1:]) + "]" if len(s["rows"]) > 1 else ""),
                     ha="left", va="top", fontsize=12)
        if handles:
            fig.legend(handles=handles, fontsize=12, loc="upper right", bbox_to_anchor=(0.98, 0.98), ncol=len(handles))
        fig.tight_layout()
        name = f"{stem}-{slug(p['value'])}" if s["panel"] else stem
        # PNG for viewing: matplotlib's SVG output misplaces LaTeX tick labels (the PDF is right)
        fig.savefig(out / f"{name}.png", bbox_inches="tight", dpi=200 if not preview else 110)
        files.append(f"{name}.png")
        if not preview:
            fig.savefig(out / f"{name}.pdf", bbox_inches="tight")
            book.savefig(fig, bbox_inches="tight")
            files.append(f"{name}.pdf")
        plt.close(fig)
    if book is not None:
        book.close()
        files.append(f"{stem}-all.pdf")
    return files


# --------------------------------------------------------------------------- training curves (views)

_HIST = {}


def history(client, run_id, key, finished=True):
    """[(step, value)] of a metric of a run (cached: forever for a finished run, a minute otherwise)."""
    import time
    hit = _HIST.get((run_id, key))
    if hit is not None and (hit[0] or time.time() - hit[1] < 60):
        return hit[2]
    pts = sorted((m.step, m.value) for m in client.get_metric_history(run_id, key))
    _HIST[(run_id, key)] = (finished, time.time(), pts)
    return pts


def _smooth(values, alpha):
    out, s = [], None
    for v in values:
        s = v if s is None else alpha * s + (1 - alpha) * v
        out.append(s)
    return out


def curve_data(runs, order, cfg, view):
    """{metrics, x, panels: [{value, charts: [{metric, series: [{label, runs, points: [[x, mean, sd, n]]}]}]}]}.
    `runs`: labelled run dicts. A group (panel x color value) averages its runs step by step (after smoothing)."""
    import mlflow
    client = mlflow.tracking.MlflowClient(cfg["tracking_uri"])
    hide = {k: set(v) for k, v in (cfg["layout"].get("hide") or {}).items()}
    runs = [r for r in runs if all(r.get(d) not in vals for d, vals in hide.items())]
    src, color, panel = view.get("source") or "", view.get("color") or "", view.get("panel") or ""
    rid = (lambda r: r.get(f"{src}.run_id")) if src else (lambda r: r.get("run_id"))
    status = (lambda r: r.get(f"{src}.status")) if src else (lambda r: r.get("status"))
    groups = {}
    for r in runs:
        i = rid(r)
        if not i:
            continue
        key = (r.get(panel, "") if panel else "", r.get(color, "") if color else "")
        groups.setdefault(key, {})[i] = status(r) == "FINISHED"
    rank = lambda dim, v: (order.get(dim, []).index(v) if v in order.get(dim, []) else 10 ** 6, str(v))
    keys = sorted(groups, key=lambda k: (rank(panel, k[0]), rank(color, k[1])))
    x_epoch, alpha = view.get("x") == "epoch", float(view.get("smooth") or 0)
    out = {"metrics": list(view.get("metrics") or []), "x": "epoch" if x_epoch else "step", "panels": []}
    for p in dict.fromkeys(k[0] for k in keys):
        charts = []
        for metric in out["metrics"]:
            series = []
            for k in [k for k in keys if k[0] == p]:
                at = {}
                for i, finished in groups[k].items():
                    pts = history(client, i, metric, finished)
                    if not pts:
                        continue
                    xs = [s for s, _ in pts]
                    if x_epoch:
                        ep = history(client, i, "epoch", finished)
                        xs = [next((e for s2, e in reversed(ep) if s2 <= s), ep[0][1] if ep else s) for s in xs]
                    for x, v in zip(xs, _smooth([v for _, v in pts], alpha)):
                        at.setdefault(x, []).append(v)
                points = []
                for x in sorted(at):
                    vs = at[x]
                    m = sum(vs) / len(vs)
                    sd = math.sqrt(sum((v - m) ** 2 for v in vs) / (len(vs) - 1)) if len(vs) > 1 else None
                    points.append([x, m, sd, len(vs)])
                if points:
                    series.append({"label": k[1] if color else "all runs", "runs": len(groups[k]), "points": points})
            charts.append({"metric": metric, "series": series})
        out["panels"].append({"value": p, "charts": charts})
    return out


def curves_plot(data, cfg, view, out, preview=False):
    """One figure per view: a row per metric, a column per panel value, a line (and band) per color value."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import seaborn as sns
    rc = {"axes.formatter.use_mathtext": True}
    if not preview:
        rc.update({"text.usetex": True, "font.family": "LM Roman 10"})
    sns.set(font_scale=1.2, style="whitegrid", rc=rc)
    lbl = (lambda t: tex(t)) if not preview else (lambda t: str(t))
    panels, metrics = data["panels"] or [{"value": "", "charts": []}], data["metrics"]
    fig, axes = plt.subplots(nrows=max(1, len(metrics)), ncols=len(panels), squeeze=False, sharex="col",
                             figsize=(8 * len(panels), 3.4 * max(1, len(metrics))))
    labels = list(dict.fromkeys(s["label"] for p in panels for c in p["charts"] for s in c["series"]))
    palette = sns.color_palette("tab10", max(1, len(labels)))
    colors = {l: palette[i % len(palette)] for i, l in enumerate(labels)}
    for pi, p in enumerate(panels):
        for mi, metric in enumerate(metrics):
            ax = axes[mi, pi]
            chart = next((c for c in p["charts"] if c["metric"] == metric), {"series": []})
            for s in chart["series"]:
                xs = [q[0] for q in s["points"]]
                ms = [q[1] for q in s["points"]]
                ax.plot(xs, ms, color=colors[s["label"]], lw=1.4, label=lbl(f"{s['label']} (n={s['runs']})"))
                if view.get("std", True):
                    lo = [q[1] - (q[2] or 0) for q in s["points"]]
                    hi = [q[1] + (q[2] or 0) for q in s["points"]]
                    ax.fill_between(xs, lo, hi, color=colors[s["label"]], alpha=0.15, lw=0)
            if view.get("logy"):
                ax.set_yscale("log")
            if not chart["series"]:
                ax.text(0.5, 0.5, "No history", ha="center", va="center", transform=ax.transAxes)
            ax.set_ylabel(lbl(metric) if pi == 0 else "")
            if mi == 0 and p["value"] != "":
                ax.set_title(lbl(p["value"]))
            if mi == len(metrics) - 1:
                ax.set_xlabel(data["x"])
    fig.suptitle(lbl(view.get("title") or view["id"]))
    fig.tight_layout()
    handles, names = axes[0, 0].get_legend_handles_labels()
    if handles:                                      # below the plots: the labels can be long
        fig.legend(handles, names, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=min(2, len(handles)), fontsize=9,
                   frameon=False)
    name = f"{cfg.get('stem') or 'results'}-{slug(view['id'])}"
    out.mkdir(parents=True, exist_ok=True)
    fig.savefig(out / f"{name}.png", bbox_inches="tight", dpi=110 if preview else 200)
    files = [f"{name}.png"]
    if not preview:
        fig.savefig(out / f"{name}.pdf", bbox_inches="tight")
        files.append(f"{name}.pdf")
    plt.close(fig)
    return files


# --------------------------------------------------------------------------- command line

def main(argv):
    views = {argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--view"}
    argv = [a for i, a in enumerate(argv) if a != "--view" and (i == 0 or argv[i - 1] != "--view")]
    flags = {a for a in argv if a.startswith("--")}
    args = [a for a in argv if not a.startswith("--")]
    out = Path(args[0]) if args else Path(".")
    chosen = views or "--views" in flags
    want_table = "--table" in flags or not (flags & {"--table", "--plot"} or chosen)
    want_plot = "--plot" in flags or not (flags & {"--table", "--plot"} or chosen)
    df = load_runs(CONFIG)
    runs = _records(df)
    order = label_runs(runs, CONFIG["dimensions"])
    s = summary(runs, order, CONFIG)
    print(f"{len(runs)} runs of {CONFIG['experiment']} (filter: {CONFIG.get('filter') or 'none'})")
    for name, vals in order.items():
        print(f"  {name}: " + ", ".join(f"{v} ({sum(r[name] == v for r in runs)})" for v in vals))
    out.mkdir(parents=True, exist_ok=True)
    stem = CONFIG.get("stem") or "results"
    if want_table:
        (out / f"{stem}-table.tex").write_text(latex_table(s, CONFIG))
        print("wrote", f"{stem}-table.tex")
    if want_plot:
        for f in plot(s, CONFIG, out, preview="--preview" in flags):
            print("wrote", f)
    for view in CONFIG.get("views") or []:
        if view.get("type") == "curves" and ("--views" in flags or view["id"] in views or not (flags or views)):
            for f in curves_plot(curve_data(runs, order, CONFIG, view), CONFIG, view, out, preview="--preview" in flags):
                print("wrote", f)
    (out / f"{stem}-summary.json").write_text(json.dumps(s, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
