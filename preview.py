"""Preview of the program config: the base YAML (CONFIG) with the APP_* environment variables applied.

An environment variable APP_TASK__INIT_ARGS__BATCH_SIZE=128 sets task.init_args.batch_size. The result is a list of
lines, each marked `same`, `changed` (a value of the file was replaced) or `added` (a key the file does not have, which
is also what a misspelt name looks like).
"""
import copy
import posixpath
import re
import subprocess

import yaml

import core

REF = re.compile(r"\$(?:\{([A-Z_][A-Z0-9_]*)\}|([A-Z_][A-Z0-9_]*))")


SUBCONFIG = "subconfig"


def _reader(cfg, table):
    """(read(relative path) -> text, label) of the files the program sees: git at COMMIT when the script checks it out."""
    root = core.project_root(cfg)
    commit = (table.get(cfg["project"]["commit_var"]) or {}).get("value", "").strip()
    debug = (table.get("DEBUG") or {}).get("value", "0").strip()

    def from_tree(rel):
        file = root / rel
        if not file.is_file():
            raise ValueError(f"{file} not found" + (" (and not in git at the selected commit)" if commit else ""))
        return file.read_text()

    if not commit or debug not in ("0", ""):
        return from_tree, "working tree"

    def from_git(rel):
        if core.is_generated(cfg, rel):                               # written by the launcher: never in git
            return from_tree(rel)
        try:
            r = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{rel}"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return r.stdout
        except (OSError, subprocess.SubprocessError):
            pass
        return from_tree(rel)

    return from_git, f"commit {commit[:8]}"


def read_base(cfg, table):
    """(text, source label) of the base config file itself (without its subconfigs)."""
    var = cfg["project"]["config_var"]
    path = (table.get(var) or {}).get("value", "").strip()
    if not path:
        raise ValueError(f"variable {var} is empty: no base config to preview")
    read, where = _reader(cfg, table)
    return read(path), f"{path} ({where})"


def _merge(a, b):
    """misc.utils.merge_dictionaries: `b` wins, nested dicts are merged."""
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(a[k], v) if isinstance(a.get(k), dict) and isinstance(v, dict) else v
    return out


def _load_subconfigs(node, rel, read, used, depth=0):
    """Parser.load_subconfigs_: `subconfig*` keys (sorted) are loaded first, the other keys are merged on top."""
    if not isinstance(node, dict) or depth > 20:
        return node
    keys = sorted(k for k in node if str(k).startswith(SUBCONFIG))
    out = {}
    for k in keys:
        sub = posixpath.normpath(posixpath.join(posixpath.dirname(rel), str(node[k])))
        try:
            parsed = yaml.safe_load(read(sub)) or {}
        except yaml.YAMLError as err:
            raise ValueError(f"subconfig {sub}: {err}") from err
        used.append(sub)
        out = _merge(out, _load_subconfigs(parsed, sub, read, used, depth + 1))
    for k, v in node.items():
        if k in keys:
            continue
        loaded = _load_subconfigs(v, rel, read, used, depth + 1)
        out[k] = _merge(out[k], loaded) if isinstance(out.get(k), dict) and isinstance(loaded, dict) else loaded
    return out


def is_subconfig_var(cfg, name):
    return name.split(cfg["project"]["env_sep"])[-1].upper().startswith(SUBCONFIG.upper())


def load_base(cfg, table):
    """(tree, source label, notes) of the config as the program parses it, before the APP_* overrides:
    ..._SUBCONFIG variables replace subconfig keys (only where the path exists in the file), then subconfigs are merged."""
    var = cfg["project"]["config_var"]
    path = (table.get(var) or {}).get("value", "").strip()
    if not path:
        raise ValueError(f"variable {var} is empty: no base config to preview")
    read, where = _reader(cfg, table)
    try:
        tree = yaml.safe_load(read(path)) or {}
    except yaml.YAMLError as err:
        raise ValueError(f"{path}: {err}") from err
    prefix, sep, notes = cfg["project"]["env_prefix"], cfg["project"]["env_sep"], []
    empty = core.empty_subconfigs(cfg, table)
    for name, e in table.items():
        if not name.startswith(prefix) or not is_subconfig_var(cfg, name):
            continue
        if name in empty:
            notes.append(f"{name} is empty for this XP ({e['value']} is not set): not passed, the file's subconfigs apply")
            continue
        segs = [s.lower() for s in name[len(prefix):].split(sep) if s]
        node = tree
        for s in segs[:-1]:
            node = node.get(s) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node[segs[-1]] = expand(e["value"], table)
        else:
            notes.append(f"{name} is ignored by the program: {'.'.join(segs[:-1])} is not in {path}")
    used = []
    tree = _load_subconfigs(tree, path, read, used) if isinstance(tree, dict) else {}
    label = f"{path} ({where})" + (f" + {len(used)} subconfig{'s' if len(used) > 1 else ''}" if used else "")
    return tree, label, notes


def expand(value, table, depth=3):
    """Replace $NAME / ${NAME} by the variable's value; other $ forms (loop variables, $(...)) stay."""
    for _ in range(depth):
        new = REF.sub(lambda m: table[m.group(1) or m.group(2)]["value"] if (m.group(1) or m.group(2)) in table else m.group(0), value)
        if new == value:
            break
        value = new
    return value


def parse_value(text):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def overrides(cfg, table):
    prefix, sep = cfg["project"]["env_prefix"], cfg["project"]["env_sep"]
    out = []
    for name, e in table.items():
        if not name.startswith(prefix) or not name[len(prefix):] or is_subconfig_var(cfg, name):
            continue                                        # subconfig variables are applied by load_base
        segs = tuple(s.lower() for s in name[len(prefix):].split(sep) if s)
        val = expand(e["value"], table)
        out.append({"name": name, "path": segs, "value": parse_value(val), "raw": val, "loop": bool(e.get("loop"))})
    return sorted(out, key=lambda o: len(o["path"]))


def _find(d, key):
    for k in d:
        if str(k).lower() == key:
            return k
    return None


def is_flat(node):
    """A class object written without class_path and init_args (jsonargparse: the init args of the declared class),
    e.g. `metrics: {metric_list: [...]}`. A variable's INIT_ARGS step stays at that level (a file mixing both forms
    is refused by jsonargparse; the program applies the variables after jsonargparse made it {class_path, init_args})."""
    return isinstance(node, dict) and bool(node) and not any(str(k).lower() in ("class_path", "init_args") for k in node)


def apply(base, ovs):
    """Apply overrides to `base`; returns (tree, marks, warnings). marks: {path: (status, was, variable)}."""
    tree = base if isinstance(base, dict) else {}
    marks, warnings = {}, []
    for o in ovs:
        node, path = tree, ()
        for seg in o["path"][:-1]:
            if seg == "init_args" and is_flat(node):
                continue
            key = _find(node, seg)
            if key is None:
                key = seg
                node[key] = {}
                marks.setdefault(path + (key,), ("added", None, o["name"]))
            elif not isinstance(node[key], dict):
                # a class given as a plain string (class_path shorthand) becomes {class_path: ...}
                node[key] = {"class_path": node[key]} if isinstance(node[key], str) else {}
            node, path = node[key], path + (key,)
        last = o["path"][-1]
        key = _find(node, last)
        if key is None:
            node[last] = o["value"]
            marks[path + (last,)] = ("added", None, o["name"])
        elif node[key] != o["value"]:
            marks[path + (key,)] = ("changed", node[key], o["name"])
            node[key] = o["value"]
    for a in ovs:
        for b in ovs:
            if len(b["path"]) > len(a["path"]) and b["path"][:len(a["path"])] == a["path"]:
                warnings.append(f"{a['name']} replaces a whole object that {b['name']} also sets inside: check the order jsonargparse applies them")
    return tree, marks, warnings


def fmt(v):
    s = yaml.dump(v, Dumper=yaml.SafeDumper, default_flow_style=True, width=10000, allow_unicode=True, sort_keys=False)
    return s.replace("\n...\n", "").strip() if not isinstance(v, (dict, list)) else s.strip()


def _missing_lines(missing, path, pad):
    if path not in missing:
        return []
    is_class, args = missing[path]
    return [{"t": f"{pad}{'init_args.' if is_class else ''}{a}: ???", "s": "missing", "by": "required: no default and no value"}
            for a in args]


def emit(node, marks, path=(), indent=0, inherited=None, out=None, by_inherited=None, missing=None):
    """Lines of the YAML: s = same (from the file) | changed (a variable replaced the value) | added (a variable set a
    key the file does not have); by = the variable responsible."""
    out = [] if out is None else out
    pad = "  " * indent
    for k, v in node.items():
        p = path + (k,)
        mark = marks.get(p)
        status = mark[0] if mark else inherited
        by = mark[2] if mark and len(mark) > 2 else by_inherited
        was = mark[1] if mark and mark[0] == "changed" else None
        extra = {"by": by} if status and by else {}
        if isinstance(v, dict) and v:
            out.append({"t": f"{pad}{k}:", "s": status or "same", **extra})
            emit(v, marks, p, indent + 1, status, out, by, missing)
            out.extend(_missing_lines(missing or {}, p, pad + "  "))
        elif isinstance(v, list) and any(isinstance(i, (dict, list)) for i in v):
            out.append({"t": f"{pad}{k}:", "s": status or "same", **extra})
            for ln in yaml.dump(v, Dumper=yaml.SafeDumper, default_flow_style=None, width=10000, allow_unicode=True).splitlines():
                out.append({"t": f"{pad}  {ln}", "s": status or "same", **extra})
        else:
            line = {"t": f"{pad}{k}: {fmt(v)}", "s": status or "same", **extra}
            if was is not None:
                text = fmt(was)
                line["was"] = text if len(text) <= 80 else text[:77] + "..."
            out.append(line)
            out.extend(_missing_lines(missing or {}, p, pad + "  "))
    if not path:
        out.extend(_missing_lines(missing or {}, (), pad))
    return out


def build(cfg, table, state=None, warn=()):
    """Preview lines. With `state` (checker.analyse) the base has its subconfigs, the variables the program ignores are
    left out and every problem found is listed; without it, the overrides are applied as written."""
    warnings = list(warn)
    if state is not None:
        base, source = copy.deepcopy(state.base), state.source
        ovs, skip = state.ovs, set()
        for o in ovs:
            res = state.results.get(o["name"])
            if not res:
                continue
            for n in res["notes"]:
                if n["level"] in ("warn", "error"):
                    warnings.append(f"{o['name']}: {n['text']}")
            if any(n["level"] == "error" and n["text"].startswith("ignored") for n in res["notes"]):
                skip.add(o["name"])
        warnings = state.notes + warnings
        tree, marks, _ = apply(base, [o for o in ovs if o["name"] not in skip])
    else:
        base, source, notes = load_base(cfg, table)
        ovs = overrides(cfg, table)
        tree, marks, more = apply(base, ovs)
        warnings += notes + more
    missing = {}
    if state is not None:
        import checker                                       # pylint: disable=import-outside-toplevel
        try:
            missing = checker.missing_all(state)
        except Exception:  # pylint: disable=broad-except  # the highlight is a help: never break the preview
            missing = {}
    lines = emit(tree, marks, missing=missing)
    loop_names = {o["name"] for o in ovs if o["loop"]}
    for ln in lines:
        if ln["s"] != "same" and re.search(r"\$\{?[a-z_]", ln["t"]):
            ln["loop"] = True
    added = sum(1 for ln in lines if ln["s"] == "added")
    changed = sum(1 for ln in lines if ln["s"] == "changed")
    if loop_names:
        warnings.append("Values that use a loop variable ($window, $seed, ...) are shown as written; they change on every run of the loop.")
    return {"source": source, "lines": lines, "warnings": warnings, "changed": changed, "added": added,
            "missing": sum(1 for ln in lines if ln["s"] == "missing"),
            "overrides": len(ovs)}


class _PrettyDumper(yaml.SafeDumper):
    """Block style for mappings, flow style for lists of plain values ([64, 128])."""

    def ignore_aliases(self, data):
        return True


def _represent_list(dumper, data):
    flat = all(not isinstance(x, (dict, list)) for x in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flat)


_PrettyDumper.add_representer(list, _represent_list)


def pretty(text):
    """`text` as block YAML when it is a dict or a list of objects written in flow style (e.g. on one line);
    unchanged otherwise, or when the result would not parse to the same value."""
    if not isinstance(text, str) or not text.strip()[:1] in ("{", "["):
        return text
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    if not isinstance(value, (dict, list)) or not value:
        return text
    if isinstance(value, list) and all(not isinstance(x, (dict, list)) for x in value):
        return text                                         # [64, 128] reads well as it is
    out = yaml.dump(value, Dumper=_PrettyDumper, sort_keys=False, default_flow_style=False, allow_unicode=True,
                    width=100, indent=2).rstrip("\n")
    try:
        return out if yaml.safe_load(out) == value else text
    except yaml.YAMLError:
        return text
