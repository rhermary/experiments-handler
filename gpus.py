"""GPU usage of the servers (the Python version of states.sh, without the docker part), with as few ssh calls as possible.

Current server (the one the launcher runs on, possibly inside a docker container):
    GPUs and their processes come from the local nvidia-smi. Only the owner and command of a process need the host,
    because a container cannot see the host's processes: they are asked with one ssh call to the docker host, for the
    PIDs not seen before (a process keeps its owner and command, so they are cached). A refresh with no new process
    makes no ssh call.
Other servers:
    one ssh call per refresh (hostname, GPUs, processes and their owners in one script). Connections are shared
    (ControlMaster) so that refreshes do not open a new connection each time.
Which server is the current one is found once and cached in .gpu-servers.json: by host name (no ssh), else by asking
the docker host (one ssh call) and checking that its GPUs are the local ones (same UUIDs).
"""
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time

from pathlib import Path

import core
# next to the settings (core.state_path): which server this is (cached), and optionally userlist.csv, a passwd-format
# list of the users of the servers, to name the owners of processes that ps only shows by UID


def CACHE_PATH():  # noqa: N802  # pylint: disable=invalid-name
    return core.state_path(".gpu-servers.json")


def USERS_FILE():  # noqa: N802  # pylint: disable=invalid-name
    return core.state_path("userlist.csv")
GPU_FIELDS = ("index", "name", "memory.used", "memory.total", "memory.free", "utilization.gpu", "temperature.gpu",
              "gpu_uuid")
APP_FIELDS = ("pid", "gpu_uuid", "used_gpu_memory")
PS_FIELDS = "pid=,uid=,user:32=,etimes=,args="
EXPERIMENT = re.compile(r"--experiment[= ](\S+)|APP_SETTINGS__EXPERIMENT=(\S+)")
SSH_OPTS = ["-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "StrictHostKeyChecking=no",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={tempfile.gettempdir()}/xp-launcher-ssh-%C",
            "-o", "ControlPersist=300"]
REMOTE = f"""
echo @@HOST; hostname
echo @@GPUS; nvidia-smi --query-gpu={','.join(GPU_FIELDS)} --format=csv,noheader,nounits
apps=$(nvidia-smi --query-compute-apps={','.join(APP_FIELDS)} --format=csv,noheader,nounits)
echo @@APPS; echo "$apps"
pids=$(echo "$apps" | cut -d, -f1 | tr -d ' ' | paste -sd, -)
echo @@PS; [ -n "$pids" ] && ps --no-headers -o {PS_FIELDS} -p "$pids"
echo @@END
"""

_LOCK = threading.Lock()
_PS = {}                       # (server key, pid) -> {user, uid, start (epoch s), cmd}


# --------------------------------------------------------------------------- running commands

def _run(args, timeout=10):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ValueError(f"{args[0]}: {r.stderr.strip() or r.stdout.strip() or 'failed'}")
    return r.stdout


def ssh(target, command, timeout=25):
    """Run `command` on target {host, port, user}; the output, or ValueError("unreachable ...")."""
    args = ["ssh", *SSH_OPTS, "-p", str(target.get("port") or 22)]
    if target.get("user"):
        args += ["-l", target["user"]]
    args += [target["host"], command]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as err:
        raise ValueError(f"{target['host']}: no answer after {timeout} s") from err
    if r.returncode == 255:                                  # ssh's own failure (the remote command never ran)
        raise ValueError(f"{target['host']}:{target.get('port') or 22} unreachable"
                         + (f" ({r.stderr.strip()})" if r.stderr.strip() else " (timeout, refused or key not accepted)"))
    return r.stdout


# --------------------------------------------------------------------------- parsing

def _csv(text, fields):
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= len(fields):
            extra = len(parts) - len(fields)                 # a GPU name may contain commas
            parts = [parts[0], ", ".join(parts[1:2 + extra]), *parts[2 + extra:]] if extra else parts
            out.append(dict(zip(fields, parts)))
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _users():
    out = {}
    try:
        for line in USERS_FILE().read_text().splitlines():
            parts = line.strip().split(":")
            if len(parts) >= 3 and parts[2].isdigit():
                out[parts[2]] = parts[0]
    except OSError:
        pass
    return out


def _ps_lines(text):
    """pid -> {user, uid, start, cmd} from `ps -o pid=,uid=,user=,etimes=,args=` output."""
    out, names, now = {}, None, time.time()
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        pid, uid, user, elapsed, cmd = parts
        if user == uid:                                      # no name for this UID where ps ran
            names = _users() if names is None else names
            user = names.get(uid, uid)
        out[pid] = {"user": user, "uid": uid, "start": now - int(elapsed) if elapsed.isdigit() else None, "cmd": cmd}
    return out


def _sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("@@") and line[2:].isupper():
            cur = line[2:]
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _assemble(gpus, apps, procs, missing_reason=""):
    now, by_uuid = time.time(), {}
    for a in apps:
        p = procs.get(a["pid"])
        m = EXPERIMENT.search(p["cmd"]) if p else None
        by_uuid.setdefault(a["gpu_uuid"], []).append({
            "pid": a["pid"], "mem": _num(a["used_gpu_memory"]), "visible": p is not None,
            "user": p["user"] if p else None, "elapsed": int(now - p["start"]) if p and p["start"] else None,
            "cmd": p["cmd"] if p else None, "xp": (m.group(1) or m.group(2)) if m else None,
            "why": "" if p else missing_reason})
    return [{"index": int(g["index"]), "name": g["name"], "uuid": g["gpu_uuid"],
             "mem_used": _num(g["memory.used"]), "mem_total": _num(g["memory.total"]), "mem_free": _num(g["memory.free"]),
             "util": _num(g["utilization.gpu"]), "temp": _num(g["temperature.gpu"]),
             "processes": by_uuid.get(g["gpu_uuid"], [])} for g in gpus]


# --------------------------------------------------------------------------- which server is this

def _local_gpus():
    text = _run(["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits"])
    return _csv(text, GPU_FIELDS)


def _local_apps():
    text = _run(["nvidia-smi", f"--query-compute-apps={','.join(APP_FIELDS)}", "--format=csv,noheader,nounits"])
    return _csv(text, APP_FIELDS)


def _in_docker():
    if Path("/.dockerenv").exists():
        return True
    try:
        return "docker" in Path("/proc/1/cgroup").read_text()
    except OSError:
        return False


def _gateway():
    """Default gateway (the docker host, from a container on the default bridge)."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                return socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
    except (OSError, ValueError):
        pass
    return None


def _match(servers, hostname):
    h = (hostname or "").lower().split(".")[0]
    return next((s for s in servers if s["name"].lower() == h or s["host"].lower().split(".")[0] == h), None)


def _load_cache():
    try:
        return json.loads(CACHE_PATH().read_text())
    except (OSError, ValueError):
        return {}


def identify(servers, refresh=False):
    """{server (name or None), hostname, via ({host, port, user} to reach the host's processes, or None when ps sees
    them directly), how}. Cached; at most one ssh call when the cache does not match the local GPUs."""
    try:
        uuids = sorted(g["gpu_uuid"] for g in _local_gpus())
    except (OSError, ValueError):
        return {"server": None, "hostname": socket.gethostname(), "via": None, "how": "no nvidia-smi here", "gpus": False}
    cache = _load_cache()
    names = {s["name"] for s in servers}
    if not refresh and cache.get("uuids") == uuids and (cache.get("server") in names or cache.get("server") is None):
        return {**cache, "how": cache.get("how", "") + " (cached)", "gpus": True}
    hostname = socket.gethostname()
    found, via, how = _match(servers, hostname), None, "host name"
    if found is None and _in_docker():
        gw = _gateway()
        user = next((s.get("user") for s in servers if s.get("user")), None) or os.environ.get("USER")
        if gw:
            target = {"host": gw, "port": 22, "user": user}
            try:
                lines = ssh(target, "hostname; nvidia-smi --query-gpu=gpu_uuid --format=csv,noheader", timeout=10).split()
                if lines and set(lines[1:]) & set(uuids):
                    hostname, via, how = lines[0], target, f"docker host {gw} (same GPUs)"
                    found = _match(servers, hostname)
            except ValueError as err:
                how = f"docker host {gw} not reachable: {err}"
    out = {"server": found["name"] if found else None, "hostname": hostname, "via": via, "how": how, "uuids": uuids}
    try:
        CACHE_PATH().write_text(json.dumps(out, indent=1))
    except OSError:
        pass
    return {**out, "gpus": True}


# --------------------------------------------------------------------------- state of one server

def _owners(key, pids, via):
    """Owner / command of `pids`: local ps first, then the cache, then one ssh call to `via` for the rest."""
    procs, calls = {}, 0
    with _LOCK:
        for k in [k for k in _PS if k[0] == key and k[1] not in pids]:
            del _PS[k]                                       # finished processes
        procs.update({pid: _PS[(key, pid)] for pid in pids if (key, pid) in _PS})
    todo = [p for p in pids if p not in procs]
    if todo:
        try:
            procs.update(_ps_lines(_run(["ps", "--no-headers", "-o", PS_FIELDS, "-p", ",".join(todo)])))
        except ValueError:                                   # ps exits 1 when none is visible
            pass
        todo = [p for p in todo if p not in procs]
    error = ""
    if todo and via:
        try:
            procs.update(_ps_lines(ssh(via, f"ps --no-headers -o {PS_FIELDS} -p {','.join(todo)}; true")))
            calls += 1
        except ValueError as err:
            error = str(err)
    with _LOCK:
        for pid in pids:
            if pid in procs:
                _PS[(key, pid)] = procs[pid]
    return procs, calls, error


def state(servers, name=None):
    """GPUs of server `name` (None: the current one). {server, hostname, mode, ssh_calls, gpus: [...], note}."""
    me = identify(servers)
    if name in (None, "", "local") or name == me["server"]:
        if not me["gpus"]:
            raise ValueError("nvidia-smi is not available on this machine")
        gpus, apps = _local_gpus(), _local_apps()
        procs, calls, error = _owners(("local",), [a["pid"] for a in apps], me["via"])
        why = error or ("in another container or namespace, and the docker host is not reachable by ssh"
                        if not me["via"] else "")
        return {"server": me["server"] or "local", "hostname": me["hostname"], "mode": "local", "ssh_calls": calls,
                "via": me["via"]["host"] if me["via"] else None, "how": me["how"], "gpus": _assemble(gpus, apps, procs, why),
                "note": error}
    target = next((s for s in servers if s["name"] == name), None)
    if target is None:
        raise ValueError(f"unknown server '{name}'")
    out = _sections(ssh(target, REMOTE))
    if "GPUS" not in out:
        raise ValueError(f"{name}: unexpected answer (is nvidia-smi installed?)")
    gpus, apps = _csv(out["GPUS"], GPU_FIELDS), _csv(out.get("APPS", ""), APP_FIELDS)
    procs = _ps_lines(out.get("PS", ""))
    return {"server": name, "hostname": out.get("HOST", "").strip() or target["host"], "mode": "ssh", "ssh_calls": 1,
            "via": target["host"], "how": "", "gpus": _assemble(gpus, apps, procs, "not visible to ps"), "note": ""}


def state_all(servers):
    """Every server at once (in parallel; one ssh call per server, the current one read locally), like the tmux
    view of states.sh: [{server, ok, error, ms, ...state}]. An unreachable server does not delay the others beyond
    its own ssh timeout."""
    from concurrent.futures import ThreadPoolExecutor   # pylint: disable=import-outside-toplevel
    me = identify(servers)

    def one(s):
        t = time.time()
        try:
            out = state(servers, s["name"])
            return {**out, "server": s["name"], "ok": True, "ms": int((time.time() - t) * 1000)}
        except (ValueError, OSError, subprocess.SubprocessError) as err:
            return {"server": s["name"], "ok": False, "error": str(err), "ms": int((time.time() - t) * 1000)}

    with ThreadPoolExecutor(max_workers=max(1, len(servers))) as pool:
        results = list(pool.map(one, servers))
    return {"servers": results, "current": me["server"]}
