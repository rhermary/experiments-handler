"""Difference between two selections of an XP (e.g. two options of a block): the variables whose effective value
differs, and a line diff of the config the program gets (base config + variables, as in the Config preview)."""
import difflib

import checker
import core
import preview

CONTEXT = 3


def _config_lines(cfg, sel):
    """(lines of the effective config as text, source, error) for a selection."""
    table, _ = core.collect(cfg, sel)
    try:
        try:
            out = preview.build(cfg, table, state=checker.analyse(cfg, sel))
        except ValueError:
            raise
        except Exception:  # pylint: disable=broad-except  # no source folder: overrides as written
            out = preview.build(cfg, table)
    except (ValueError, OSError) as err:
        return [], "", str(err)
    return [ln["t"] for ln in out["lines"]], out.get("source", ""), ""


def _parents(lines, i):
    """Keys enclosing line i (by indentation), e.g. ['task:', 'init_args:', 'model:']."""
    def indent(s):
        return len(s) - len(s.lstrip(" -"))
    out, level = [], indent(lines[i]) if i < len(lines) else 0
    for j in range(min(i, len(lines)) - 1, -1, -1):
        s = lines[j]
        if s.strip() and indent(s) < level:
            out.append(s.strip().split(":")[0].lstrip("- ").strip())
            level = indent(s)
            if level == 0:
                break
    return list(reversed(out))


def config_diff(a, b, context=CONTEXT):
    """Hunks of a line diff: [{where: enclosing keys, lines: [{t, s: same|del|add}]}]."""
    hunks = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        lines = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [{"t": t, "s": "same"} for t in a[i1:i2]]
            if tag in ("replace", "delete"):
                lines += [{"t": t, "s": "del"} for t in a[i1:i2]]
            if tag in ("replace", "insert"):
                lines += [{"t": t, "s": "add"} for t in b[j1:j2]]
        first = group[0][1]
        hunks.append({"where": ".".join(_parents(a, first)) if a else "", "line": first + 1, "lines": lines})
    return hunks


def diff(cfg, sel_a, sel_b):
    """What changes from selection A to selection B."""
    if sel_a.get("xp") != sel_b.get("xp"):
        raise ValueError("the two selections must be of the same XP")
    ta, _ = core.collect(cfg, sel_a)
    tb, _ = core.collect(cfg, sel_b)
    variables = []
    for name in list(dict.fromkeys([*ta, *tb])):
        ea, eb = ta.get(name), tb.get(name)
        va, vb = (ea or {}).get("value"), (eb or {}).get("value")
        if va != vb:
            variables.append({"name": name, "a": va, "b": vb, "a_by": (ea or {}).get("source", ""),
                              "b_by": (eb or {}).get("source", ""), "scope": (ea or eb or {}).get("scope", "")})
    la, src_a, err_a = _config_lines(cfg, sel_a)
    lb, src_b, err_b = _config_lines(cfg, sel_b)
    return {"variables": variables, "config": config_diff(la, lb), "source": {"a": src_a, "b": src_b},
            "errors": {k: e for k, e in (("a", err_a), ("b", err_b)) if e},
            "blocks": [b["id"] for b in core.xp_of(cfg, sel_a["xp"])["blocks"]
                       if sel_a.get("blocks", {}).get(b["id"]) != sel_b.get("blocks", {}).get(b["id"])]}
