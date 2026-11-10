"""Jobs: your containers running on this machine and on the connected servers (Servers > Connect), as launched by the
scripts (docker create --user $(id -u) ... in the template): the running containers whose user is you there, except
the launcher's own ones (launcher-db, launcher-synchronised-db, launcher-app-*, launcher-mlflow-ui-*).

Reading them is read-only (docker ps / docker inspect, and a grep of the launch folders to find which launch started a
container): not logged as an action. Stopping one is an action (shown, logged): docker stop.
"""
import json
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import actions
import core
import remote
import remote_launch

OWN_PREFIX = "launcher-"                         # the launcher's own containers: not jobs
LOGS_TAIL = 200


def _script(launches=""):
    """Shell: @@UID <uid>, then @@C <docker inspect JSON> per running container, @@L <id> <launch output.log>."""
    q = shlex.quote
    lines = ["echo \"@@UID $(id -u)\"",
             "ids=$(docker ps -q --no-trunc 2>/dev/null)",
             "[ -n \"$ids\" ] && docker inspect --format '@@C {{json .}}' $ids"]
    if launches:
        lines.append(f"for id in $ids; do f=$(grep -l \"${{id:0:12}}\" {q(launches)}/*/output.log 2>/dev/null | head -n 1); "
                     "[ -n \"$f\" ] && echo \"@@L $id $f\"; done")
    return "\n".join(lines + ["exit 0"])


def _age(started):
    """Seconds since docker's StartedAt (2026-11-09T10:06:16.123456789Z, UTC)."""
    try:
        t = datetime.strptime(started[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int(time.time() - t.timestamp()))


def _parse(cfg, text):
    """[{id, name, image, started, age_s, xp, devices, cmd, launch}] of the containers that are yours."""
    uid, conts, launch = None, [], {}
    for ln in text.splitlines():
        if ln.startswith("@@UID "):
            uid = ln.split()[1]
        elif ln.startswith("@@C "):
            try:
                conts.append(json.loads(ln[4:]))
            except ValueError:
                continue
        elif ln.startswith("@@L "):
            _, cid, path = ln.split(" ", 2)
            launch[cid] = path.rsplit("/", 2)[-2] if "/" in path else path
    prefix = cfg["project"]["env_prefix"]
    out = []
    for c in conts:
        conf, name = c.get("Config") or {}, (c.get("Name") or "").lstrip("/")
        user = str(conf.get("User") or "").split(":")[0]
        if name.startswith(OWN_PREFIX) or uid is None or user != uid:
            continue
        env = dict(e.split("=", 1) for e in conf.get("Env") or [] if "=" in e)
        devices = next((v for k, v in env.items() if k.startswith(prefix) and k.upper().endswith("DEVICES")), "")
        started = (c.get("State") or {}).get("StartedAt") or ""
        cmd = " ".join(conf.get("Cmd") or [])
        out.append({"id": c.get("Id", "")[:12], "full_id": c.get("Id", ""), "name": name, "image": conf.get("Image") or "",
                    "started": started[:19].replace("T", " "), "age_s": _age(started),
                    "xp": env.get(f"{prefix}SETTINGS__EXPERIMENT") or "", "devices": devices,
                    "cmd": cmd[-300:], "launch": launch.get(c.get("Id", ""), "")})
    return sorted(out, key=lambda j: j["started"], reverse=True)


def _here():
    r = subprocess.run(["bash", "-s"], input=_script(), capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0 or "@@UID" not in r.stdout:
        raise ValueError((r.stderr or r.stdout).strip()[-300:] or "docker is not reachable here")
    return r.stdout


def _there(sv):
    r = subprocess.run(remote.ssh_argv(sv, "bash -s"), input=_script(core.server_paths(sv)["launches"]),
                       capture_output=True, text=True, timeout=40, check=False)
    if r.returncode != 0 or "@@UID" not in r.stdout:
        raise ValueError((r.stderr or r.stdout).strip()[-300:] or "no answer")
    return r.stdout


def listing(cfg):
    """{places: [{server (None: this machine), label, connected, jobs, error}]}: the connected servers are asked in
    parallel; a container seen both here and on a server (this machine is one of them) is listed once, there."""
    servers = [s for s in core.active_servers(cfg)]
    places = [{"server": None, "label": "this machine", "connected": True}] + \
             [{"server": s["name"], "label": s["name"], "connected": remote.connected(s)} for s in servers]

    def ask(p):
        if not p["connected"]:
            return {**p, "jobs": [], "error": "not connected (Servers > Connect)"}
        try:
            text = _here() if p["server"] is None else _there(next(s for s in servers if s["name"] == p["server"]))
            return {**p, "jobs": _parse(cfg, text), "error": ""}
        except (ValueError, OSError, subprocess.TimeoutExpired) as err:
            return {**p, "jobs": [], "error": str(err)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        places = list(pool.map(ask, places))
    remote_ids = {j["full_id"] for p in places if p["server"] for j in p["jobs"]}
    places[0]["jobs"] = [j for j in places[0]["jobs"] if j["full_id"] not in remote_ids]
    return {"places": places, "when": datetime.now().strftime("%H:%M:%S")}


def _target(cfg, server):
    if not server:
        return None
    sv = remote.server(cfg, server)
    if not remote.connected(sv):
        raise ValueError(f"{server} is not connected: click Connect in Servers first")
    return sv


def _check_id(cid):
    if not cid or not all(ch in "0123456789abcdef" for ch in cid) or len(cid) < 12:
        raise ValueError(f"not a container id: '{cid}'")
    return cid


def logs(cfg, server, cid):
    """The last LOGS_TAIL lines of a container (read-only)."""
    cid = _check_id(cid)
    cmd = f"docker logs --tail {LOGS_TAIL} {cid} 2>&1"
    sv = _target(cfg, server)
    argv = remote.ssh_argv(sv, cmd) if sv else ["bash", "-c", cmd]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=40, check=False, stdin=subprocess.DEVNULL)
    return (r.stdout or r.stderr)[-60000:]


def stop_steps(cfg, server, cid):
    """The action that stops a container: docker stop (its process gets SIGTERM, then SIGKILL after 30 s)."""
    cid = _check_id(cid)
    sv = _target(cfg, server)
    where = sv["name"] if sv else "this machine"
    cmd = f"docker stop --time 30 {cid}"
    argv = remote.ssh_argv(sv, cmd) if sv else ["bash", "-c", cmd]
    return where, [actions.step(f"stop the container {cid} on {where}", argv, timeout=60)]


# --------------------------------------------------------------------------- past jobs: the output kept by @@KEEP_LOGS@@

PAST_MAX = 60                                   # logs listed per place (newest first)
LOG_RE = re.compile(r"^(\d{8}_\d{6})_(.+)_([0-9a-f]{12})\.log$")


def _logdir(cfg, server):
    return remote_launch.logdir(remote.server(cfg, server)) if server else str(core.state_path(core.JOB_LOGS))


def _past_script(d):
    q = shlex.quote(d)
    return "\n".join([
        f"D={q}",
        'ls -t "$D"/*.log 2>/dev/null | head -n ' + str(PAST_MAX) + ' | while read -r f; do',
        '  echo "@@F|$(basename "$f")|$(stat -c "%s|%Y" "$f")|$(tail -n 1 "$f" | grep -o "^\\[exit [0-9?-]*\\]")"',
        "done", "exit 0"])


def _parse_past(text, running):
    out = []
    for ln in text.splitlines():
        if not ln.startswith("@@F|"):
            continue
        _, name, size, mtime, last = (ln.split("|") + [""] * 5)[:5]
        m = LOG_RE.match(name)
        if not m:
            continue
        stamp, xp, cid = m.groups()
        code = last[len("[exit "):-1] if last.startswith("[exit ") else None
        state = "running" if cid in running else "unknown" if code in (None, "?") else "done" if code == "0" else "failed"
        out.append({"name": name, "xp": xp, "id": cid, "started": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}",
                    "size": int(size) if size.isdigit() else None, "ended": int(mtime) if mtime.isdigit() else None,
                    "exit": code, "state": state})
    return out


def past(cfg):
    """{places: [{server, label, connected, jobs: [{name, xp, id, started, size, ended, exit, state}], error}]}:
    the kept outputs, newest first; state: running (its container still runs), done (exit 0), failed, unknown (the
    recorder stopped before the job: e.g. the launcher restarted while a job of this machine ran)."""
    now = listing(cfg)
    running = {p["server"] or "": {j["id"] for j in p["jobs"]} for p in now["places"]}
    servers = core.active_servers(cfg)

    def ask(p):
        if not p["connected"]:
            return {**p, "jobs": [], "error": p["error"]}
        try:
            d = _logdir(cfg, p["server"])
            if p["server"] is None:
                r = subprocess.run(["bash", "-s"], input=_past_script(d), capture_output=True, text=True, timeout=30, check=False)
            else:
                sv = next(s for s in servers if s["name"] == p["server"])
                r = subprocess.run(remote.ssh_argv(sv, "bash -s"), input=_past_script(d), capture_output=True, text=True,
                                   timeout=40, check=False)
            return {**p, "folder": d, "jobs": _parse_past(r.stdout, running.get(p["server"] or "", set())), "error": ""}
        except (ValueError, OSError, subprocess.TimeoutExpired) as err:
            return {**p, "jobs": [], "error": str(err)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        places = list(pool.map(ask, now["places"]))
    return {"places": places, "when": now["when"]}


def past_log(cfg, server, name, lines=400):
    """The last `lines` lines of a kept output."""
    if not LOG_RE.match(name or ""):
        raise ValueError(f"not a job log: '{name}'")
    path = _logdir(cfg, server) + "/" + name
    cmd = f"tail -n {int(lines)} {shlex.quote(path)} 2>&1"
    sv = _target(cfg, server)
    argv = remote.ssh_argv(sv, cmd) if sv else ["bash", "-c", cmd]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=40, check=False, stdin=subprocess.DEVNULL)
    return (r.stdout or r.stderr)[-200000:]
