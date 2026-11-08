"""The servers seen from the launcher, over ssh with your keys (ssh agent): one connection per server is opened when you
click Connect and reused by every command after it (ssh ControlMaster), until you disconnect or it expires (4 h).
Nothing connects by itself: without an open connection, commands are not sent (the status says "not connected").

Every command is built here and run through actions.py (shown first, logged after).
"""
import json
import os
import shlex
import subprocess
from pathlib import Path

import actions
import core

CTL_DIR = Path(f"/tmp/launcher-ssh-{os.getuid()}")          # short: a unix socket path is limited to ~100 characters
PERSIST = "4h"


def _target(sv):
    return (["-p", str(sv.get("port") or 22)] + (["-l", sv["user"]] if sv.get("user") else []) + [sv["host"]])


def ssh_argv(sv, command=None, master=False):
    """ssh to a server through its shared connection. master: open it (Connect); otherwise use the open one only."""
    CTL_DIR.mkdir(mode=0o700, exist_ok=True)
    opts = ["-o", f"ControlPath={CTL_DIR}/%C", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    opts += (["-o", "ControlMaster=yes", "-o", f"ControlPersist={PERSIST}", "-f", "-N"] if master
             else ["-o", "ControlMaster=no"])
    return ["ssh", *opts, *_target(sv)] + ([command] if command else [])


def ssh_script(label, sv, script, timeout=60, allow_fail=False):
    """A step that runs a multi-line shell script on the server (ssh ... bash -s, the script as input): logged as
    it runs, readable: ssh ... bash -s <<'EOF' / the script / EOF."""
    argv = ssh_argv(sv, "bash -s")
    return actions.step(label, argv, stdin=script + "\n", timeout=timeout, allow_fail=allow_fail,
                        display=f"{shlex.join(argv)} <<'EOF'\n{script}\nEOF")


def connected(sv):
    """Whether the shared connection to the server is open (asks the local ssh only: nothing is sent)."""
    try:
        r = subprocess.run(["ssh", "-o", f"ControlPath={CTL_DIR}/%C", "-O", "check", *_target(sv)],
                           capture_output=True, text=True, timeout=5, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def server(cfg, name):
    sv = next((s for s in cfg["servers"] if s.get("name") == name), None)
    if sv is None:
        raise ValueError(f"no server named '{name}' in the settings")
    return sv


# --------------------------------------------------------------------------- actions

def connect_steps(sv):
    return [actions.step(f"open the connection to {sv['name']} (your ssh agent asks for the key once)", ssh_argv(sv, master=True), timeout=60)]


def disconnect_steps(sv):
    return [actions.step(f"close the connection to {sv['name']}",
                         ["ssh", "-o", f"ControlPath={CTL_DIR}/%C", "-O", "exit", *_target(sv)], timeout=15)]


def pg_state(sock, port):
    """Shell: running (Postgres answers on its socket), stale (the socket file is left but nothing answers: Postgres
    was killed, e.g. its container stopped without a clean shutdown) or stopped (no socket)."""
    s = shlex.quote(f"{sock}/.s.PGSQL.{port}")
    return (f"if python3 -c 'import socket,sys; c=socket.socket(socket.AF_UNIX); c.settimeout(3); c.connect(sys.argv[1])' {s} 2>/dev/null; "
            f"then echo running; elif test -S {s}; then echo stale; else echo stopped; fi")


def _status_script(sv, central):
    """A read-only shell script: what is running and where, on one server."""
    p = core.server_paths(sv)
    q = shlex.quote
    lines = [
        "echo @@HOST; hostname",
        f"echo @@PG; {pg_state(p['psql_sock'], p['pg_port'])}",
        f"echo @@DB; ls -d {p['db_pattern']} 2>/dev/null | sort",
        "echo @@GPU; nvidia-smi --query-gpu=index,uuid,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null",
        "echo @@APPS; nvidia-smi --query-compute-apps=gpu_uuid --format=csv,noheader 2>/dev/null",
        f"echo @@DISK; df -Pk {q(p['base'])} 2>/dev/null | tail -n 1",
        "echo @@DIRS; for d in " + " ".join(q(p[k]) for k in ("artifacts", "checkpoints", "launcher")) + "; do test -d \"$d\" && echo \"$d\"; done",
    ]
    if central:
        lines += [f"echo @@SYNCPG; {pg_state(p['sync_sock'], p['sync_port'])}",
                  f"echo @@SYNCDB; ls -d {p['sync_db_pattern']} 2>/dev/null | sort",
                  "echo @@SYNCDIRS; for d in " + " ".join(q(p[k]) for k in ("sync_artifacts", "sync_checkpoints")) + "; do test -d \"$d\" && echo \"$d\"; done"]
    return "\n".join(lines + ["exit 0"])


def status_steps(sv):
    return [ssh_script(f"read the state of {sv['name']} (read-only: Postgres sockets, database folders, GPUs, disk)",
                       sv, _status_script(sv, sv.get("central")), timeout=40)]


# --------------------------------------------------------------------------- databases

def _db(sv, which):
    """(socket, port, folder pattern, label) of a server's own database or its synchronised one (the central server's
    is the one the syncs write to; any server can have one, e.g. to prepare a new central server)."""
    p = core.server_paths(sv)
    if which == "sync":
        return p["sync_sock"], p["sync_port"], p["sync_db_pattern"], "synchronised database"
    return p["psql_sock"], p["pg_port"], p["db_pattern"], "own database"


SYNC_CONTAINER = "launcher-synchronised-db"
CONTAINERS = {"own": "launcher-db", "sync": SYNC_CONTAINER}
HERE = Path(__file__).resolve().parent


def db_status_steps(sv, image=""):
    """Read-only: Postgres sockets, the database containers (yours: server.sh, or the synchronised one), the folder
    each Postgres uses (asked inside its container), the dated folders, and whether _empty_db exists."""
    p = core.server_paths(sv)
    q = shlex.quote
    lines = ["echo @@CONTAINERS",
             "docker ps -q | xargs -r docker inspect --format "
             "'{{.Id}}|{{.Config.User}}|{{.State.StartedAt}}|{{.Config.Image}}|{{.Name}}|{{json .Config.Env}}|{{json .Config.Cmd}}' 2>/dev/null"
             f" | awk -F'|' -v u=\"$(id -u)\" '$2==u && ($0 ~ /server(-ssl)?\\.sh/ || $5 ~ /launcher-(synchronised-)?db/)'",
             f"echo @@EMPTY; test -d {q(p['empty_db'])} && echo yes || echo no",
             # the images on the server (of the same repository as the default one), to choose the one to start
             "echo @@IMAGES; docker images " + (q(image.rsplit(":", 1)[0]) + " " if image else "")
             + "--format '{{.Repository}}:{{.Tag}}|{{.CreatedSince}}|{{.Size}}' 2>/dev/null | grep -v '<none>'"]
    # the database containers only (yours: server.sh, or the launcher's), to ask Postgres its folder
    lines.append("DBC=$(docker ps -q | xargs -r docker inspect --format '{{.Id}}|{{.Config.User}}|{{.Name}}|{{json .Config.Cmd}}' 2>/dev/null"
                 " | awk -F'|' -v u=\"$(id -u)\" '$2==u && ($4 ~ /server(-ssl)?\\.sh|serve\\.sh/ || $3 ~ /launcher-(synchronised-)?db/) {print $1}')")
    for which in ("own", "sync"):
        sock, port, pattern, _ = _db(sv, which)
        lines += [f"echo @@{which.upper()}_SOCK; {pg_state(sock, port)}",
                  f"echo @@{which.upper()}_FOLDERS; ls -d {pattern} 2>/dev/null | sort",
                  f"echo @@{which.upper()}_DATADIR",
                  f"for c in $DBC; do docker exec \"$c\" psql -h {q(sock)} -p {port} -U \"$(id -un)\" -d mlruns_db -Atc 'show data_directory' 2>/dev/null | grep '^/' && break; done"]
    return [ssh_script(f"read the databases of {sv['name']} (read-only: sockets, containers, folders)", sv, "\n".join(lines + ["exit 0"]), timeout=60)]


def parse_db_status(sv, text):
    sec, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith("@@"):
            cur = ln[2:].strip()
            sec[cur] = []
        elif cur and ln.strip():
            sec[cur].append(ln.strip())
    containers = []
    for ln in sec.get("CONTAINERS", []):
        parts = ln.split("|", 6)
        if len(parts) < 7:
            continue
        cid, _user, started, image, name, env, cmd = parts
        try:
            env = dict(e.split("=", 1) for e in json.loads(env) if "=" in e)
        except ValueError:
            env = {}
        kind = "sync" if SYNC_CONTAINER in name else "own"
        containers.append({"id": cid[:12], "started": started[:19].replace("T", " "), "image": image, "name": name.lstrip("/"),
                           "kind": kind, "cmd": cmd, "folder": env.get("FOLDER") or env.get("POSTGRES_DATA") or None,
                           "by": "launcher" if name.lstrip("/") in CONTAINERS.values() else "launch-server.sh",
                           "synched": env.get("SYNCHED") == "true", "allow_modif": env.get("ALLOW_MODIF") == "true"})
    images = []
    for ln in sec.get("IMAGES", []):
        name, _, rest = ln.partition("|")
        created, _, size = rest.partition("|")
        images.append({"image": name, "created": created, "size": size})
    out = {"empty_db": (sec.get("EMPTY") or ["no"])[0] == "yes", "containers": containers, "images": images}
    for which in ("own", "sync"):
        w = which.upper()
        out[which] = {"state": (sec.get(f"{w}_SOCK") or ["?"])[0], "folders": sec.get(f"{w}_FOLDERS", []),
                      # a path, or nothing (docker exec errors such as "OCI runtime exec failed" can come on stdout)
                      "data_directory": next((ln for ln in sec.get(f"{w}_DATADIR", []) if ln.startswith("/")), None),
                      "container": next((c for c in containers if c["kind"] == which), None)}
    return out


def _scp_target(sv, path):
    return (f"{sv['user']}@" if sv.get("user") else "") + f"{sv['host']}:{path}"


def db_start_steps(sv, which, folder, allow_modif=False, image=""):
    """Start a database with the launcher's db/launch.sh (copied to <launcher>/db on the server first): a container of
    `image` runs db/serve.sh, Postgres on the database's socket and port, then the MLflow UI (port 5923 for the own
    database, 5924 for the synchronised one). The image must be on the server already (Pull image): nothing is
    downloaded while starting."""
    p = core.server_paths(sv)
    sock, port, _, label = _db(sv, which)
    if not folder:
        raise ValueError("choose the database folder to start")
    if not image:
        raise ValueError("no image to start the database in: choose one")
    q = shlex.quote
    dest = f"{p['launcher']}/db"
    CTL_DIR.mkdir(mode=0o700, exist_ok=True)
    scp = ["scp", "-q", "-o", f"ControlPath={CTL_DIR}/%C", "-o", "BatchMode=yes", "-o", "ControlMaster=no",
           "-P", str(sv.get("port") or 22), str(HERE / "db" / "launch.sh"), str(HERE / "db" / "serve.sh"), _scp_target(sv, dest + "/")]
    ui = p["ui_port"] if which == "own" else p["sync_ui_port"]
    script = "\n".join([
        f"docker image inspect {q(image)} >/dev/null 2>&1 || {{ echo '{image} is not on {sv['name']}: pull it first (Pull image)' >&2; exit 1; }}",
        f"bash {q(dest)}/launch.sh --image {q(image)} --name {CONTAINERS[which]} --label {which} \\",
        f"  --folder {q(folder)} --socket {q(sock)} --port {port} --ui-port {ui}" + (" --allow-modif" if allow_modif else "")])
    return [ssh_script(f"make the folder of the launcher's database scripts on {sv['name']}", sv, f"mkdir -p {q(dest)}", timeout=30),
            actions.step(f"copy db/launch.sh and db/serve.sh to {dest}", scp, timeout=60),
            ssh_script(f"start the {label} of {sv['name']} on {folder.rsplit('/', 1)[-1]} ({image}, MLflow UI on port {ui})", sv, script, timeout=90)]


def db_pull_steps(sv, image):
    """docker pull of an image on the server (can take minutes)."""
    if not image:
        raise ValueError("no image to pull")
    return [actions.step(f"pull {image} on {sv['name']}", ssh_argv(sv, f"docker pull --quiet {shlex.quote(image)}"), timeout=1800)]


def db_stop_steps(sv, which, container):
    """Stop a database container (as your stop.sh does: docker container stop)."""
    if not container:
        raise ValueError("no running database container was found: read the state first")
    _, _, _, label = _db(sv, which)
    return [actions.step(f"stop the {label} of {sv['name']} (container {container})", ssh_argv(sv, f"docker container stop {shlex.quote(container)}"), timeout=90)]


def db_new_steps(sv, which, today):
    """A new, empty, dated database folder: a copy of _empty_db (as your reset_db.sh --new does)."""
    p = core.server_paths(sv)
    q = shlex.quote
    target = f"{p['base']}/{sv['name'].lower()}_{today}_db" if which == "own" else f"{p['synchronised']}/db_{today}"
    lines = [f"test -d {q(p['empty_db'])} || {{ echo 'no {p['empty_db']}' >&2; exit 1; }}",
             f"test ! -e {q(target)} || {{ echo '{target} exists already' >&2; exit 1; }}"]
    if which == "sync":
        lines.append("mkdir -p " + " ".join(q(p[k]) for k in ("synchronised", "sync_artifacts", "sync_checkpoints", "sync_sock")))
    lines.append(f"cp -r {q(p['empty_db'])} {q(target)}")
    return [ssh_script(f"create {target.rsplit('/', 1)[-1]} on {sv['name']} (a copy of {p['empty_db'].rsplit('/', 1)[-1]})", sv, "\n".join(lines), timeout=600)]


def db_sizes_steps(sv, which):
    _, _, pattern, label = _db(sv, which)
    return [actions.step(f"measure the {label} folders of {sv['name']}", ssh_argv(sv, f"du -s --block-size=1M {pattern} 2>/dev/null; true"), timeout=600)]


def db_scripts_steps(sv):
    p = core.server_paths(sv)
    d = shlex.quote(f"{p['launcher']}/db")
    return [actions.step(f"show the database scripts on {sv['name']} (copied there at each start)",
                         ssh_argv(sv, f"cat {d}/launch.sh {d}/serve.sh"), timeout=30)]


def parse_status(text):
    sec, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith("@@"):
            cur = ln[2:].strip()
            sec[cur] = []
        elif cur:
            sec[cur].append(ln.strip())
    busy = {ln for ln in sec.get("APPS", []) if ln}
    gpus = []
    for ln in sec.get("GPU", []):
        parts = [x.strip() for x in ln.split(",")]
        if len(parts) == 4 and parts[0].isdigit():
            used = float(parts[2] or 0)
            gpus.append({"index": int(parts[0]), "mem_used": used, "mem_total": float(parts[3] or 0),
                         "free": parts[1] not in busy and used < 512})
    disk = (sec.get("DISK") or [""])[0].split()
    dbs = [d for d in sec.get("DB", []) if d]
    out = {"hostname": (sec.get("HOST") or [""])[0], "pg": (sec.get("PG") or ["?"])[0], "dbs": dbs,
           "db_latest": dbs[-1] if dbs else None, "gpus": gpus, "dirs": [d for d in sec.get("DIRS", []) if d],
           "disk_free_gb": round(int(disk[3]) / 1024 ** 2, 1) if len(disk) >= 4 and disk[3].isdigit() else None}
    if "SYNCPG" in sec:
        sdbs = [d for d in sec.get("SYNCDB", []) if d]
        out.update(sync_pg=(sec["SYNCPG"] or ["?"])[0], sync_dbs=sdbs, sync_db_latest=sdbs[-1] if sdbs else None,
                   sync_dirs=[d for d in sec.get("SYNCDIRS", []) if d])
    return out
