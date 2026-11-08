"""Actions on the servers. An action is a list of commands: they can be shown without running anything (dry run), and
when they run, each command, its output and its exit code are written to <settings folder>/actions/<id>/commands.log,
with one line per action in actions.log. Nothing runs that is not in that list.

A step is a command (argv) or, for work done here in Python (copying database rows), a function with the text that
says what it does; both are logged the same way.
"""
import json
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime

import core

_LOCK = threading.Lock()


def step(label, argv=None, fn=None, display=None, timeout=120, allow_fail=False, stdin=None, always=False):
    """A step of an action: `argv` (a command) or `fn` (Python, returns its output text); `display` is what is shown.
    always: runs even after an earlier step failed (cleanup: closing a tunnel)."""
    return {"label": label, "argv": argv, "fn": fn, "display": display or (shlex.join(argv) if argv else label),
            "timeout": timeout, "allow_fail": allow_fail or always, "stdin": stdin, "always": always}


def _dir():
    return core.state_path("actions")


def _log():
    return core.state_path("actions.log")


def plan(steps):
    """What would run: [{label, display}] (a dry run: nothing is executed)."""
    return [{"label": s["label"], "display": s["display"]} for s in steps]


def run(kind, server, title, steps, meta=None):
    """Run the steps in order (stop at the first failure unless the step allows it), logging everything.
    Returns {id, kind, server, title, ok, steps: [{label, display, rc, out, err, secs}]}."""
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H-%M-%S")                # folder names: no ':'
    when = now.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        base = f"{stamp}_{kind}_{core.slug(server or 'all')}"
        folder, n = _dir() / base, 2
        while folder.exists():
            folder, n = _dir() / f"{base}_{n}", n + 1
        folder.mkdir(parents=True)
    log = folder / "commands.log"
    out = {"id": folder.name, "kind": kind, "server": server, "title": title, "when": when,
           "ok": True, "steps": [], **({"meta": meta} if meta else {})}
    with open(log, "w", encoding="utf8") as f:
        f.write(f"# {title}\n# {when}  server: {server or '-'}\n\n")
    for s in steps:
        if not out["ok"] and not s.get("always"):
            continue
        t0, rc, so, se = time.time(), 0, "", ""
        try:
            if s["argv"]:
                r = subprocess.run(s["argv"], capture_output=True, text=True, timeout=s["timeout"], check=False,
                                   input=s["stdin"], stdin=None if s["stdin"] is not None else subprocess.DEVNULL)
                rc, so, se = r.returncode, r.stdout, r.stderr
            else:
                so = s["fn"]() or ""
        except subprocess.TimeoutExpired:
            rc, se = 124, f"no answer after {s['timeout']} s"
        except Exception as err:  # pylint: disable=broad-except  # logged as the step's failure
            rc, se = 1, f"{type(err).__name__}: {err}"
        res = {"label": s["label"], "display": s["display"], "rc": rc, "out": so, "err": se, "secs": round(time.time() - t0, 2)}
        out["steps"].append(res)
        with open(log, "a", encoding="utf8") as f:
            f.write(f"## {s['label']}\n$ {s['display']}\n")
            if so:
                f.write(so if so.endswith("\n") else so + "\n")
            if se:
                f.write("[stderr]\n" + (se if se.endswith("\n") else se + "\n"))
            f.write(f"[exit {rc}, {res['secs']} s]\n\n")
        if rc != 0 and not s["allow_fail"]:
            out["ok"] = False
    (folder / "action.json").write_text(json.dumps({k: v for k, v in out.items()}, indent=1, default=str))
    with _LOCK, open(_log(), "a", encoding="utf8") as f:
        f.write(json.dumps({"id": out["id"], "when": out["when"], "kind": kind, "server": server, "title": title,
                            "ok": out["ok"], "steps": len(out["steps"])}) + "\n")
    return out


def _when(text):
    """2026-11-08 15:17:47 (actions logged before used 15-17-47)."""
    return re.sub(r"^(\d{4}-\d\d-\d\d)[ _](\d\d)-(\d\d)-(\d\d)$", r"\1 \2:\3:\4", str(text or ""))


def recent(limit=100, server=None, kind=None):
    """The last actions, newest first."""
    try:
        lines = _log().read_text(encoding="utf8").splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            a = json.loads(ln)
        except ValueError:
            continue
        if (server and a.get("server") != server) or (kind and a.get("kind") != kind):
            continue
        a["when"] = _when(a.get("when"))
        out.append(a)
        if len(out) >= limit:
            break
    return out


def read(action_id):
    """An action: its record and its commands.log."""
    folder = _dir() / action_id
    if "/" in action_id or ".." in action_id or not folder.is_dir():
        raise ValueError(f"no action '{action_id}'")
    rec = json.loads((folder / "action.json").read_text()) if (folder / "action.json").exists() else {}
    return {**rec, "when": _when(rec.get("when")), "log": (folder / "commands.log").read_text(encoding="utf8"), "folder": str(folder)}
