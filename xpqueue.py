"""Queue of experiments, launched one after the other, each when enough GPUs are free: on this machine, or on another
server (an item queued with "Run on" set to it: remote_launch.py). Each server has its own line: the first waiting
item of one server does not hold up the items of another. The GPUs of another server are read over ssh.

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
import remote
import remote_launch

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


def add(cfg, sel, mode="auto", count=None, manual=False, server=None):
    """Queue the selection: render its script now (the devices left open), for this machine or for `server` (its
    name). Returns the item."""
    sv = remote.server(cfg, server) if server else None
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
    script, _, _ = core.render(cfg, frozen, remote_launch.workdir(sv) if sv else None)
    item = {"id": uuid.uuid4().hex[:10], "xp": sel["xp"], "options": _summary(cfg, sel),
            "loop": sel.get("loop"), "devices_var": dvar, "mode": mode if dvar else "none",
            "count": n if mode == "auto" else len(fixed or []), "fixed": fixed if mode == "fixed" else None,
            "queued_value": value, "script": script, "status": "waiting", "manual": bool(manual),
            "server": server or None,                           # checked above (remote.server)
            "config": (table.get(cfg["project"]["config_var"]) or {}).get("value", "").strip(),
            "image": remote_launch.image_of(table),
            "created": datetime.now().isoformat(timespec="seconds")}
    with _LOCK:
        data = _load()
        data["items"].append(item)
        _save(data)
    _WAKE.set()
    return item


def _reserved(items, server):
    """GPU index -> XP given it less than RESERVE_S ago, on `server` (None: this machine)."""
    now, out = time.time(), {}
    for it in items:
        if (it.get("server") or None) == server and it.get("status") in ("starting", "started") \
                and now - it.get("started_at", 0) < RESERVE_S:
            for g in it.get("gpus") or []:
                out[g] = it["xp"]
    return out


_REMOTE_GPUS = {}                                # server -> (time, states, note): ssh is asked at most every CACHE_S
CACHE_S = 10


def server_gpu_states(cfg, items, server):
    """As gpu_states, for the GPUs of another server (over ssh)."""
    hit = _REMOTE_GPUS.get(server)
    if hit and time.time() - hit[0] < CACHE_S:
        smi, note = hit[1], hit[2]
    else:
        try:
            smi, note = gpus.state(core.active_servers(cfg), server)["gpus"], ""
        except Exception as err:  # pylint: disable=broad-except  # unreachable, no nvidia-smi
            smi, note = None, f"GPUs of {server} unknown: {err}"
        _REMOTE_GPUS[server] = (time.time(), smi, note)
    if smi is None:
        return None, note
    reserved = _reserved(items, server)
    out = []
    for g in smi:
        i, used = int(g["index"]), g.get("mem_used") or 0
        why = ("a process runs on it" if g.get("processes") else f"{int(used)} MiB used" if used >= FREE_MIB
               else f"just given to {reserved[i]}" if i in reserved else "")
        out.append({"index": i, "name": g["name"], "free": not why, "why": why, "mem_used": used,
                    "mem_total": g.get("mem_total")})
    return out, ""


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
    reserved = _reserved(items, None)
    out = []
    for g in smi:
        i, used = int(g["index"]), gpus._num(g["memory.used"]) or 0   # pylint: disable=protected-access
        why = ("a process runs on it" if g["gpu_uuid"] in with_proc else f"{int(used)} MiB used" if used >= FREE_MIB
               else f"just given to {reserved[i]}" if i in reserved else "")
        out.append({"index": i, "name": g["name"], "free": not why, "why": why, "mem_used": used,
                    "mem_total": gpus._num(g["memory.total"])})       # pylint: disable=protected-access
    return out, ""


def _free_gpus(cfg, items, server=None):
    """(free GPU indices or None, note), on this machine or on `server`."""
    states, note = server_gpu_states(cfg, items, server) if server else gpu_states(items)
    return (None, note) if states is None else ([g["index"] for g in states if g["free"]], "")


def _record(cfg, item, gpu_list):
    """The item's script with its devices, written into <scripts_dir>/<XP>/ (as the Launch button does); the item
    marked as starting. Returns (script, path)."""
    value = "[" + ", ".join(str(g) for g in gpu_list) + "]" if gpu_list is not None else item.get("queued_value", "")
    script = item["script"].replace(TOKEN, value)
    root = core.project_root(cfg)
    folder = root / core.xp_dir(cfg, item["xp"])
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{core.slug(item['server'])}" if item.get("server") else "")
    path, n = folder / f"{stamp}_queue.sh", 1
    while path.exists():
        n += 1
        path = folder / f"{stamp}_queue_{n}.sh"
    path.write_text(script)
    path.chmod(0o755)
    item.update(status="starting", devices=value, gpus=gpu_list or [], path=str(path.relative_to(root)),
                started=datetime.now().isoformat(timespec="seconds"), started_at=time.time())
    return script, path


def _launch(cfg, item, gpu_list):
    """Write the item's script with its devices and run it here (as the Launch button does)."""
    _, path = _record(cfg, item, gpu_list)
    proc = subprocess.Popen(["bash", str(path)], cwd=core.project_root(cfg), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=LAUNCH_WAIT_S)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.communicate, daemon=True).start()
        item.update(status="started", output="(still running after the launch wait)")
        return
    item.update(status="started" if proc.returncode == 0 else "failed", returncode=proc.returncode, output=out[-4000:])


def _launch_remote(cfg, item, gpu_list):
    """Write the item's script and launch it on its server, in a thread (copying the project there can take a
    while): the item stays "starting" until the launch is done."""
    script, _ = _record(cfg, item, gpu_list)
    sv = remote.server(cfg, item["server"])

    def run():
        try:
            res, folder = remote_launch.launch(cfg, sv, script, item["xp"], item.get("config", ""), item.get("image", ""))
            upd = {"status": "started" if res["ok"] else "failed", "output": remote_launch.output_of(res)[-4000:],
                   "action": res["id"], "folder": folder}
        except Exception as err:  # pylint: disable=broad-except
            upd = {"status": "failed", "output": f"{type(err).__name__}: {err}"}
        with _LOCK:
            data = _load()
            for it in data["items"]:
                if it["id"] == item["id"]:
                    it.update(upd)
            _save(data)
    threading.Thread(target=run, daemon=True, name=f"queue-{sv['name']}").start()


def _pick(item, free):
    """GPUs to give to the item from `free`, or None when it has to wait."""
    if item["mode"] == "fixed":
        return item["fixed"] if set(item["fixed"]) <= set(free) else None
    return free[:item["count"]] if len(free) >= item["count"] else None


WAIT = object()


def _devices_for(cfg, items, target, forced, devices):
    """(GPUs to start `target` on, or WAIT; note)."""
    where = f"{target['server']}: " if target.get("server") else ""
    if devices is not None and target["mode"] != "none":
        idx, _ = parse_devices(devices)
        if idx is None:
            raise ValueError(f"devices: give GPU indices, e.g. [0, 1] (got '{devices}')")
        return idx, ""
    if target["mode"] == "none":
        return None, ""
    free, note = _free_gpus(cfg, items, target.get("server"))
    if free is None:
        if not forced:
            return WAIT, note
        return (target["fixed"] if target["mode"] == "fixed" else None), note
    gpu_list = _pick(target, free)
    if gpu_list is None and not forced:
        need = target["fixed"] if target["mode"] == "fixed" else f"{target['count']} free GPU(s)"
        return WAIT, f"{where}waiting for {need}; free now: {free or 'none'}"
    if gpu_list is None:                                     # started by hand: the free ones, else what was queued
        gpu_list = free[:target["count"]] if target["mode"] == "auto" and free else (target["fixed"] or None)
    return gpu_list, ""


def step(force_id=None, devices=None):
    """Start the first waiting (not manual) item of each server if its GPUs are free; or the item `force_id`, now,
    with `devices` ("[0, 1]") when given. Returns the queue."""
    cfg = core.load()
    with _LOCK:
        data = _load()
        waiting = [it for it in data["items"] if it["status"] == "waiting" and (force_id or not it.get("manual"))]
        if force_id:
            targets = [it for it in waiting if it["id"] == force_id][:1]
        elif data["paused"]:
            targets = []
        else:
            seen, targets = set(), []
            for it in waiting:                               # the first of each server's line
                if (it.get("server") or "") not in seen:
                    seen.add(it.get("server") or "")
                    targets.append(it)
        if not targets:
            data["note"] = "paused" if data["paused"] and waiting else ""
            _save(data)
            return data
        notes = []
        for target in targets:
            gpu_list, note = _devices_for(cfg, data["items"], target, bool(force_id), devices)
            if note:
                notes.append(note)
            if gpu_list is WAIT:
                continue
            try:
                if target.get("server"):
                    _launch_remote(cfg, target, gpu_list)
                else:
                    _launch(cfg, target, gpu_list)
            except Exception as err:  # pylint: disable=broad-except
                target.update(status="failed", output=f"{type(err).__name__}: {err}")
        data["note"] = " · ".join(notes)
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
        cfg = core.load()
        out["server_gpus"] = {}
        for name in dict.fromkeys(it["server"] for it in data["items"] if it.get("server") and it["status"] == "waiting"):
            states, note = server_gpu_states(cfg, data["items"], name)
            out["server_gpus"][name] = {"gpus": states, "note": note}
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
