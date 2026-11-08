"""Queue of experiments, launched one after the other on this machine, each when enough GPUs are free.

An item is frozen when it is queued: its script is rendered then, from the selection of that moment (options, command
loop, edited values), so later edits of the XP do not change it. Only the devices variable (the one ending in DEVICES,
e.g. APP_SETTINGS__DEVICES) is left open and set when the item starts:
    auto    any N free GPUs (N: the number of GPUs the value asked for when queued: [1, 3] -> 2, 2 -> 2)
    fixed   exactly the GPUs of the value, when they are all free
A GPU is free when nvidia-smi shows no process on it and less than FREE_MIB used. A GPU just given to an item stays
taken for RESERVE_S seconds (the container needs some time before its process appears).

The queue is kept next to the settings (.queue.json) and checked every POLL_S seconds by a thread of the app, and at
once when it changes. Items run in order: the first waiting item blocks the ones after it. A manual item is never
started by the queue (and blocks nothing): it waits for "Start now", which can also be given the devices to use.
"""
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime

import core
import gpus

POLL_S = 20
RESERVE_S = 300
FREE_MIB = 512
TOKEN = "XPQUEUEDEVICES0TOKEN"                               # stands for the devices value in the frozen script
LAUNCH_WAIT_S = 30

_LOCK = threading.RLock()
_WAKE = threading.Event()
_STARTED = False


def _path():
    return core.state_path(".queue.json")


def _load():
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        data = {}
    return {"paused": bool(data.get("paused")), "items": list(data.get("items") or []), "note": data.get("note", "")}


def _save(data):
    tmp = _path().with_name(_path().name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, _path())


def devices_var(cfg, table):
    """Name of the devices variable of a selection (env_prefix ... DEVICES), or None."""
    prefix = cfg["project"]["env_prefix"]
    return next((n for n in table if n.startswith(prefix) and n.upper().endswith("DEVICES")), None)


def parse_devices(value):
    """(GPU indices or None, how many GPUs): "[1, 3]" -> ([1, 3], 2); "2" -> (None, 2); "auto" -> (None, 1)."""
    text = str(value or "").strip()
    if text.startswith("["):
        idx = [int(x) for x in re.findall(r"\d+", text)]
        return idx, max(1, len(idx))
    if re.fullmatch(r"\d+", text):
        return None, max(1, int(text))
    return None, 1


def _summary(cfg, sel):
    xp = core.xp_of(cfg, sel["xp"])
    def shown(c, b):
        if isinstance(c, list):
            return c[0] if len(c) == 1 else f"{len(c)} values: " + ", ".join(c)
        return c or (b["rows"][0]["name"] if b["rows"] else "")
    return [{"block": b["label"], "option": shown(sel.get("blocks", {}).get(b["id"]), b)} for b in xp["blocks"] if len(b["rows"]) > 1]


def add(cfg, sel, mode="auto", count=None, manual=False):
    """Queue the selection: render its script now (the devices left open). Returns the item."""
    table, _ = core.collect(cfg, sel)
    if (table.get("DEBUG") or {}).get("value", "0").strip() not in ("", "0"):
        raise ValueError("DEBUG=1 attaches the container to a terminal: queue only runs with DEBUG=0")
    dvar = devices_var(cfg, table)
    value = (table.get(dvar) or {}).get("value", "") if dvar else ""
    fixed, n = parse_devices(value)
    if mode == "fixed" and fixed is None:
        raise ValueError(f"{dvar or 'the devices variable'} is '{value}': give GPU indices ([0, 1]) to keep them fixed")
    if count is not None:
        n = max(1, int(count))
    frozen = dict(sel, values={**(sel.get("values") or {}), **({dvar: TOKEN} if dvar else {})})
    script, _, _ = core.render(cfg, frozen)
    item = {"id": uuid.uuid4().hex[:10], "xp": sel["xp"], "options": _summary(cfg, sel),
            "loop": sel.get("loop"), "devices_var": dvar, "mode": mode if dvar else "none",
            "count": n if mode == "auto" else len(fixed or []), "fixed": fixed if mode == "fixed" else None,
            "queued_value": value, "script": script, "status": "waiting", "manual": bool(manual),
            "created": datetime.now().isoformat(timespec="seconds")}
    with _LOCK:
        data = _load()
        data["items"].append(item)
        _save(data)
    _WAKE.set()
    return item


def gpu_states(items):
    """([{index, name, free, why, mem_used, mem_total}], note) of this machine's GPUs: nvidia-smi only (fast enough to
    be asked every few seconds). why: a process, memory used, or given to a queued item less than RESERVE_S ago."""
    try:
        smi, apps = gpus._local_gpus(), gpus._local_apps()   # pylint: disable=protected-access
    except Exception as err:  # pylint: disable=broad-except  # no nvidia-smi
        return None, f"GPUs of this machine unknown: {err}"
    if not smi:
        return None, "GPUs of this machine unknown: nvidia-smi is not available"
    with_proc = {a["gpu_uuid"] for a in apps}
    now, reserved = time.time(), {}
    for it in items:
        if it.get("status") == "started" and now - it.get("started_at", 0) < RESERVE_S:
            for g in it.get("gpus") or []:
                reserved[g] = it["xp"]
    out = []
    for g in smi:
        i, used = int(g["index"]), gpus._num(g["memory.used"]) or 0   # pylint: disable=protected-access
        why = ("a process runs on it" if g["gpu_uuid"] in with_proc else f"{int(used)} MiB used" if used >= FREE_MIB
               else f"just given to {reserved[i]}" if i in reserved else "")
        out.append({"index": i, "name": g["name"], "free": not why, "why": why, "mem_used": used,
                    "mem_total": gpus._num(g["memory.total"])})       # pylint: disable=protected-access
    return out, ""


def _free_gpus(cfg, items):  # pylint: disable=unused-argument
    """(free GPU indices or None, note)."""
    states, note = gpu_states(items)
    return (None, note) if states is None else ([g["index"] for g in states if g["free"]], "")


def _launch(cfg, item, gpu_list):
    """Write the item's script with its devices and run it (as the Launch button does)."""
    value = "[" + ", ".join(str(g) for g in gpu_list) + "]" if gpu_list is not None else item.get("queued_value", "")
    script = item["script"].replace(TOKEN, value)
    root = core.project_root(cfg)
    folder = root / core.xp_dir(cfg, item["xp"])
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path, n = folder / f"{stamp}_queue.sh", 1
    while path.exists():
        n += 1
        path = folder / f"{stamp}_queue_{n}.sh"
    path.write_text(script)
    path.chmod(0o755)
    item.update(status="starting", devices=value, gpus=gpu_list or [], path=str(path.relative_to(root)),
                started=datetime.now().isoformat(timespec="seconds"), started_at=time.time())
    proc = subprocess.Popen(["bash", str(path)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=LAUNCH_WAIT_S)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.communicate, daemon=True).start()
        item.update(status="started", output="(still running after the launch wait)")
        return
    item.update(status="started" if proc.returncode == 0 else "failed", returncode=proc.returncode, output=out[-4000:])


def _pick(item, free):
    """GPUs to give to the item from `free`, or None when it has to wait."""
    if item["mode"] == "fixed":
        return item["fixed"] if set(item["fixed"]) <= set(free) else None
    return free[:item["count"]] if len(free) >= item["count"] else None


def step(force_id=None, devices=None):
    """Start the first waiting (not manual) item if its GPUs are free; or the item `force_id`, now, with `devices`
    ("[0, 1]") when given. Returns the queue."""
    cfg = core.load()
    with _LOCK:
        data = _load()
        waiting = [it for it in data["items"] if it["status"] == "waiting" and (force_id or not it.get("manual"))]
        target = next((it for it in waiting if it["id"] == force_id), None) if force_id else (None if data["paused"] else (waiting[0] if waiting else None))
        if target is None:
            data["note"] = "paused" if data["paused"] and waiting else ""
            _save(data)
            return data
        if devices is not None and target["mode"] != "none":
            idx, _ = parse_devices(devices)
            if idx is None:
                raise ValueError(f"devices: give GPU indices, e.g. [0, 1] (got '{devices}')")
            gpu_list = idx
        elif target["mode"] == "none":
            gpu_list = None
        else:
            free, note = _free_gpus(cfg, data["items"])
            if free is None:
                data["note"] = note
                if not force_id:
                    _save(data)
                    return data
                gpu_list = target["fixed"] if target["mode"] == "fixed" else None
            else:
                gpu_list = _pick(target, free)
                if gpu_list is None and not force_id:
                    need = target["fixed"] if target["mode"] == "fixed" else f"{target['count']} free GPU(s)"
                    data["note"] = f"waiting for {need}; free now: {free or 'none'}"
                    _save(data)
                    return data
                if gpu_list is None:                         # started by hand: the free ones, else what was queued
                    gpu_list = free[:target["count"]] if target["mode"] == "auto" and free else (target["fixed"] or None)
                data["note"] = ""
        try:
            _launch(cfg, target, gpu_list)
        except Exception as err:  # pylint: disable=broad-except
            target.update(status="failed", output=f"{type(err).__name__}: {err}")
        _save(data)
        return data


def act(action, item_id=None, devices=None):
    """remove | up | down | retry | start (now, `devices` optional) | manual | auto for an item; pause | resume |
    clear (finished items) for the queue."""
    with _LOCK:
        data = _load()
        items = data["items"]
        i = next((k for k, it in enumerate(items) if it["id"] == item_id), None)
        if action in ("pause", "resume"):
            data["paused"] = action == "pause"
        elif action == "clear":
            data["items"] = [it for it in items if it["status"] in ("waiting", "starting")]
        elif i is None:
            raise ValueError("no such item in the queue")
        elif action == "remove":
            if items[i]["status"] == "starting":
                raise ValueError("this item is starting: wait a few seconds")
            items.pop(i)
        elif action in ("up", "down"):                   # past the started items, to the next waiting one
            order = [k for k, it in enumerate(items) if it["status"] == "waiting"]
            if i in order:
                p = order.index(i) + (-1 if action == "up" else 1)
                if 0 <= p < len(order):
                    j = order[p]
                    items[i], items[j] = items[j], items[i]
        elif action in ("manual", "auto"):
            items[i]["manual"] = action == "manual"
        elif action == "retry":
            items[i].update(status="waiting", output="", devices=None, gpus=[], path=None)
        elif action != "start":
            raise ValueError(f"unknown action '{action}'")
        _save(data)
    if action == "start":
        return step(force_id=item_id, devices=devices)
    _WAKE.set()
    return _load()


def view(with_gpus=False):
    """The queue for the page (scripts left out), with the GPUs of this machine when asked."""
    data = _load()
    out = {"paused": data["paused"], "note": data["note"], "poll_s": POLL_S, "reserve_s": RESERVE_S,
           "items": [{k: v for k, v in it.items() if k != "script"} for it in data["items"]]}
    if with_gpus:
        out["gpus"], out["gpus_note"] = gpu_states(data["items"])
    return out


def start_thread():
    """Check the queue every POLL_S seconds (and when it changes), for as long as the app runs."""
    global _STARTED  # pylint: disable=global-statement
    if _STARTED:
        return
    _STARTED = True

    def loop():
        while True:
            _WAKE.wait(POLL_S)
            _WAKE.clear()
            try:
                step()
            except Exception:  # pylint: disable=broad-except  # e.g. settings being edited: try again later
                pass
    threading.Thread(target=loop, daemon=True, name="xp-queue").start()
