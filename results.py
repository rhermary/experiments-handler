"""Results tab: the report of an XP (its MLflow runs, labelled by dimensions) and the script that makes it.

Dimensions start from the blocks of the XP: for each block, what distinguishes its options in the program's
configuration (the APP_* variables whose values differ between the options, $VARIABLES expanded) becomes an MLflow
filter per option (params.task.model.classifier.cls = '...': INIT_ARGS dropped, CLASS_PATH -> cls, lists and objects
matched with LIKE). A variable that reads another block's variable is left out (it would change with that block).
They can then be edited freely (report_template.py describes the syntax) and are saved with the XP.
The labelling, the summary, the LaTeX table and the plots are the code of report_template.py, used here for the preview
and written, with the XP's settings, into the script the server runs.
"""
import copy
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time


import core
import preview

DEFAULT_METRICS = (("aupr/max", "AUPR"), ("auroc/max", "AUROC"), ("f1score/max/youden", "F1"))
CACHE_S = 60
NOISE = re.compile(r"(^|\.)(log_dir|experiment|checkpoint_path|dataset_dir|cache_dir|description|run_name)$")
_LOCK = threading.Lock()
_RUNS = {}                                                  # (uri, experiment) -> (time, DataFrame)


# --------------------------------------------------------------------------- what each option sets

def param_names(cfg, name, value):
    """{param: expected value} that the variable `name` = `value` (YAML text, expanded) gives in MLflow."""
    p, sep = cfg["project"]["env_prefix"], cfg["project"]["env_sep"]
    segs = [s.lower() for s in name[len(p):].split(sep) if s and s.upper() != "INIT_ARGS"]
    if segs and segs[-1] == "class_path":
        segs[-1] = "cls"
    out = {}

    def flat(prefix, v):
        if isinstance(v, dict) and ("class_path" in v or "init_args" in v):
            if "class_path" in v:
                out[prefix + ".cls"] = v["class_path"]
            for k, x in (v.get("init_args") or {}).items():
                flat(f"{prefix}.{k}", x)
        else:
            out[prefix] = v

    flat(".".join(segs), preview.parse_value(value))
    return out


REF = re.compile(r"\$\{?([A-Z_][A-Z0-9_]*)")


def config_params(cfg, table):
    """{param: value} of the config the program gets for a selection (its base config file with the variables
    applied), flattened as the runs log it: task.cls, task.epochs, task.model.adapter.adapters (a list as it is)."""
    base, _, _ = preview.load_base(cfg, table)
    tree, _, _ = preview.apply(base, preview.overrides(cfg, table))
    out = {}

    def walk(prefix, v):
        if isinstance(v, dict) and ("class_path" in v or "init_args" in v):
            if "class_path" in v:
                out[prefix + ".cls"] = v["class_path"]
            for k, x in (v.get("init_args") or {}).items():
                walk(f"{prefix}.{k}", x)
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(f"{prefix}.{k}" if prefix else str(k), x)
        else:
            out[prefix] = v
    walk("", tree if isinstance(tree, dict) else {})
    return out


def signatures(cfg, sel):
    """For each block of the XP: {option name: {param: expected value}} (what distinguishes the options).

    A variable identifies the options of a block when its value differs between them and it belongs to this block:
    set by the block, or set elsewhere but reading only this block's variables ($DATASET). A value that reads a
    variable of another block (a Detector option using $PERC_20K of the Dataset) would change with that other block,
    so it identifies neither."""
    xp = core.xp_of(cfg, sel["xp"])
    prefix, out = cfg["project"]["env_prefix"], {}
    owner = {}
    for b in xp["blocks"]:
        for r in b["rows"]:
            for k in r["vars"]:
                owner.setdefault(core.full_name(b, k, prefix), set()).add(b["id"])
    for b in xp["blocks"]:
        values, raws, tables = {}, {}, {}
        for r in b["rows"]:
            s = copy.deepcopy(sel)
            s.setdefault("blocks", {})[b["id"]] = r["name"]
            table, _ = core.collect(cfg, s)
            tables[r["name"]] = table
            keep = [n for n in table if n.startswith(prefix) and not preview.is_subconfig_var(cfg, n)]
            values[r["name"]] = {n: preview.expand(table[n]["value"], table) for n in keep}
            raws[r["name"]] = {n: table[n]["value"] for n in keep}
        names = set().union(*values.values()) if values else set()
        differ = [n for n in names if len({v.get(n) for v in values.values()}) > 1]
        sig = {}
        for r in b["rows"]:
            params = {}
            for n in differ:
                v, raw = values[r["name"]].get(n), raws[r["name"]].get(n, "")
                if v is None or re.search(r"\$\{?[a-z_(]", v):    # changes with the loop ($window): not an identity
                    continue
                refs = set().union(*(owner.get(x, set()) for x in REF.findall(raw))) if REF.search(raw) else set()
                if n in owner and b["id"] not in owner[n]:
                    continue                                 # set by another block
                if refs - {b["id"]}:
                    continue                                 # reads a variable of another block
                params.update(param_names(cfg, n, v))
            sig[r["name"]] = params
        # options that choose another base config file: what differs in the configs they give
        var = cfg["project"]["config_var"]
        files = {r["name"]: (tables[r["name"]].get(var) or {}).get("value", "") for r in b["rows"]}
        if len(set(files.values())) > 1 and var in set().union(*(r["vars"] for r in b["rows"])):
            try:
                flat = {name: config_params(cfg, tables[name]) for name in files}
            except (ValueError, OSError):
                flat = {}
            keys = set().union(*flat.values()) if flat else set()
            for k in sorted(keys):
                vals = [flat[name].get(k) for name in flat]
                if len({repr(v) for v in vals}) < 2 or any(isinstance(v, str) and "$" in v for v in vals):
                    continue                                 # the same everywhere, or changes with the loop
                for name in flat:
                    if k not in sig[name] and flat[name].get(k) is not None:
                        sig[name][k] = flat[name][k]
        out[b["id"]] = sig
    return out


# --------------------------------------------------------------------------- runs

def default_metrics(columns):
    cols = [c[len("metrics."):] for c in columns if c.startswith("metrics.")]
    out = []
    for suffix, lab in DEFAULT_METRICS:
        cands = [c for c in cols if c.endswith("test/" + suffix)]
        if cands:
            out.append({"key": min(cands, key=len) if not any(c.startswith("untrained/") for c in cands)
                        else next(c for c in cands if c.startswith("untrained/")), "label": lab})
    return out


def experiment_of(cfg, sel):
    table, _ = core.collect(cfg, sel)
    xp = core.xp_of(cfg, sel["xp"])
    if (xp.get("results") or {}).get("experiment"):
        return xp["results"]["experiment"]
    e = table.get("APP_SETTINGS__EXPERIMENT")
    return preview.expand(e["value"], table) if e else xp["name"]


# --------------------------------------------------------------------------- LaTeX

PREAMBLE = r"""\documentclass[border=4pt,varwidth=40cm]{standalone}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage{booktabs}
\usepackage{multirow}
\usepackage{amsmath}
\begin{document}
"""


def render_latex(body):
    """SVG of a LaTeX fragment (a tabular), compiled with latex + dvisvgm (glyphs as paths, no shell escape)."""
    if not shutil.which("latex") or not shutil.which("dvisvgm"):
        raise ValueError("latex and dvisvgm are needed to render the table")
    with tempfile.TemporaryDirectory(prefix="xp-latex-") as tmp:
        tex = os.path.join(tmp, "t.tex")
        with open(tex, "w", encoding="utf8") as f:
            f.write(PREAMBLE + body + "\n\\end{document}\n")
        r = subprocess.run(["latex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "t.tex"],
                           cwd=tmp, capture_output=True, text=True, timeout=30, check=False)
        if r.returncode != 0:
            err = [ln for ln in r.stdout.splitlines() if ln.startswith("!") or ln.startswith("l.")]
            raise ValueError("LaTeX error: " + (" ".join(err[:4]) or r.stdout[-400:]))
        r = subprocess.run(["dvisvgm", "--no-fonts", "--exact-bbox", "-o", "t.svg", "t.dvi"],
                           cwd=tmp, capture_output=True, text=True, timeout=30, check=False)
        if r.returncode != 0:
            raise ValueError("dvisvgm: " + r.stderr[-300:])
        with open(os.path.join(tmp, "t.svg"), encoding="utf8") as f:
            return f.read()


# --------------------------------------------------------------------------- report: dimensions, preview, script

import report_template as T                                # noqa: E402  # the code shared with the generated script

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.py")
DEFAULT_FILTER = "attributes.status = 'FINISHED'"


def _literal(v):
    """A value as MLflow logs it (str of the Python value)."""
    return str(v)


def _leaves(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from _leaves(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _leaves(x)
    elif v is not None:
        yield str(v)


def rule_of(expected, prefix="", others=()):
    """MLflow filter for what an option sets: params.K = 'v' (lists and objects: LIKE their values in order);
    `prefix` "ckpt.": the same, tested on a linked run (ckpt.params.K = 'v'). `others`: what the other options of
    the block set: a null that they set to values means "anything else" (a null seed is replaced by a random one)."""
    conds = []
    for k, v in expected.items():
        key = f"{prefix}params.{k}"
        if v is None:
            taken = sorted({_literal(o[k]) for o in others if o.get(k) is not None and not isinstance(o[k], (dict, list, tuple))})
            if taken:
                conds.append(f"{key} NOT IN (" + ", ".join("'" + t.replace("'", "\\'") + "'" for t in taken) + ")")
                continue
        if isinstance(v, (dict, list, tuple)):
            # objects: the classes in their order, then each 'key': value pair anywhere (MLflow logs the str() of the
            # value, whose key order may differ from the option's); plain lists: their values in order
            classes = [str(c) for _, c in _pairs(v) if _ == "class_path"]
            pairs = [(a, b) for a, b in _pairs(v) if a is not None and a not in ("class_path", "init_args")]
            if classes or pairs:
                if classes:
                    conds.append(f"{key} LIKE '%{'%'.join(c.replace(chr(39), '') for c in classes)}%'")
                for a, b in pairs:
                    text = f"'{a}': {b!r}".replace("\\", "\\\\").replace('"', '\\"')
                    conds.append(f'{key} LIKE "%{text}%"')
            else:
                parts = [p.replace("'", "") for p in _leaves(v)]
                conds.append(f"{key} LIKE '%{'%'.join(parts)}%'" if parts else f"{key} = '{_literal(v)}'")
        else:
            conds.append(f"{key} = '{_literal(v).replace(chr(39), chr(92) + chr(39))}'")
    return " and ".join(conds)


def _pairs(v, key=None):
    """(key, scalar) of every leaf of nested dicts and lists (key None for list items that are scalars)."""
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, (dict, list, tuple)):
                yield from _pairs(x, k)
            else:
                yield str(k), x
    elif isinstance(v, (list, tuple)):
        for x in v:
            if isinstance(x, (dict, list, tuple)):
                yield from _pairs(x, key)
            else:
                yield None, x


def _rule_rank(v):
    """Order of the values of a dimension (the first rule a run matches wins): an option that sets nothing matches
    anything, and one that sets a null (= 'None', NOT IN) matches runs that logged nothing or anything else: after
    the others."""
    return (v["rule"] == "", "= 'None'" in v["rule"] or " NOT IN (" in v["rule"])


def _quote(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _logged(expected, prefix=""):
    """The parameters an option gives, as a run logs them (str() of the value)."""
    return {f"{prefix}params.{k}": (str(v) if isinstance(v, (dict, list, tuple)) else _literal(v))
            for k, v in expected.items() if v is not None}


def _exclusive(values, expected, prefix=""):
    """When the rule of an option also matches what another option logs (Conv1D matches Conv1D > Perceiver), it
    would take that option's runs: add the negation of a condition of the other's rule that the option itself fails
    (adapters NOT LIKE '%Conv1D%Perceiver%'). `expected`: {label: what the option sets}."""
    neg = {"LIKE": "NOT LIKE", "=": "!=", "IN": "NOT IN"}
    parsed = {}
    for v in values:
        try:
            parsed[v["label"]] = T.parse_rule(v["rule"])
        except ValueError:
            parsed[v["label"]] = None
    logged = {v["label"]: _logged(expected.get(v["label"]) or {}, prefix) for v in values}
    for v in values:
        mine = parsed[v["label"]]
        if not mine:
            continue
        extra = []
        for w in values:
            other = parsed[w["label"]]
            if w is v or not other or not T.matches(logged[w["label"]], mine) or not T.matches(logged[w["label"]], other):
                continue                                     # this rule does not take the other's runs
            cond = next((c for c in other if c[1] in neg and not T.matches(logged[v["label"]], [c])), None)
            if cond is None:
                continue
            col, op, want = cond
            if op == "IN":
                extra.append(f"{col} NOT IN (" + ", ".join(_quote(x) for x in want) + ")")
            else:
                extra.append(f"{col} {neg[op]} {_quote(want)}")
        if extra:
            v["rule"] = " and ".join([v["rule"], *extra])
    return values


def _rule_columns(rule):
    try:
        return {c for c, _, _ in T.parse_rule(rule)}
    except ValueError:
        return set()


def auto_dimensions(cfg, sel, links=(), id_cols=(), df=None):
    """One dimension per block of the XP, from what its options set (the rules can then be edited).

    With links: a block that only sets which run is linked (a checkpoint path) is replaced by what the linked runs are:
    the dimensions of the XP that made them (the XP whose MLflow experiment they are in), else their description."""
    xp = core.xp_of(cfg, sel["xp"])
    sigs = signatures(cfg, sel)
    out, names = [], set()
    for b in xp["blocks"]:
        rows = [r for r in b["rows"] if not r.get("loop_all")]   # "all (N)" of a list: not a value a run can have
        values = _exclusive([{"label": r["name"], "rule": rule_of(sigs[b["id"]][r["name"]], others=[sigs[b["id"]][o["name"]] for o in rows if o is not r])}
                             for r in rows], sigs[b["id"]])
        values.sort(key=_rule_rank)                           # broad rules last: the first match wins
        cols = set().union(*(_rule_columns(v["rule"]) for v in values)) if values else set()
        if links and cols and cols <= set(id_cols):
            continue                                          # e.g. the checkpoint path: replaced by the link
        out.append({"name": b["label"], "values": values, "block": b["id"]})
        names.add(b["label"])
    for link in links:
        n = link["name"]
        exp = None
        if df is not None and f"{n}.experiment" in df.columns:
            known = df[f"{n}.experiment"].dropna()
            exp = known.mode().iloc[0] if len(known) else None
        maker = None
        for x in cfg["xps"]:
            if x["name"] != xp["name"] and exp and experiment_of(cfg, {"xp": x["name"], "blocks": {}, "loop": None, "values": {}}) == exp:
                maker = x
                break
        added = False
        if maker is not None:                                 # the XP variants of the parent runs
            msel = {"xp": maker["name"], "blocks": {}, "loop": None, "values": {}}
            msigs = signatures(cfg, msel)
            for b in maker["blocks"]:
                if len(b["rows"]) < 2:
                    continue
                values = _exclusive([{"label": r["name"], "rule": rule_of(msigs[b["id"]][r["name"]], prefix=f"{n}.",
                                                                  others=[msigs[b["id"]][o["name"]] for o in b["rows"] if o is not r])} for r in b["rows"]],
                                    msigs[b["id"]], prefix=f"{n}.")
                if not any(v["rule"] for v in values):
                    continue
                values.sort(key=_rule_rank)
                name = f"{b['label']} ({n})"
                if name not in names:
                    out.append({"name": name, "values": values})
                    names.add(name)
                    added = True
        if not added:                                         # what differs between the parent runs: one per axis
            prefix = f"{n}.axis."
            for col in [c for c in (df.columns if df is not None else []) if c.startswith(prefix)]:
                name = f"{n} {col[len(prefix):]}"
                if name not in names:
                    out.append({"name": name, "param": col})
                    names.add(name)
            out.append({"name": n if n not in names else f"{n} (linked run)", "param": f"{n}.label"})
    return out


def detect_links(df):
    """([{name, param}], id columns): the parameters that hold the MLflow run id of another run (a checkpoint path,
    the config of a run); several parameters naming the same runs make one link."""
    seen, id_cols = {}, set()
    own = list(df["run_id"]) if "run_id" in df.columns else []
    cols = sorted((c for c in df.columns if c.startswith("params.")), key=lambda c: ("checkpoint" not in c and "ckpt" not in c, c))
    for c in cols:
        ids = tuple(T.run_id_in(v) for v in df[c])
        if not any(ids) or all(i is None or i == o for i, o in zip(ids, own)):
            continue
        id_cols.add(c)
        seen.setdefault(ids, c)
    links, taken = [], set()
    for c in seen.values():
        last = c.rsplit(".", 1)[-1]
        base = "ckpt" if ("checkpoint" in last or "ckpt" in last) else (re.sub(r"_?(path|dir|id|file)$", "", last) or "link")
        name, i = base, 2
        while name in taken:
            name, i = f"{base}{i}", i + 1
        taken.add(name)
        links.append({"name": name, "param": c})
    return links, id_cols


def selection_of(xp, sid=None):
    """The selection `sid` of the XP's results (filter, values kept, rows/columns, metrics, views), else the first;
    {"id": "main", "title": "Main"} when there is none yet."""
    sels = (xp.get("results") or {}).get("selections") or [{"id": "main", "title": "Main"}]
    return next((s for s in sels if s["id"] == sid), sels[0])


def report_config(cfg, sel, dimensions=None, fresh=False):
    """CONFIG of the report script for the selected XP and selection (sel["selection"]): its saved settings, else
    defaults from the blocks and the runs."""
    xp = core.xp_of(cfg, sel["xp"])
    top = xp.get("results") or {}
    res = selection_of(xp, sel.get("selection"))           # filter, layout, metrics, views, readme
    base = {"tracking_uri": cfg["project"]["tracking_uri"], "experiment": experiment_of(cfg, sel),
            "filter": res.get("filter", DEFAULT_FILTER), "links": []}
    raw = _runs_df(base, fresh)
    found, id_cols = detect_links(raw) if len(raw) else ([], set())
    links = top["links"] if "links" in top else found
    dims = dimensions if dimensions is not None else top.get("dimensions")
    if dims is None or not dims:
        dims = auto_dimensions(cfg, sel, links, id_cols, _runs_df({**base, "links": links}) if links else None)
    names = [d["name"] for d in dims]
    lay = {"panel": "", "column": "", "rows": [], "hide": {}, "scale": 100, "digits": 1, "std": True, "missing": True,
           **(res.get("layout") or {})}
    if not res.get("layout"):                                # first time: the blocks with several options
        multi = [d["name"] for d in dims if len(d.get("values") or []) > 1]
        lay.update(panel=multi[0] if multi else "", column=multi[1] if len(multi) > 1 else "", rows=multi[2:])
    lay["panel"] = lay["panel"] if lay["panel"] in names else ""
    lay["column"] = lay["column"] if lay["column"] in names else ""
    lay["rows"] = [r for r in lay["rows"] if r in names]
    metrics = res.get("metrics")
    sels = top.get("selections") or [{"id": "main", "title": "Main"}]
    stem = re.sub(r"[^A-Za-z0-9.+-]+", "_", xp["name"]).strip("_") or "results"
    return {**{k: v for k, v in base.items() if k != "links"}, "links": links, "detected_links": found,
            "id_params": sorted(id_cols), "links_saved": "links" in top,
            "selection": res["id"], "selection_title": res["title"],
            "selections": [{"id": s["id"], "title": s["title"]} for s in sels],
            "views": res.get("views") or [], "readme": res.get("readme") or {},
            "metrics": {m["key"]: m["label"] for m in metrics} if metrics else None,
            "dimensions": dims, "layout": lay, "stem": stem + ("" if res["id"] == sels[0]["id"] else "-" + res["id"])}


def _runs_df(conf, fresh=False):
    import mlflow                                           # pylint: disable=import-outside-toplevel
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    key = (conf["tracking_uri"], conf["experiment"], conf["filter"])
    links = conf.get("links") or []
    with _LOCK:
        hit = _RUNS.get(key)
        if hit and not fresh and time.time() - hit[0] < CACHE_S:
            return T.join_links(hit[1], links, conf["tracking_uri"]) if links and len(hit[1]) else hit[1]
    mlflow.set_tracking_uri(conf["tracking_uri"])
    if mlflow.tracking.MlflowClient(conf["tracking_uri"]).get_experiment_by_name(conf["experiment"]) is None:
        raise ValueError(f"no MLflow experiment named '{conf['experiment']}'")
    try:
        df = T.load_runs({**conf, "links": []})
    except Exception as err:  # pylint: disable=broad-except
        raise ValueError(f"MLflow did not accept the filter: {str(err)[:300]}") from err
    if fresh:
        T._PARENTS.clear()                                  # pylint: disable=protected-access
    with _LOCK:
        _RUNS[key] = (time.time(), df)
    return T.join_links(df, links, conf["tracking_uri"]) if links and len(df) else df


def _cond_text(col, op, want):
    if op in ("IS NULL", "IS NOT NULL"):
        return f"{col} {op}"
    if op in ("IN", "NOT IN"):
        return f"{col} {op} (" + ", ".join(repr(w) for w in want) + ")"
    return f"{col} {op} {want!r}"


def _why_excluded(filter_text):
    """run -> [{cond, got}]: the conditions of the filter (MLflow syntax, joined with 'and') a run fails, each tested
    alone with the evaluator of the dimensions."""
    try:
        conds = T.parse_rule(filter_text)
    except ValueError as err:
        reason = [{"cond": "the filter", "got": f"not checked here: {err}"}]
        return lambda run: reason

    def why(run):
        out = []
        for col, op, want in conds:
            if not T.matches(run, [(col, op, want)]):
                got = run.get(col)
                missing = got is None or (isinstance(got, float) and math.isnan(got))
                out.append({"cond": _cond_text(col, op, want), "got": "not set" if missing else str(got)})
        return out or [{"cond": "the filter", "got": "every condition holds here: MLflow left it out (e.g. a type or case difference)"}]
    return why


def _link_info(df, links):
    """Per link: how many runs it resolves, the experiments of the linked runs, and what differs between them
    (the axes: one dimension each)."""
    out = []
    for l in links:
        n = l["name"]
        ids = df[f"{n}.run_id"] if f"{n}.run_id" in df.columns else None
        found = df[f"{n}.status"].notna() if f"{n}.status" in df.columns else None
        exps = df[f"{n}.experiment"].dropna().value_counts().to_dict() if f"{n}.experiment" in df.columns else {}
        axes = []
        for c in [c for c in df.columns if c.startswith(f"{n}.axis.")]:
            vals = df[c].dropna().value_counts().to_dict()
            axes.append({"name": c[len(n) + 6:], "column": c, "values": {str(k): int(v) for k, v in vals.items()}})
        out.append({"name": n, "param": l["param"], "runs": int(len(df)),
                    "with_id": int(ids.notna().sum()) if ids is not None else 0,
                    "found": int(found.sum()) if found is not None else 0,
                    "parents": int(ids.dropna().nunique()) if ids is not None else 0,
                    "experiments": {str(k): int(v) for k, v in exps.items()}, "axes": axes})
    return out


def report_preview(cfg, sel, dimensions=None, fresh=False):
    """Everything the page shows before the script runs: labelled runs, counts per value, summary, LaTeX."""
    conf = report_config(cfg, sel, dimensions, fresh)
    df = _runs_df(conf)
    if conf["metrics"] is None:
        conf["metrics"] = {m["key"]: m["label"] for m in default_metrics(df.columns)}
    runs = T._records(df)                                   # pylint: disable=protected-access
    errors = {}
    for d in conf["dimensions"]:
        for v in d.get("values") or []:
            try:
                T.parse_rule(v.get("rule") or "")
            except ValueError as err:
                errors[f"{d['name']}\u0000{v['label']}"] = str(err)
    if errors:
        return {"config": conf, "errors": errors, "runs": [], "summary": None, "counts": {}, "latex": ""}
    order = T.label_runs(runs, conf["dimensions"])
    s = T.summary(runs, order, conf)
    counts = {name: {v: sum(1 for r in runs if r.get(name) == v) for v in vals} for name, vals in order.items()}
    names = [d["name"] for d in conf["dimensions"]]

    def record(r):
        return {"id": r["run_id"], "status": r.get("status"), "name": r.get("tags.mlflow.runName") or "",
                "start": str(r.get("start_time") or "")[:16], "commit": str(r.get("tags.mlflow.source.git.commit") or "")[:8],
                "labels": {n: r.get(n) for n in names},
                "metrics": {k: r.get("metrics." + k) for k in conf["metrics"]}}
    # the runs the filter leaves out (the experiment's runs without the filter, minus the ones kept)
    excluded, excluded_error = [], ""
    if (conf.get("filter") or "").strip():
        try:
            kept = {r["run_id"] for r in runs}
            others = [r for r in T._records(_runs_df({**conf, "filter": ""}, fresh))  # pylint: disable=protected-access
                      if r["run_id"] not in kept]
            T.label_runs(others, conf["dimensions"])
            why = _why_excluded(conf["filter"])
            excluded = [{**record(r), "why": why(r)} for r in others]
        except ValueError as err:
            excluded_error = str(err)
    return {"config": conf, "order": order, "counts": counts, "summary": s, "latex": T.latex_table(s, conf),
            "runs": [record(r) for r in runs], "excluded": excluded, "excluded_error": excluded_error,
            "params": sorted(c for c in df.columns if c.startswith("params.")),
            "available_metrics": sorted(c[len("metrics."):] for c in df.columns if c.startswith("metrics.")),
            "link_info": _link_info(df, conf["links"]),
            # metrics of the linked runs, per link (for curves of the linked runs: e.g. the training of a checkpoint)
            "link_metrics": {l["name"]: sorted(c[len(l["name"]) + len(".metrics."):] for c in df.columns
                                               if c.startswith(l["name"] + ".metrics.")) for l in conf["links"]},
            "experiment_id": df["experiment_id"].iloc[0] if len(df) else None, "errors": {}}


def curves_preview(cfg, sel, view_id, fresh=False):
    """The data of a curves view (the same function the script uses), for the page to draw."""
    conf = report_config(cfg, sel, None, fresh)
    view = next((v for v in conf["views"] if v["id"] == view_id), None)
    if view is None:
        raise ValueError(f"no view '{view_id}' in this XP")
    if not view["metrics"]:
        return {"metrics": [], "x": view["x"], "panels": []}
    runs = T._records(_runs_df(conf))                       # pylint: disable=protected-access
    order = T.label_runs(runs, conf["dimensions"])
    return T.curve_data(runs, order, conf, view)


def script_text(conf):
    """The report script: the shared code with this XP's CONFIG (every value a Python literal)."""
    with open(TEMPLATE_PATH, encoding="utf8") as f:
        text = f.read()
    import pprint                                           # pylint: disable=import-outside-toplevel
    conf = {k: v for k, v in conf.items() if k not in ("detected_links", "readme", "id_params", "links_saved",
                                                       "selection", "selection_title", "selections")}      # for the page only
    block = "# <CONFIG>\nCONFIG = " + pprint.pformat(conf, width=110, sort_dicts=False) + "\n# </CONFIG>"
    text = re.sub(r"# <CONFIG>.*?# </CONFIG>", lambda _: block, text, count=1, flags=re.S)
    return text.replace("THIS_FILE.py", f"{conf['stem']}-report.py")


_OUTPUTS = {}


def run_script(text, what, timeout=300):
    """Run the report script (written by script_text) for `what` (table | plot | preview); its outputs."""
    import base64                                           # pylint: disable=import-outside-toplevel
    import hashlib                                          # pylint: disable=import-outside-toplevel
    import sys                                              # pylint: disable=import-outside-toplevel
    key = (hashlib.sha256(text.encode()).hexdigest(), what)
    if key in _OUTPUTS:
        return _OUTPUTS[key]
    flags = {"table": ["--table"], "plot": ["--plot"], "preview": ["--plot", "--preview"]}.get(what) \
        or (["--view", what[len("view:"):]] if what.startswith("view:") else None)
    if flags is None:
        raise ValueError(f"unknown output '{what}'")
    with tempfile.TemporaryDirectory(prefix="xp-report-") as tmp:
        path, out = os.path.join(tmp, "report.py"), os.path.join(tmp, "out")
        with open(path, "w", encoding="utf8") as f:
            f.write(text)
        try:
            r = subprocess.run([sys.executable, path, out, *flags], cwd=tmp, capture_output=True, text=True,
                               timeout=timeout, check=False,
                               env={**os.environ, "MPLBACKEND": "Agg", "MPLCONFIGDIR": os.path.join(tmp, "mpl"),
                                    "MLFLOW_DISABLE_AGENT_HINT": "1"})
        except subprocess.TimeoutExpired as err:
            raise ValueError(f"the script ran for more than {timeout} s") from err
        if r.returncode != 0:
            lines = [ln for ln in r.stderr.splitlines() if ln.strip() and "Warning" not in ln]
            raise ValueError("the script failed:\n" + "\n".join(lines[-14:]))
        res = {"log": r.stdout[-3000:], "files": []}
        for name in sorted(os.listdir(out)):
            full = os.path.join(out, name)
            if name.endswith(".tex"):
                with open(full, encoding="utf8") as fh:
                    res["tex"] = fh.read()
                res["tex_svg"] = render_latex("\n".join(l for l in res["tex"].splitlines() if not l.startswith("%")))
            elif name.endswith((".png", ".pdf")):
                with open(full, "rb") as fh:
                    res["files"].append({"name": name, "b64": base64.b64encode(fh.read()).decode()})
    if len(_OUTPUTS) > 30:
        _OUTPUTS.clear()
    _OUTPUTS[key] = res
    return res
