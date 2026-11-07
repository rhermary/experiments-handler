"""What the program does with each APP_* variable of a selection, checked against the classes found in src/.

The program (misc.parser.Parser) loads the base config with its subconfigs, parses it, then applies every APP_*
variable to the parsed config: it walks the path and sets the last key, but only if every object on the way exists.
So a variable is
  ignored   when an object on its path is null or absent in the config (nothing is created), when another variable
            replaces an object on its path as a whole (the parsed object becomes a plain dict: whatever the order,
            the inner value is lost), or when it goes inside a plain value (dict, list, scalar);
  rejected  when the last key is not an argument of the class that holds it, or when CLASS_PATH switches to a class
            that does not accept the init_args the config sets (jsonargparse: "Option 'x' is not accepted");
  fine      otherwise, with notes on what it replaces (file value, whole object, another source of the same variable).

Defaults are not a problem when switching classes: jsonargparse only keeps the init_args that are explicitly given.
"""
import copy
from dataclasses import dataclass, field

import core
import indexer
import picker
import preview

LEVELS = ("ok", "info", "warn", "error")


@dataclass
class State:
    """The config as the program sees it for one selection, and the verdict on each variable."""
    cfg: dict
    idx: indexer.Index
    table: dict
    base: dict
    source: str
    ovs: list                        # every APP_* override (preview.overrides), in the order the program gets them
    effective: dict                  # base with the overrides that reach the config applied
    results: dict = field(default_factory=dict)       # name -> {"level", "notes": [{level, text}], "fix"?}
    missing: list = field(default_factory=list)       # [{"path", "cls", "args"}] required arguments with no value
    notes: list = field(default_factory=list)


def dotted(segs):
    return ".".join(s for s in segs if s != "init_args") or "(root)"


def _note(res, level, text):
    res["notes"].append({"level": level, "text": text})
    if LEVELS.index(level) > LEVELS.index(res["level"]):
        res["level"] = level


def _find(d, key):
    for k in d:
        if str(k).lower() == key:
            return k
    return None


def _accepts(idx, cls):
    """(argument names, complete) of a class: complete is False when part of it is not in src/."""
    if cls not in idx.classes:
        return set(), False
    params, missing = idx.params_of(cls)
    return {p.name.lower() for p in params}, not missing


@dataclass
class Walk:
    kind: str                         # arg | class_path | init_args | unknown | dropped
    level: picker.Level = None        # the Level that holds the last segment
    arg: dict = None
    reason: str = ""
    names: list = None                # argument names (picker path) of the object that holds the last segment


def walk(idx, tree, segs, base=None):
    """Follow the segments of a variable through `tree` the way the program does (top-level arguments: those of
    `base`, the config file)."""
    level = picker.root_level(idx, tree, base)
    names, i = [], 0
    while i < len(segs):
        seg, last = segs[i], i == len(segs) - 1
        if level.mode == "class":
            if seg == "class_path":
                return Walk("class_path", level, names=names) if last else Walk(
                    "dropped", level, reason=f"{dotted(segs[:i + 1])} is a class name, it has no fields")
            if seg != "init_args":
                return Walk("dropped", level, reason=f"{dotted(segs[:i])} is a class object: only CLASS_PATH and "
                                                     f"INIT_ARGS can follow, not {seg.upper()}")
            if last:
                return Walk("init_args", level, names=names)
            i += 1
            seg, last = segs[i], i == len(segs) - 1
        a = level.find(seg)
        if a is None:
            complete = level.mode == "root" or (level.cls in idx.classes and not level.missing)
            if not complete:
                return Walk("unchecked", level, reason=seg, names=names)
            if not last:
                owner = picker.short(level.cls) if level.cls else "the config root"
                return Walk("dropped", level, reason=f"{owner} has no argument {seg} (at {dotted(segs[:i + 1])})")
            return Walk("unknown", level, reason=seg, names=names)
        if last:
            return Walk("arg", level, arg=a, names=names)
        found, val = picker.lookup(level.container, seg)
        child = picker.describe(idx, a["desc"], val if found else None, None, level.child_path + [seg])
        here = dotted(segs[:i + 1])
        if child.mode == "class":
            if not (found and isinstance(val, (dict, str))):
                return Walk("dropped", level, reason=f"{here} is {'null' if found else 'not set'} in the config, and "
                                                     "a variable never creates an object: set it as a whole first")
        elif child.mode == "group":
            if (found and val is None) or (not found and a["default"][0] in ("", "None")):
                return Walk("dropped", level, reason=f"{here} is {'null' if found else 'not set'} in the config")
        else:
            return Walk("dropped", level, reason=f"{here} is a plain value ({a['type'] or 'no type'}): a variable "
                                                 "cannot set a key inside it, set the whole value instead")
        names, level = names + [seg], child
        i += 1
    return Walk("dropped", reason="empty name")


def _apply(tree, ovs):
    """Set each override in `tree`, creating the intermediate dicts the parsed config has (defaults)."""
    for o in ovs:
        node = tree
        for seg in o["path"][:-1]:
            if seg == "init_args" and preview.is_flat(node):
                continue
            key = _find(node, seg)
            if key is None:
                key, node[seg] = seg, {}
            elif isinstance(node[key], str):                # class shorthand: `adapter: pkg.Class`
                node[key] = {"class_path": node[key]}
            elif not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        key = _find(node, o["path"][-1])
        node[key if key is not None else o["path"][-1]] = copy.deepcopy(o["value"])
    return tree


def setters(cfg, sel):
    """name -> [(source, value)] of every place that sets a variable, in the order they apply (the last one wins)."""
    prefix = cfg["project"]["env_prefix"]
    out = {}
    for g in core.GROUPS:
        for v in cfg["variables"][g]:
            out.setdefault(v["name"], []).append(("variable default", v["value"]))
    for b in core.xp_of(cfg, sel.get("xp"))["blocks"]:
        row = core.pick(b["rows"], (sel.get("blocks") or {}).get(b["id"]), b["label"])
        for k, v in row["vars"].items():
            out.setdefault(core.full_name(b, k, prefix), []).append((f"{b['label']}: {row['name']}", v))
    for name, v in (sel.get("values") or {}).items():
        if name in out:
            out[name].append(("session edit", str(v)))
    return out


def _class_value(value):
    """(class_path, init_args dict or None) of a whole-object value, or (None, None)."""
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict) and isinstance(value.get("class_path"), str):
        ia = value.get("init_args")
        return value["class_path"], ia if isinstance(ia, dict) else None
    return None, None


def _check_class(idx, res, cls, init_args, where, cands, extra=None):
    """Notes for a class given to `where`: known, subclass of the declared type, init_args it accepts."""
    if "$" in cls:
        return
    if cls not in idx.classes:
        _note(res, "warn", f"{cls} is not in the source folder (library class or typo): not checked")
        return
    if cands and cls not in {c["fqn"] for c in cands}:
        _note(res, "warn", f"{picker.short(cls)} is not one of the classes the type of {where} allows")
    names, complete = _accepts(idx, cls)
    given = {str(k).lower(): k for k in (init_args or {})} | ({str(k).lower(): k for k in extra} if extra else {})
    bad = sorted(k for k in given if k not in names)
    if bad and complete:
        _note(res, "error", f"{picker.short(cls)} does not accept {', '.join(bad)}: the program stops "
                            f"(\"Option '{bad[0]}' is not accepted\")")
    elif bad:
        _note(res, "warn", f"cannot check {', '.join(bad)}: part of {picker.short(cls)} is not in the source folder")


def _required(idx, cls, init_args):
    """Required arguments (no default) of `cls` that `init_args` does not give."""
    if cls not in idx.classes:
        return []
    params, _ = idx.params_of(cls)
    have = {str(k).lower() for k in (init_args or {})}
    return [p.name for p in params if not p.has_default and p.name.lower() not in have]


def _value_text(v):
    text = preview.fmt(v)
    return text if len(text) <= 60 else text[:57] + "..."


def analyse(cfg, sel, focus=None, fresh=False):
    """State of a selection. `focus`: names of the variables to explain (default: every APP_* variable)."""
    table, _ = core.collect(cfg, sel)
    idx = indexer.get_index(picker.find_src(cfg), fresh)
    base, source, notes = preview.load_base(cfg, table)
    order = {name: i for i, name in enumerate(table)}
    ovs = sorted(preview.overrides(cfg, table), key=lambda o: order[o["name"]])
    st = State(cfg, idx, table, base, source, ovs, {}, notes=list(notes))
    who = setters(cfg, sel)
    prefix = cfg["project"]["env_prefix"]
    focus = set(focus) if focus is not None else {o["name"] for o in ovs}
    results = {n: {"level": "ok", "notes": []} for n in focus}

    # 1. structure: does the variable reach the config at all?
    reach = []
    for o in ovs:
        w = walk(idx, base, list(o["path"]))
        res = results.get(o["name"], {"level": "ok", "notes": []})
        if w.kind == "dropped":
            _note(res, "error", "ignored by the program: " + w.reason)
            continue
        outer = [p for p in ovs if p is not o and len(p["path"]) < len(o["path"]) and o["path"][:len(p["path"])] == p["path"]]
        if outer:
            p = max(outer, key=lambda x: len(x["path"]))
            _note(res, "error", f"ignored by the program: {p['name']} ({table[p['name']]['source']}) replaces "
                                f"{dotted(p['path'])} as a whole; put this value inside it instead")
            if o["name"] in results:
                try:
                    res["fix"] = {"name": p["name"], "value": _fold(st, p, o["path"], table[o["name"]]["value"]),
                                  "drop": [], "label": f"write it inside {dotted(p['path'])}"}
                except ValueError:
                    pass
            continue
        reach.append(o)
    st.effective = _apply(copy.deepcopy(base), reach)

    # 2. meaning: check each variable that reaches the config against the classes it ends up in
    links = {}
    cp = picker.class_path_of(picker.lookup(st.effective, "task")[1])
    if cp in idx.classes:
        links = idx.links(cp)
    created = []                                            # (names, class, init_args) of objects whose class is set here
    for o in reach:
        if o["name"] not in results:
            continue
        res, segs = results[o["name"]], list(o["path"])
        w = walk(idx, st.effective, segs, base)
        bw = walk(idx, base, segs)
        lvl = w.level
        owner = picker.short(lvl.cls) if lvl is not None and lvl.cls else "the root"
        if w.kind == "unknown":
            _note(res, "error", f"{owner} has no argument {w.reason}: the program stops (\"not accepted\")")
        elif w.kind == "unchecked":
            what = picker.short(lvl.cls) if lvl.cls else dotted(segs[:-1])
            _note(res, "warn", f"cannot check {w.reason}: {what} is not (fully) in the source folder")
        elif w.kind == "class_path":
            new, ia = o["value"], lvl.container
            if isinstance(new, str):
                old = bw.level.cls if bw.kind == "class_path" and bw.level is not None else None
                _check_class(idx, res, new, ia, dotted(segs[:-1]), lvl.candidates)
                if old and old != new:
                    _note(res, "info", f"switches {dotted(segs[:-1])} from {picker.short(old)} to {picker.short(new)}")
                if any(n["level"] == "error" and "does not accept" in n["text"] for n in res["notes"]):
                    names, _ = _accepts(idx, new)
                    kept = {k: v for k, v in (ia or {}).items() if str(k).lower() in names}
                    whole = {"class_path": new, **({"init_args": kept} if kept else {})}
                    res["fix"] = {"name": prefix + cfg["project"]["env_sep"].join(s.upper() for s in segs[:-1]),
                                  "value": preview.fmt(whole), "drop": [p["name"] for p in reach if p is not o and
                                                                        p["path"][:len(segs) - 1] == tuple(segs[:-1])],
                                  "label": f"replace {dotted(segs[:-1])} as a whole"}
                created.append((segs[:-1], new, ia))
            elif new is not None:
                _note(res, "error", "CLASS_PATH needs a class name")
        elif w.kind == "init_args":
            if isinstance(o["value"], dict):
                _check_class(idx, res, lvl.cls or "", o["value"], dotted(segs[:-1]), [])
            if bw.level is not None and isinstance(bw.level.container, dict) and bw.level.container:
                _note(res, "info", f"replaces all the init_args of {dotted(segs[:-1])} from the config "
                                   f"({', '.join(map(str, bw.level.container))})")
        else:                                               # an argument
            a, real = w.arg, segs
            prim, nullable, members = picker.primary(a["desc"])
            kind = picker.ui_kind(prim)
            found, old = picker.lookup(bw.level.container if bw.level else None, segs[-1])
            new = o["value"]
            link = links.get(".".join(real))
            if link:
                _note(res, "warn", f"{dotted(real)} is linked from {link}: the link sets it, this value is not used")
            if kind == "class" and new is not None:
                cls, ia = _class_value(new)
                if cls is None and isinstance(new, dict):     # {init_args: {...}} without class_path keeps the class
                    cls, ia = picker.class_path_of(old) if found else None, new.get("init_args")
                if cls:
                    _check_class(idx, res, cls, ia, dotted(real), picker.candidates_of(idx, members))
                    created.append((real, cls, ia))
                ocls, oia = _class_value(old) if found else (None, None)
                if ocls and ocls != cls:
                    _note(res, "info", f"replaces {picker.short(ocls)} from the config by {picker.short(cls or '?')}")
                if oia:
                    gone = [str(k) for k in oia if str(k).lower() not in {str(x).lower() for x in (ia or {})}]
                    if gone:
                        _note(res, "info", f"replaces the whole object from the config: {', '.join(gone)} "
                                           "set there are dropped (defaults apply)")
            elif found and old != new:
                _note(res, "info", f"changes the config value {_value_text(old)}")
            elif not found:
                d = a["default"][0]
                _note(res, "info", "not in the config file" + (f" (default {d})" if d else " (required, no default)"))
            choices = picker.choices_of(idx, prim, nullable)
            raw = o["raw"]
            if choices and "$" not in raw and kind != "flag" and preview.fmt(new) not in choices and str(new) not in choices:
                _note(res, "warn", f"{_value_text(new)} is not one of {', '.join(choices)}")
            inner = [p["name"] for p in ovs if p is not o and len(p["path"]) > len(segs) and p["path"][:len(segs)] == tuple(segs)]
            if inner:
                _note(res, "warn", f"replaces {dotted(segs)} as a whole, so {', '.join(inner)} set inside it "
                                   "are ignored: put those values in this one")

    # 3. sources: the same variable set in several places
    for name, res in results.items():
        src = who.get(name, [])
        if len(src) > 1:
            *before, last = src
            mine = table.get(name, {}).get("source")
            if mine and last[0] != mine and mine != "edited":
                _note(res, "warn", f"overridden by {last[0]} ({last[1]}): this value is not used")
            prev = [s for s in before if s[0] != mine]
            if prev:
                _note(res, "info", "overrides " + "; ".join(f"{s} ({_value_text(v) if v else 'empty'})" for s, v in prev[-2:]))
        if not name.startswith(prefix):
            res.setdefault("script", True)

    # 4. what the objects whose class is chosen here still need
    for real, cls, ia in created:
        eff = walk(idx, st.effective, list(real) + ["class_path"], base)
        eia = eff.level.container if eff.kind == "class_path" and eff.level is not None else ia
        here = ".".join(real) + ".init_args."
        miss = [m for m in _required(idx, cls, eia) if here + m not in links]
        if miss:
            st.missing.append({"path": dotted(real), "env": prefix + cfg["project"]["env_sep"].join(s.upper() for s in real),
                               "cls": picker.short(cls), "args": miss})
    for m in st.missing:
        for name, res in results.items():
            if name.upper().startswith(m["env"]):
                _note(res, "warn", f"{m['cls']} needs {', '.join(m['args'])} (no default) and nothing sets "
                                   f"{'it' if len(m['args']) == 1 else 'them'}")
                break
    st.results = results
    return st


def report(st):
    """JSON-able summary of a State."""
    return {"vars": st.results, "missing": st.missing, "notes": st.notes, "source": st.source}


def switch(st, names, cls):
    """How to give the class `cls` (or "__null__") to the node at `names` without breaking what is around it.

    Returns {set: [[name, value]], remove: [{name, source}], message}. A CLASS_PATH variable is enough when the node
    exists and the new class accepts every init_arg the config gives it; otherwise the node is written as a whole
    object that keeps the accepted init_args (values of variables set inside it are folded in, as written)."""
    cfg, idx = st.cfg, st.idx
    chain = picker.locate(idx, st.effective, st.base, names)
    level = chain[-1][1]
    if level.mode != "class":
        raise ValueError(f"{'.'.join(names) or 'the root'} does not take a class")
    real = tuple(s.lower() for s in level.child_path[:-1])
    node_env, class_env = picker.env_name(cfg, list(real)), picker.env_name(cfg, list(real) + ["class_path"])
    src = lambda name: st.table.get(name, {}).get("source", "")
    whole = next((o for o in st.ovs if o["path"] == real), None)
    inner = [o for o in st.ovs if len(o["path"]) > len(real) and o["path"][:len(real)] == real]
    where = ".".join(names)
    if cls == "__null__":
        return {"set": [[node_env, "NULL"]], "remove": [{"name": o["name"], "source": src(o["name"])} for o in inner],
                "message": f"{where} set to NULL" + (f"; {len(inner)} variable(s) inside it removed" if inner else "")}
    if not level.exists and whole is None:
        return {"set": [[node_env, f"{{class_path: {cls}}}"]], "remove": [],
                "message": f"{where} is not in the config: it is created as a whole object"}
    accepted, complete = _accepts(idx, cls)
    container = level.container if isinstance(level.container, dict) else {}
    keep, drop, skipped = [], [], []
    by_key = {o["path"][len(real) + 1]: o for o in inner
              if len(o["path"]) == len(real) + 2 and o["path"][len(real)] == "init_args"}
    for k, v in container.items():
        key = str(k).lower()
        if complete and key not in accepted:
            drop.append(str(k))
        elif key in by_key:                                  # set by a variable: keep its text ($VARS expand in the script)
            keep.append(f"{k}: {st.table[by_key[key]['name']]['value']}")
        elif isinstance(v, str) and "$" in v:                # ${settings.x} interpolation: the shell would expand it
            skipped.append(str(k))
        else:
            keep.append(f"{k}: {preview.fmt(v)}")
    if whole is None and not drop and not skipped:
        class_var = next((o for o in st.ovs if o["path"] == real + ("class_path",)), None)
        msg = f"{where}: CLASS_PATH set to {picker.short(cls)}"
        if not complete and cls in idx.classes:
            msg += " (part of the class is not in the source folder: its init_args could not be checked)"
        elif cls not in idx.classes:
            msg += f" ({cls} is not in the source folder: its init_args could not be checked)"
        return {"set": [[class_var["name"] if class_var else class_env, cls]], "remove": [], "message": msg}
    text = f"{{class_path: {cls}" + (f", init_args: {{{', '.join(keep)}}}" if keep else "") + "}"
    remove = [{"name": o["name"], "source": src(o["name"])} for o in inner]
    if whole is not None and whole["name"] != node_env:
        remove.append({"name": whole["name"], "source": src(whole["name"])})
    parts = [f"{where} is written as a whole object"]
    if drop:
        parts.append(f"{', '.join(drop)} {'is' if len(drop) == 1 else 'are'} not accepted by {picker.short(cls)} and "
                     "left out")
    if skipped:
        parts.append(f"{', '.join(skipped)} (${{...}} in the config) left to the default")
    if inner:
        parts.append(f"{len(inner)} variable(s) inside it folded in")
    return {"set": [[whole["name"] if whole else node_env, text]], "remove": remove, "message": "; ".join(parts)}


def _fold(st, o, segs, value):
    """The value of the variable `o` (which sets an object as a whole) with `segs` set to `value` inside it."""
    tree = preview.parse_value(st.table[o["name"]]["value"])
    if isinstance(tree, str):                               # class shorthand
        tree = {"class_path": tree}
    if not isinstance(tree, dict):
        raise ValueError(f"{o['name']} sets {dotted(o['path'])} to {_value_text(tree)}: set it to an object first")
    node = tree
    for seg in segs[len(o["path"]):-1]:
        key = _find(node, seg)
        if key is None:
            key, node[seg] = seg, {}
        elif isinstance(node[key], str) and seg != "init_args":
            node[key] = {"class_path": node[key]}
        elif not isinstance(node[key], dict):
            node[key] = {}
        node = node[key]
    key = _find(node, segs[-1])
    node[key if key is not None else segs[-1]] = preview.parse_value(value)
    return preview.fmt(tree)


def assign(st, name, value):
    """How to set the variable `name` to `value` so that the program uses it: {set, remove, message}.

    When a variable already replaces an object on the path as a whole, a separate variable would be ignored: the value
    is written inside that variable's value instead (its other content is kept, $VARS as written)."""
    prefix, sep = st.cfg["project"]["env_prefix"], st.cfg["project"]["env_sep"]
    if not name.startswith(prefix):
        return {"set": [[name, value]], "remove": [], "message": ""}
    segs = tuple(s.lower() for s in name[len(prefix):].split(sep) if s)
    outer = [o for o in st.ovs if len(o["path"]) < len(segs) and segs[:len(o["path"])] == o["path"]]
    if not outer:
        return {"set": [[name, value]], "remove": [], "message": ""}
    o = max(outer, key=lambda x: len(x["path"]))
    text = _fold(st, o, segs, value)
    source = st.table[o["name"]]["source"]
    return {"set": [[o["name"], text]], "remove": [],
            "message": f"written inside {o['name']} ({source}): it sets {dotted(o['path'])} as a whole, so a separate "
                       "variable would be ignored", "source": source}


def missing_all(st):
    """{path of an object in the effective config: (is a class, [required arguments without a value])}, for every
    object whose class is in the source folder (the arguments filled by the task's links() are not missing)."""
    idx, out = st.idx, {}
    links = {}
    cp = picker.class_path_of(picker.lookup(st.effective, "task")[1])
    if cp in idx.classes:
        links = idx.links(cp)

    def visit(level, obj_path, cont_path, depth=0, flat=False):
        if depth > 12:
            return
        complete = level.mode == "root" or (level.cls in idx.classes and not level.missing)
        for a in level.args:
            found, val = picker.lookup(level.container, a["name"])
            if ".".join(level.child_path + [a["name"]]) in links:
                continue
            if not found or val is None:
                if complete and a["required"] and not (found and picker.primary(a["desc"])[1]):    # null is a value of X | None
                    out.setdefault(obj_path, (level.mode == "class" and not flat, []))[1].append(a["name"])
                continue
            child = picker.describe(idx, a["desc"], val, None, level.child_path + [a["name"]])
            key = next((k for k in (level.container or {}) if str(k).lower() == a["name"].lower()), a["name"])
            if child.mode == "class" and child.cls in idx.classes:
                ia = next((k for k in val if str(k).lower() == "init_args"), "init_args") if isinstance(val, dict) else "init_args"
                sub_flat = preview.is_flat(val)                      # init args given without init_args
                visit(child, cont_path + (key,), cont_path + ((key,) if sub_flat else (key, ia)), depth + 1, sub_flat)
            elif child.mode == "group" and child.cls in idx.classes and isinstance(val, dict):
                visit(child, cont_path + (key,), cont_path + (key,), depth + 1)

    visit(picker.root_level(st.idx, st.effective, st.base), (), ())
    return out
