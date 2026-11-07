"""Base configs written by the launcher, and XPs imported from MLflow.

Generated configs live in <root>/scripts/<XP>/configs/<xp>.yaml (next to the XP's scripts), outside git. The script copies that folder into the
container and puts it back after `git checkout $COMMIT`, so a generated config works whatever the commit (its
subconfigs, if any, come from the commit). An XP uses one through a "Base config" block that sets the config variable.

Import: every run of an MLflow experiment stores its final configuration (config.yaml in its artifacts: defaults filled,
subconfigs merged, variables applied). The base config is the first run's; what differs between runs becomes blocks,
one per thing that changes: a value, a list, or a whole object when its class changes (its arguments go with it). The
commit the runs used becomes a "Commit" block. Importing again into the same XP adds the values not seen before.
"""
import copy
import os
import posixpath
import tempfile
from pathlib import Path

import yaml

import core
import preview

NOISE = ("settings.experiment", "settings.log_dir", "settings.debug")


def gen_path(cfg, xp_name):
    """(relative path, absolute path) of the generated config of an XP: scripts/<XP>/configs/<xp>.yaml."""
    rel = f"{core.gen_dir(cfg, xp_name)}/{core.slug(xp_name)}.yaml"
    return rel, core.project_root(cfg) / rel


def is_generated(cfg, path):
    return core.is_generated(cfg, path)


def _check_rel(cfg, rel):
    rel = posixpath.normpath(rel or "")
    if not is_generated(cfg, rel) or not rel.endswith((".yaml", ".yml")):
        raise ValueError(f"only files of {cfg['project']['scripts_dir']}/<XP>/configs/ (.yaml) are written by the launcher")
    return rel


def read(cfg, rel):
    rel = _check_rel(cfg, rel)
    return (core.project_root(cfg) / rel).read_text()


def write(cfg, rel, text):
    """Save a generated config (it must be a YAML mapping)."""
    rel = _check_rel(cfg, rel)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ValueError(f"not valid YAML: {err}") from err
    if not isinstance(data, dict):
        raise ValueError("a config is a mapping (task: ..., settings: ...)")
    path = core.project_root(cfg) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n")
    return rel


SKELETON = """# Base config written in the XP launcher: fill it with the Arguments browser (Save to: the base config file).
# Required values still missing are shown in red in the Config preview. Block variables are applied on top of it.
settings:
  experiment: experiment
"""


def _rebase_subconfigs(node, old_dir, new_dir):
    """Rewrite relative subconfig paths for a file that moves from old_dir to new_dir."""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).startswith("subconfig") and isinstance(v, str):
                target = posixpath.normpath(posixpath.join(old_dir, v))
                node[k] = posixpath.relpath(target, new_dir)
            else:
                _rebase_subconfigs(v, old_dir, new_dir)
    elif isinstance(node, list):
        for x in node:
            _rebase_subconfigs(x, old_dir, new_dir)


def create(cfg, sel, source="", from_path=""):
    """Write the generated config of the selected XP (empty skeleton, or a copy of `from_path` read as the script
    will see it) and make the XP use it. Returns (relative path, the new config to save)."""
    xp = core.xp_of(cfg, sel["xp"])
    rel, path = gen_path(cfg, xp["name"])
    if source == "copy":
        table, _ = core.collect(cfg, sel)
        read_file, _ = preview._reader(cfg, table)       # pylint: disable=protected-access
        data = yaml.safe_load(read(cfg, from_path) if is_generated(cfg, from_path) else read_file(from_path)) or {}
        _rebase_subconfigs(data, posixpath.dirname(from_path), posixpath.dirname(rel))
        text = f"# Base config of {xp['name']}, from {from_path} (written in the XP launcher)\n" + yaml.safe_dump(data, sort_keys=False)
    else:
        text = SKELETON.replace("experiment: experiment", f"experiment: {xp['name']}")
    if path.exists():
        raise ValueError(f"{rel} exists already: edit it, or delete it first")
    write(cfg, rel, text)
    return rel, use_config(cfg, sel["xp"], rel)


def use_config(cfg, xp_name, rel, label=None):
    """The config with the XP's "Base config" block setting the config variable to `rel` (added if needed)."""
    cfg = copy.deepcopy(cfg)
    xp = core.xp_of(cfg, xp_name)
    var = cfg["project"]["config_var"]
    name = label or Path(rel).stem
    block = next((b for b in xp["blocks"] if b["id"] == "base_config"), None)
    if block is None:
        block = {"id": "base_config", "label": "Base config", "path": "", "rows": []}
        xp["blocks"].insert(0, block)
    row = next((r for r in block["rows"] if r["vars"].get(var) == rel), None)
    if row is None:
        n = name
        while any(r["name"] == n for r in block["rows"]):
            n += "+"
        block["rows"].insert(0, {"name": n, "vars": {var: rel}})
    return cfg


# --------------------------------------------------------------------------- import from MLflow

def experiments(cfg):
    import mlflow                                           # pylint: disable=import-outside-toplevel
    client = mlflow.tracking.MlflowClient(cfg["project"]["tracking_uri"])
    return [{"name": e.name, "id": e.experiment_id} for e in client.search_experiments() if e.name != "Default"]


def _configs(cfg, experiment, limit=500):
    """[(run info, config dict)] of the finished runs that have a config.yaml artifact."""
    import mlflow                                           # pylint: disable=import-outside-toplevel
    client = mlflow.tracking.MlflowClient(cfg["project"]["tracking_uri"])
    exp = client.get_experiment_by_name(experiment)
    if exp is None:
        raise ValueError(f"no MLflow experiment named '{experiment}'")
    out = []
    for run in client.search_runs([exp.experiment_id], filter_string="attributes.status = 'FINISHED'", max_results=limit):
        uri = run.info.artifact_uri or ""
        local = Path(uri.replace("file://", "")) / "config.yaml"
        try:
            if local.is_file():
                text = local.read_text()
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    text = Path(client.download_artifacts(run.info.run_id, "config.yaml", tmp)).read_text()
            data = yaml.safe_load(text)
        except Exception:  # pylint: disable=broad-except  # a run without config.yaml: skipped
            continue
        if isinstance(data, dict):
            out.append(({"id": run.info.run_id, "commit": run.data.tags.get("mlflow.source.git.commit", ""),
                         "name": run.data.tags.get("mlflow.runName", "")}, data))
    if not out:
        raise ValueError(f"no finished run of '{experiment}' has a config.yaml artifact")
    return out


_MISSING = object()


def _get(node, path):
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return _MISSING
        node = node[k]
    return node


def varying(configs):
    """[(path, [value per run])] of what differs between the runs' configs: a whole object when its class differs,
    else its fields, recursively; lists and scalars as they are."""
    out = []

    def walk(path, nodes):
        present = [n for n in nodes if n is not _MISSING]
        if all(isinstance(n, dict) for n in present) and present:
            classes = {n.get("class_path") for n in present}
            if len(classes) > 1 or len(present) < len(nodes):  # class switch (or object missing in some runs)
                if len({yaml.safe_dump(n, sort_keys=True) if n is not _MISSING else "" for n in nodes}) > 1:
                    out.append((path, nodes))
                return
            keys = list(dict.fromkeys(k for n in present for k in n))
            for k in keys:
                walk(path + [k], [n.get(k, _MISSING) for n in present])
            return
        if len({yaml.safe_dump(n, sort_keys=True) if n is not _MISSING else "\0" for n in nodes}) > 1:
            out.append((path, nodes))

    walk([], [c for _, c in configs])
    return [(p, v) for p, v in out if ".".join(x for x in p if x != "init_args") not in NOISE]


def _classes(v):
    """Short names of the classes inside a value, in order."""
    if isinstance(v, dict):
        out = [v["class_path"].rpartition(".")[2]] if isinstance(v.get("class_path"), str) else []
        for x in v.values():
            out += _classes(x)
        return out
    if isinstance(v, list):
        return [c for x in v for c in _classes(x)]
    return []


def _option_name(v):
    if v is _MISSING:
        return "(not set)"
    if isinstance(v, dict) and isinstance(v.get("class_path"), str):
        inner = _classes(v.get("init_args") or {})
        name = v["class_path"].rpartition(".")[2]
        return f"{name}({', '.join(inner)})" if inner else name
    if isinstance(v, list) and _classes(v):
        return ", ".join(_classes(v))
    text = preview.fmt(v)
    return text if len(text) <= 40 else text[:37] + "..."


def _env(cfg, path):
    p = cfg["project"]
    return p["env_prefix"] + p["env_sep"].join(s.upper() for s in path)


def import_xp(cfg, experiment, xp_name=None):
    """A new XP from an MLflow experiment, or the new values added to it when it exists. Returns (cfg, summary)."""
    configs = _configs(cfg, experiment)
    cfg = copy.deepcopy(cfg)
    name = xp_name or experiment
    xp = next((x for x in cfg["xps"] if x["name"] == name), None)
    created = xp is None
    rel, path = gen_path(cfg, name)
    if created:
        xp = {"name": name, "blocks": [], "results": {"experiment": experiment}}
        cfg["xps"].append(xp)
        base = copy.deepcopy(configs[0][1])
        text = (f"# Base config of {name}: the configuration of the MLflow run {configs[0][0]['id']} ({experiment}),\n"
                "# imported by the XP launcher. What differs between the runs is set by the blocks.\n")
        write(cfg, rel, text + yaml.safe_dump(base, sort_keys=False))
        cfg = use_config(cfg, name, rel, label=f"{experiment} (imported)")
        xp = core.xp_of(cfg, name)
    elif not path.exists():
        raise ValueError(f"{rel} is missing: the XP '{name}' was not imported, or its base config was deleted")
    added = {"blocks": 0, "options": 0}

    def block_for(bid, label):
        b = next((x for x in xp["blocks"] if x["id"] == bid), None)
        if b is None:
            b = {"id": bid, "label": label, "path": "", "rows": []}
            xp["blocks"].append(b)
            added["blocks"] += 1
        return b

    def add_option(b, oname, var_values):
        for r in b["rows"]:
            if r["vars"] == var_values:
                return
        n, i = oname, 2
        while any(r["name"] == n for r in b["rows"]):
            n, i = f"{oname} ({i})", i + 1
        b["rows"].append({"name": n, "vars": var_values})
        added["options"] += 1

    commits = list(dict.fromkeys(r["commit"] for r, _ in configs if r["commit"]))
    if commits:
        b = block_for("commit", "Commit")
        for c in commits:
            add_option(b, c[:8], {cfg["project"]["commit_var"]: c})
    for p, values in varying(configs):
        if any(v is _MISSING for v in values):
            values = [v for v in values if v is not _MISSING]   # a variable cannot remove a key: skip "not set"
        label = ".".join(x for x in p if x != "init_args")
        b = block_for(core.slug(label), label)
        for v in values:
            add_option(b, _option_name(v), {_env(cfg, p): preview.fmt(v)})
    for b in xp["blocks"]:
        if not b["rows"]:
            b["rows"].append({"name": "none", "vars": {}})
    return core.normalize(cfg), {"created": created, "xp": name, "runs": len(configs), "config": rel, **added}


# --------------------------------------------------------------------------- merging blocks

def merge(cfg, xp_name, first, second, limit=64):
    """Replace two blocks of an XP by one whose options are their combinations (delete the ones not needed)."""
    cfg = copy.deepcopy(cfg)
    xp = core.xp_of(cfg, xp_name)
    a, b = core.block_of(xp, first), core.block_of(xp, second)
    if a is b:
        raise ValueError("choose another block")
    if len(a["rows"]) * len(b["rows"]) > limit:
        raise ValueError(f"{len(a['rows'])} x {len(b['rows'])} options: more than {limit} combinations")
    prefix = cfg["project"]["env_prefix"]
    rows = []
    for ra in a["rows"]:
        for rb in b["rows"]:
            v = {core.full_name(a, k, prefix): x for k, x in ra["vars"].items()}
            v.update({core.full_name(b, k, prefix): x for k, x in rb["vars"].items()})
            rows.append({"name": f"{ra['name']} · {rb['name']}", "vars": v})
    merged = {"id": a["id"], "label": f"{a['label']} + {b['label']}", "path": "", "rows": rows}
    at = xp["blocks"].index(a)
    xp["blocks"] = [x for x in xp["blocks"] if x is not a and x is not b]
    xp["blocks"].insert(min(at, len(xp["blocks"])), merged)
    return core.normalize(cfg)


def delete_file(cfg, rel):
    rel = _check_rel(cfg, rel)
    p = core.project_root(cfg) / rel
    if p.exists():
        os.remove(p)


def _header(text):
    lines = []
    for ln in text.splitlines():
        if not ln.startswith("#"):
            break
        lines.append(ln)
    return "\n".join(lines) + ("\n" if lines else "")


def set_value(cfg, rel, name, value):
    """Set the field of the APP_* variable `name` in a generated config (objects on the way created; a class given as a
    plain string becomes {class_path: ...}); value is YAML text. Leading comments are kept, others are not."""
    text = read(cfg, rel)
    data = yaml.safe_load(text) or {}
    p = cfg["project"]
    segs = [x.lower() for x in name[len(p["env_prefix"]):].split(p["env_sep"]) if x]
    node = data
    for seg in segs[:-1]:
        if seg == "init_args" and preview.is_flat(node):      # `metrics: {metric_list: ...}`: no init_args to add
            continue
        key = next((k for k in node if str(k).lower() == seg), seg)
        if isinstance(node.get(key), str) and seg != "init_args":
            node[key] = {"class_path": node[key]}
        elif not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    last = next((k for k in node if str(k).lower() == segs[-1]), segs[-1])
    node[last] = preview.parse_value(value)
    write(cfg, rel, _header(text) + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def unset_value(cfg, rel, name):
    """Remove the field of `name` from a generated config (empty objects left on the way are removed too)."""
    text = read(cfg, rel)
    data = yaml.safe_load(text) or {}
    p = cfg["project"]
    segs = [x.lower() for x in name[len(p["env_prefix"]):].split(p["env_sep"]) if x]
    trail, node = [], data
    for seg in segs[:-1]:
        if seg == "init_args" and preview.is_flat(node):
            continue
        key = next((k for k in node if str(k).lower() == seg), None) if isinstance(node, dict) else None
        if key is None:
            return
        trail.append((node, key))
        node = node[key]
    key = next((k for k in node if str(k).lower() == segs[-1]), None) if isinstance(node, dict) else None
    if key is None:
        return
    del node[key]
    for parent, k in reversed(trail):
        if parent[k] == {}:
            del parent[k]
        else:
            break
    write(cfg, rel, _header(text) + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
