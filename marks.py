"""Marks on the cells of the Results table: "a result is coming".

- Launches: every launch made here (Launch, or an item of the queue when it starts; on this machine or a server) is
  recorded with its XP and the option of each of its blocks (.launches.json next to the settings). The dimensions of
  the Results come from the blocks (a dimension's values are the options of its block), so a launch marks the cells
  of the options it ran (each value of a list block looped over).
- Queued: the items of the queue not started yet (waiting, starting), read from the queue itself (.queue.json):
  once started, an item is recorded as a launch.
- By hand: a cell marked (or a launch mark cleared) in the page, per XP and selection (.cell-marks.json).

The page shows a mark on a cell that has no run yet: when the runs are in the database read, the mark is no longer
shown (it is not deleted: the same cell of another database may still miss them).
"""
import itertools
import json
import os
import threading
from datetime import datetime

import core

KEEP = 3000                                     # launches kept (the oldest are dropped)
_LOCK = threading.Lock()


def _path(name):
    return core.state_path(name)


def _read(name, default):
    try:
        return json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return default


def _write(name, data):
    tmp = _path(name).with_name(_path(name).name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, _path(name))


def options_of(cfg, sel):
    """{block id: [option names it runs]}: the option chosen, the values selected in a list block, or all of them
    when its "all" option (a loop over its values) is chosen."""
    xp = core.xp_of(cfg, sel["xp"])
    chosen = sel.get("blocks") or {}
    out = {}
    for b in xp["blocks"]:
        if b.get("kind") == "fixed":
            continue
        values = [r["name"] for r in b["rows"] if not r.get("loop_all")]
        c = chosen.get(b["id"])
        if isinstance(c, list):
            out[b["id"]] = [v for v in c if v in values]
        elif any(r.get("loop_all") and r["name"] == c for r in b["rows"]):
            out[b["id"]] = values                                   # all (N): a loop over every value
        else:
            out[b["id"]] = [c if any(r["name"] == c for r in b["rows"]) else (b["rows"][0]["name"] if b["rows"] else "")]
    return out


def record_launch(cfg, sel, server=None, where=""):
    """Remember a launch: what it will put in the Results (its XP and options)."""
    try:
        rec = {"xp": sel["xp"], "options": options_of(cfg, sel), "server": server or "",
               "when": datetime.now().strftime("%Y-%m-%d %H:%M"), "where": where}
    except (ValueError, KeyError):
        return
    with _LOCK:
        data = _read(".launches.json", [])
        data.append(rec)
        _write(".launches.json", data[-KEEP:])


def _key(xp, selection):
    return f"{xp}\u0000{selection or ''}"


def _compatible(a, b):
    """Two cells that can be the same: equal on the dimensions they both have (one may leave some out)."""
    return all(a[k] == b[k] for k in a.keys() & b.keys())


def for_results(cfg, sel, conf):
    """{marks: [{cell: {dimension: value}, kind: launch | queued | manual, server, when, where}], cleared: [cells]} of the
    selection. A launch's cell has every dimension that comes from a block, arranged in the table or not (the page
    compares the dimensions both cells have: a dimension left out of the arrangement matches any value); a dimension
    that does not come from a block is left out (any value). A mark on a value left out of the table (Values Kept) is
    dropped. A cleared cell hides the launch marks it covers."""
    hide = {k: set(v) for k, v in (conf["layout"].get("hide") or {}).items()}
    by_block = [(d["name"], d["block"]) for d in conf["dimensions"] if d.get("block")]
    manual = _read(".cell-marks.json", {}).get(_key(sel["xp"], conf.get("selection")), {})
    shown = lambda cell: not any(v in hide.get(k, ()) for k, v in cell.items())   # noqa: E731

    def cells(opts):
        if not by_block:
            return []
        combos = itertools.product(*[[v for v in opts.get(bid) or [] if v not in hide.get(n, ())] for n, bid in by_block])
        return [dict(zip([n for n, _ in by_block], c)) for c in combos]

    out = []
    for rec in _read(".launches.json", []):
        if rec.get("xp") == sel["xp"]:
            out += [{"cell": c, "kind": "launch", "server": rec.get("server") or "this machine",
                     "when": rec.get("when", ""), "where": rec.get("where", "")} for c in cells(rec.get("options") or {})]
    try:
        queue = json.loads(core.state_path(".queue.json").read_text()).get("items") or []
    except (OSError, ValueError, AttributeError):
        queue = []
    for it in queue:
        if it.get("xp") != sel["xp"] or it.get("status") not in ("waiting", "starting"):
            continue
        try:
            opts = options_of(cfg, {"xp": it["xp"], "blocks": it.get("blocks") or {}})
        except (ValueError, KeyError):
            continue
        out += [{"cell": c, "kind": "queued", "server": it.get("server") or "this machine",
                 "when": (it.get("created") or "")[:16].replace("T", " "), "where": it.get("status")} for c in cells(opts)]
    out += [{"cell": c["cell"], "kind": "manual", "when": c.get("when", "")}
            for c in manual.get("marked", []) if shown(c["cell"])]
    return {"marks": out, "cleared": manual.get("cleared", [])}


def set_mark(sel, selection, cell, on):
    """Mark a cell by hand (on), or clear it (off: its hand marks, and the launch marks it covers). The marks and
    clears of the cell, also made with other dimensions arranged, are replaced."""
    if not isinstance(cell, dict) or not cell:
        raise ValueError("cell: {dimension: value}")
    cell = {str(k): str(v) for k, v in cell.items()}
    key = _key(sel["xp"], selection)
    same = lambda c: _compatible(c, cell)                     # noqa: E731  (a mark made with other dimensions arranged)
    with _LOCK:
        data = _read(".cell-marks.json", {})
        m = data.setdefault(key, {"marked": [], "cleared": []})
        m["marked"] = [c for c in m["marked"] if not same(c["cell"])]
        m["cleared"] = [c for c in m["cleared"] if not same(c)]
        if on:
            m["marked"].append({"cell": cell, "when": datetime.now().strftime("%Y-%m-%d %H:%M")})
        else:
            m["cleared"].append(cell)
        _write(".cell-marks.json", data)
