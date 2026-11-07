"""Argument browser: walks the program's config tree (class_path / init_args) with the source index.

For one node of the tree it returns the arguments of the node's class with their type, default, docstring and current
value. The current value is the base config with the APP_* variables of the selection applied (what the preview shows),
so the page can show where each value comes from and set the ones that are missing.

Path names are the argument names only (["task", "model", "extractor"]); `init_args` is inserted where the parent is a
class (jsonargparse's class_path / init_args form), and not where it is a dataclass-like group.
"""
import os
from pathlib import Path

import core
import indexer
import preview

PRIORITY = ("class", "group", "enum", "literal", "bool", "int", "float", "str", "list", "dict", "type", "tuple", "ext",
            "callable")
INNER_CLASS = ("class", "ext")


def find_src(cfg):
    rel = cfg["project"].get("src_dir") or "src"
    tried = []
    bases = [Path(os.environ["XP_SRC"]).expanduser()] if os.environ.get("XP_SRC") else []
    bases += [core.project_root(cfg) / rel, core.HERE.parent.parent / rel]
    for cand in bases:
        tried.append(str(cand))
        if cand.is_dir():
            return cand
    raise ValueError("source folder not found (looked in " + ", ".join(tried) + "): set the project root and source folder in Settings")


def env_name(cfg, path):
    p = cfg["project"]
    return p["env_prefix"] + p["env_sep"].join(s.upper() for s in path)


def lookup(node, key):
    """(found, value) of `key` in a dict, ignoring case like the environment variables do."""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() == key.lower():
                return True, v
    return False, None


def short(fqn):
    return (fqn or "").rpartition(".")[2]


def yamlish(v):
    return "NULL" if v is None else str(v)


def primary(desc):
    """(main member, nullable, members) of a type: unions are flattened, the most 'structured' member wins."""
    members = desc["of"] if desc["k"] == "union" else [desc]
    nullable = any(m["k"] == "none" for m in members)
    members = [m for m in members if m["k"] != "none"] or [{"k": "any"}]
    for k in PRIORITY:
        for m in members:
            if m["k"] == k:
                return m, nullable, members
    return members[0], nullable, members


def ui_kind(prim):
    k = prim["k"]
    if k in ("class", "ext"):
        return "class"
    if k == "enum":
        return "flag" if prim.get("flag") else "enum"
    if k == "type":
        return "classpath"
    if k == "list":
        return "classlist" if primary(prim["of"])[0]["k"] in INNER_CLASS else "list"
    return k if k in ("group", "literal", "bool", "int", "float", "str", "dict") else "any"


class Level:
    """A node of the tree: its arguments, and where the values of its arguments live in the config."""

    def __init__(self, mode, args, container, base_container, child_path, cls=None, missing=(), candidates=()):
        self.mode, self.args, self.container, self.base_container = mode, args, container, base_container
        self.child_path, self.cls, self.missing, self.candidates = child_path, cls, set(missing), list(candidates)
        self.exists = True        # False: a class-typed node that is not in the config (null or absent)

    def find(self, name):
        return next((a for a in self.args if a["name"] == name), None)


def arg_of(idx, p):
    return {"name": p.name, "desc": idx.param_type(p), "type": idx.type_text(p), "doc": p.doc,
            "required": not p.has_default, "default": idx.default_text(p), "origin": p.origin}


def class_path_of(value):
    if isinstance(value, str):
        return value
    return value.get("class_path") if isinstance(value, dict) and isinstance(value.get("class_path"), str) else None


def candidates_of(idx, members):
    out, seen = [], set()
    for m in members:
        base = m.get("fqn") or m.get("name") if m["k"] in INNER_CLASS else None
        for c in (idx.candidates(base) if base else []):
            if c["fqn"] not in seen:
                seen.add(c["fqn"])
                out.append(c)
    return sorted(out, key=lambda c: (c["name"].lower(), c["fqn"]))


def describe(idx, desc, value, base_value, path):
    """The Level of a node of declared type `desc` whose value in the config is `value`."""
    prim, _, members = primary(desc)
    has_class = isinstance(value, dict) and "class_path" in value
    if prim["k"] == "group" and not has_class:
        groups = [m for m in members if m["k"] == "group"]
        pick = groups[0]
        if isinstance(value, dict):                          # e.g. Split | TrainValSplit: the one that accepts the keys
            for g in groups:
                names = {p.name for p in idx.params_of(g["fqn"])[0]}
                if {str(k).lower() for k in value} <= names:
                    pick = g
                    break
        params, missing = idx.params_of(pick["fqn"])
        return Level("group", [arg_of(idx, p) for p in params], value if isinstance(value, dict) else None,
                     base_value if isinstance(base_value, dict) else None, path, pick["fqn"], missing)
    cands = candidates_of(idx, members)
    cls = class_path_of(value)
    concrete = [m["fqn"] for m in members if m["k"] == "class" and m["fqn"] in idx.classes
                and not idx.abstract_set(m["fqn"]) and not idx.is_group(m["fqn"])]
    # jsonargparse: a dict without class_path is the init args of the declared class, when it is concrete
    # (subclasses or not), e.g. `metrics: {metric_list: [...]}` for `metrics: Metrics`; init_args may still be used
    implicit = len(concrete) == 1 and len([m for m in members if m["k"] == "class"]) == 1
    if cls is None:
        if isinstance(value, dict) and len(concrete) == 1:
            cls = concrete[0]
        else:
            cls = concrete[0] if len(concrete) == 1 and not idx.subs.get(concrete[0]) else None
    params, missing = idx.params_of(cls) if cls in idx.classes else ([], set())

    def inner(v):
        if not isinstance(v, dict):
            return None
        ia = v.get("init_args") if isinstance(v.get("init_args"), dict) else None
        if not implicit or "class_path" in v:
            return ia
        rest = {k: x for k, x in v.items() if k not in ("init_args", "dict_kwargs")}
        return {**rest, **ia} if ia is not None and rest else (ia if ia is not None else rest)   # flat keys + init_args
    return Level("class", [arg_of(idx, p) for p in params], inner(value), inner(base_value),
                 path + ["init_args"], cls, missing, cands)


def show(value, kind=None):
    if value is None:
        return "NULL"
    if isinstance(value, dict):
        cp = class_path_of(value)
        return short(cp) + (" {...}" if value.get("init_args") else "") if cp else "{" + ", ".join(map(str, value)) + "}"
    if isinstance(value, str) and kind == "class":
        return short(value)
    if isinstance(value, bool):
        return str(value)
    text = preview.fmt(value)
    return text if len(text) <= 90 else text[:87] + "..."


def edit_text(value, kind):
    if kind in ("class", "classpath"):
        return class_path_of(value) or ("NULL" if value is None else preview.fmt(value))
    if isinstance(value, (dict, list)):
        return preview.fmt(value)
    return "NULL" if value is None else (str(value) if isinstance(value, (str, bool)) else preview.fmt(value))


def choices_of(idx, prim, nullable):
    k, out = prim["k"], None
    if k == "bool":
        out = ["True", "False"]
    elif k == "literal":
        out = [yamlish(v) for v in prim["values"]]
    elif k == "enum" and prim["fqn"] in idx.classes:
        out = idx.enum_members(prim["fqn"])
    if out is not None and nullable and "NULL" not in out:
        out.append("NULL")
    return out


def fields_of(idx, cfg, level, set_by, table, links):
    out, below = [], {}
    for key in set_by:
        for n in range(1, len(key)):
            below[key[:n]] = below.get(key[:n], 0) + 1
    for a in level.args:
        real = level.child_path + [a["name"]]
        key = tuple(s.lower() for s in real)
        found, val = lookup(level.container, a["name"])
        in_cfg, _ = lookup(level.base_container, a["name"])
        prim, nullable, members = primary(a["desc"])
        kind = ui_kind(prim)

        def var(k):
            o = set_by.get(k)
            if not o:
                return None
            e = table.get(o["name"], {})
            return {"name": o["name"], "source": e.get("source", ""), "group": e.get("group", ""), "raw": o["raw"]}

        field = {"name": a["name"], "env": env_name(cfg, real), "type": a["type"], "doc": a["doc"],
                 "origin": short(a["origin"]), "required": a["required"], "default": a["default"][0],
                 "default_yaml": a["default"][1], "kind": kind, "nullable": nullable,
                 "has_value": found, "in_config": in_cfg,
                 "value": show(val, kind) if found else None, "edit": edit_text(val, kind) if found else "",
                 "var": var(key), "class_var": var(key + ("class_path",)) if kind == "class" else None,
                 "within": next((var(key[:n]) for n in range(len(key) - 1, 0, -1) if key[:n] in set_by), None),
                 "linked": links.get(".".join(real)), "below": below.get(key, 0),
                 "open": kind in ("class", "group"), "choices": choices_of(idx, prim, nullable)}
        if kind in ("class", "classpath", "classlist"):
            field["classes"] = candidates_of(idx, primary(prim["of"])[2] if kind != "class" else members)
            field["cls"] = class_path_of(val) if found else None
            if kind == "class" and field["cls"] is None and isinstance(val, dict) and "init_args" in val:
                field["cls"] = ""
        if kind == "flag":
            field["flag"] = True
        out.append(field)
    return out


def root_level(idx, tree, base=None):
    """The top level of the program config. Several parsers may declare top-level arguments (add_argument("--x",
    type=...)); the program's own are the ones the config file uses, so the others (e.g. a helper parser for
    checkpoints) are left out when at least one matches."""
    ref = base if isinstance(base, dict) else tree
    keys = {str(k).lower() for k in ref} if isinstance(ref, dict) else set()
    # the top-level arguments of each parser (module); the program's parser is the one the config's keys match best
    # (a config being written from scratch may not use all of them yet), "task" breaking ties
    groups = {}
    for m in idx.modules.values():
        for flag, expr in m.add_args:
            fqn = idx.resolve(m, "", expr)
            if fqn in idx.classes and flag not in [f for f, _ in groups.get(m.name, [])]:
                groups.setdefault(m.name, []).append((flag, fqn))
    roots = max(groups.values(), key=lambda g: (sum(f.lower() in keys for f, _ in g), any(f == "task" for f, _ in g)),
                default=[])
    if roots:
        args = [{"name": flag, "desc": {"k": "group" if idx.is_group(fqn) else "class", "fqn": fqn}, "type": short(fqn),
                 "doc": indexer.first_line(idx.classes[fqn].doc), "required": True, "default": ("", None), "origin": ""}
                for flag, fqn in roots]
    else:                                                   # no add_argument("--x", type=...) found: use the keys of the file
        args = [{"name": str(k), "desc": {"k": "class", "fqn": class_path_of(v) or ""} if class_path_of(v) else {"k": "any"},
                 "type": "", "doc": "", "required": False, "default": ("", None), "origin": ""} for k, v in (tree.items() if isinstance(tree, dict) else [])]
    return Level("root", args, tree, base if base is not None else tree, [])


def locate(idx, tree, base, names):
    """[(name, Level)] from the root down to the node at `names` (argument names, without init_args)."""
    level = root_level(idx, tree, base)
    chain = [("", level)]
    for name in names:
        a = level.find(name)
        if a is None:
            raise ValueError(f"unknown argument '{name}' (the source may have changed: go back to the root)")
        path = level.child_path + [name]
        found, val = lookup(level.container, name)
        _, bval = lookup(level.base_container, name)
        level = describe(idx, a["desc"], val if found else None, bval, path)
        level.exists = level.mode != "class" or (found and isinstance(val, (dict, str)))
        chain.append((name, level))
    return chain


def browse(cfg, state, names):
    """The node at `names` (argument names from the root) with its arguments and their current values.

    `state` is checker.analyse(): the config as the program sees it (subconfigs loaded, variables the program
    ignores left out)."""
    idx, table, base, effective = state.idx, state.table, state.base, state.effective
    set_by = {o["path"]: o for o in state.ovs}
    chain = locate(idx, effective, base, names)
    level = chain[-1][1]

    links = {}
    for _, v in [lookup(effective, "task")]:
        cp = class_path_of(v)
        if cp in idx.classes:
            links = idx.links(cp)

    real = level.child_path[:-1] if level.mode == "class" else level.child_path
    node = {"names": names, "env": env_name(cfg, real), "child_prefix": env_name(cfg, level.child_path) + cfg["project"]["env_sep"],
            "mode": level.mode, "cls": level.cls, "cls_name": short(level.cls) if level.cls else "",
            "doc": indexer.first_line(idx.classes[level.cls].doc) if level.cls in idx.classes else "",
            "classes": level.candidates, "class_env": env_name(cfg, real + ["class_path"]) if level.mode == "class" else None,
            "class_var": None, "var": None, "exists": level.exists, "warnings": [],
            "known": level.cls in idx.classes or level.mode == "root"}
    for slot, tail in (("class_var", ["class_path"]), ("var", [])):
        k = tuple(s.lower() for s in real + tail)
        if level.mode == "class" and k in set_by:
            e = table.get(set_by[k]["name"], {})
            node[slot] = {"name": set_by[k]["name"], "source": e.get("source", ""), "group": e.get("group", "")}
    if level.cls and level.mode == "class" and level.cls not in idx.classes:
        node["warnings"].append(f"{level.cls} is not in the source folder (library class or typo): only free fields can be set")
    if level.missing:
        node["warnings"].append("Base classes not found in the source folder, so the arguments they add are not listed: "
                                + ", ".join(sorted(level.missing)))
    if level.mode in ("class", "group") and level.cls in idx.classes and not level.missing and isinstance(level.container, dict):
        known = {a["name"].lower() for a in level.args}
        extra = [str(k) for k in level.container if str(k).lower() not in known]
        if extra:
            node["warnings"].append(f"{level.cls.rpartition('.')[2]} has no argument named {', '.join(extra)}: "
                                    "the program stops with 'not accepted' (left over from another class, or a misspelling)")
    fields = fields_of(idx, cfg, level, set_by, table, links)
    return {"node": node, "fields": fields, "source": state.source, "index": idx.summary(),
            "crumbs": [{"name": n or "root", "path": names[:i]} for i, (n, _) in enumerate(chain)]}
