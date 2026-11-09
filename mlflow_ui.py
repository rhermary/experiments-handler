"""The MLflow UI inside the launcher: an `mlflow server` started on demand on a local port, reached through the
launcher at /mlflow/ (same origin, so the page can show it in a frame and nothing else has to be reachable).
It stops with the launcher.

The server runs in a Docker container of the project's image (Settings: MLflow server image; empty: the Docker image
variable), so its MLflow is the one that wrote the databases: a newer MLflow refuses a database whose schema is older
than its own. The Results tab then reads the runs through that server (sources.py). "local" runs the launcher's own
mlflow instead, and the Results tab opens the database itself.
"""
import atexit
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import requests

PREFIX = "/mlflow"
_LOCK = threading.Lock()
_STATE = {"proc": None, "port": None, "uri": None, "image": None, "log": None, "started": None, "generation": 0}
CONTAINER = f"launcher-mlflow-ui-{os.getuid()}"
LOCAL = "local"
def LOG_PATH():  # noqa: N802  # pylint: disable=invalid-name
    """Log of the MLflow server started by the app, next to the settings."""
    import core                                       # pylint: disable=import-outside-toplevel
    return core.state_path(".mlflow-server.log")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
       "upgrade", "content-length", "content-encoding", "host"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive():
    p = _STATE["proc"]
    return p is not None and p.poll() is None


def image(cfg):
    """The image the server runs in, or None for the launcher's own mlflow."""
    img = (cfg["project"].get("mlflow_image") or "").strip()
    if img.lower() == LOCAL:
        return None
    img = img or next((v["value"] for v in cfg["variables"]["docker"] if v["name"] == "IMAGE_NAME"), "")
    if not img:
        raise ValueError("no image for the MLflow server: set it in Settings (MLflow server image), or 'local'")
    return img


def generation():
    """Changes at each (re)start: what was read through the previous server is not reused."""
    return _STATE["generation"]


def status():
    """{state: off | starting | running | failed, uri, image, log (last lines when it failed)}."""
    if not _alive():
        if _STATE["proc"] is not None:
            return {"state": "failed", "uri": _STATE["uri"], "image": _STATE["image"], "log": _tail()}
        return {"state": "off"}
    try:
        r = requests.get(f"http://127.0.0.1:{_STATE['port']}{PREFIX}/health", timeout=1)
        ok = r.status_code == 200
    except requests.RequestException:
        ok = False
    return {"state": "running" if ok else "starting", "uri": _STATE["uri"], "image": _STATE["image"],
            "since": _STATE["started"]}


def _tail(n=15):
    try:
        return "\n".join(LOG_PATH().read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _in_container(uri):
    """(URI as the container sees it, folders to mount): the unix-socket folder of a Postgres (host=/...), the folder
    of a SQLite file or of a file store (relative paths made absolute: the container has another working folder)."""
    parts = urllib.parse.urlsplit(uri)
    scheme = parts.scheme.lower()
    if scheme.startswith("sqlite"):
        path = os.path.abspath(uri.split(":///", 1)[1].split("?")[0]) if ":///" in uri else ""
        return (f"sqlite:///{path}", [os.path.dirname(path)]) if path else (uri, [])
    if scheme in ("", "file"):
        path = os.path.abspath(parts.path if scheme == "file" else uri)
        return f"file://{path}", [path]
    hosts = [h for v in urllib.parse.parse_qs(parts.query).get("host", []) for h in v.split(",")]
    hosts += [urllib.parse.unquote(parts.hostname)] if parts.hostname and parts.hostname.startswith("%2f") else []
    return uri, sorted({h for h in hosts if h.startswith("/")})


_SELF = {}


def _self_container():
    """(id, mounted folders) of the container the launcher runs in (a dev container using the host's Docker: the new
    container then shares its network and its mounts, as paths and 127.0.0.1 here mean nothing to the host), or None."""
    if "v" not in _SELF:
        _SELF["v"] = None
        if os.path.exists("/.dockerenv"):
            try:
                r = subprocess.run(["docker", "inspect", "--format", "{{json .Mounts}}", socket.gethostname()],
                                   capture_output=True, text=True, timeout=15, check=False)
                if r.returncode == 0:
                    _SELF["v"] = (socket.gethostname(), [m["Destination"] for m in json.loads(r.stdout or "[]")])
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    return _SELF["v"]


def _command(uri, img, port):
    serve = ["mlflow", "server", "--backend-store-uri", uri, "--host", "127.0.0.1", "--port", str(port),
             "--static-prefix", PREFIX, "--workers", "2"]
    if not img:
        return [sys.executable, "-m", *serve, "--x-frame-options", "SAMEORIGIN"]
    uri, mounts = _in_container(uri)
    serve[3] = uri
    me = _self_container()
    if me:
        hidden = [m for m in mounts if not any(m == d or m.startswith(d.rstrip("/") + "/") for d in me[1])]
        if hidden:
            raise ValueError(f"{', '.join(hidden)}: not on a folder mounted in the launcher's container, so the MLflow "
                             "server container cannot see it (or set the MLflow server image to 'local')")
        net = ["--network", f"container:{me[0]}", "--volumes-from", me[0]]
    else:
        net = ["--network", "host", *[a for m in mounts for a in ("--volume", f"{m}:{m}")]]
    # as in db/launch.sh: your user (it can open the forwarded sockets), a login shell (the image's PATH)
    return ["docker", "run", "--rm", "--pull", "never", "--name", CONTAINER, *net,
            "--user", f"{os.getuid()}:{os.getgid()}", "--volume", "/etc/passwd:/etc/passwd:ro",
            "--volume", "/etc/group:/etc/group:ro", "-e", "HOME=/tmp", "-e", "MLFLOW_DISABLE_AGENT_HINT=1",
            img, "/usr/bin/bash", "-lc", "exec " + shlex.join(serve)]


_HEADS = {}                                  # image -> the database schema its MLflow writes (alembic head)
HEAD_PY = ("import os, mlflow; from alembic.config import Config; from alembic.script import ScriptDirectory; "
           "c = Config(); c.set_main_option('script_location', os.path.join(os.path.dirname(mlflow.__file__), "
           "'store', 'db_migrations')); print('@@', mlflow.__version__, ScriptDirectory.from_config(c).get_current_head())")


def _image_schema(img):
    """(MLflow version, schema revision) of the image (asked once per image)."""
    if img not in _HEADS:
        r = subprocess.run(["docker", "run", "--rm", "--pull", "never", img, "/usr/bin/bash", "-lc",
                            "python3 -c " + shlex.quote(HEAD_PY) + " 2>/dev/null || python -c " + shlex.quote(HEAD_PY)],
                           capture_output=True, text=True, timeout=120, check=False)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("@@ ")), "")
        if not line:
            raise ValueError(f"cannot read the MLflow version of {img}: {(r.stderr or r.stdout).strip()[-400:]}"
                             + (f" (pull it first: docker pull {img})" if "No such image" in r.stderr else ""))
        _HEADS[img] = tuple(line.split()[1:3])
    return _HEADS[img]


def _db_schema(uri):
    """The schema revision of a SQL database (alembic_version), None for other stores."""
    if not uri.split(":", 1)[0].lower().startswith(("postgresql", "mysql", "mssql", "sqlite")):
        return None
    import sqlalchemy                                       # pylint: disable=import-outside-toplevel
    engine = sqlalchemy.create_engine(uri, poolclass=sqlalchemy.pool.NullPool)
    try:
        with engine.connect() as conn:
            return conn.execute(sqlalchemy.text("SELECT version_num FROM alembic_version")).scalar()
    except sqlalchemy.exc.SQLAlchemyError:
        return None                                         # no table: an empty database (MLflow creates it)
    finally:
        engine.dispose()


def check_schema(uri, img):
    """Refuse an image whose MLflow writes another schema than the database's: MLflow would try to create tables and
    migrate the database (a newer MLflow), or fail on a revision it does not know (an older one)."""
    version, head = _image_schema(img)
    found = _db_schema(uri)
    if found and found != head:
        raise ValueError(f"{img} has MLflow {version} (database schema {head}), but the database is at schema {found}: "
                         "set the MLflow server image (Settings) to an image whose MLflow wrote this database")


def start(uri, img=None):
    """Start the server for the tracking URI `uri`, in the image `img` (None: the launcher's mlflow); restarted
    when either changed."""
    with _LOCK:
        if _alive() and _STATE["uri"] == uri and _STATE["image"] == img:
            return status()
        if img:
            check_schema(uri, img)
        stop()
        if img:
            _leftover()
        port = _free_port()
        log = open(LOG_PATH(), "w", encoding="utf8")          # pylint: disable=consider-using-with  # kept while it runs
        cmd = _command(uri, img, port)
        log.write("$ " + shlex.join(cmd) + "\n")
        log.flush()
        env = {**os.environ, "MLFLOW_DISABLE_AGENT_HINT": "1"}
        _STATE.update(proc=subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True,
                                            stdin=subprocess.DEVNULL),
                      port=port, uri=uri, image=img, log=log, started=time.time(), generation=_STATE["generation"] + 1)
    return status()


def ensure(uri, img, wait=90):
    """The URI of a running server for `uri` (started if needed), for the MLflow clients of the Results tab."""
    s = start(uri, img)
    for _ in range(wait):
        if s["state"] != "starting":
            break
        time.sleep(1)
        s = status()
    if s["state"] != "running":
        raise ValueError(f"the MLflow server in {img} is {s['state']}" + (f":\n{s.get('log')}" if s.get("log") else ""))
    return f"http://127.0.0.1:{_STATE['port']}{PREFIX}"


def stop():
    p = _STATE["proc"]
    if p is not None and p.poll() is None:
        if _STATE["image"]:
            subprocess.run(["docker", "stop", "--time", "10", CONTAINER], capture_output=True, timeout=30, check=False)
        try:
            os.killpg(p.pid, 15)
            p.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            p.kill()
    if _STATE["log"]:
        _STATE["log"].close()
    _STATE.update(proc=None, port=None, log=None)


def _leftover():
    """A container left by a launcher that was killed: removed (its name is taken otherwise)."""
    try:
        subprocess.run(["docker", "rm", "--force", CONTAINER], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


atexit.register(stop)


def proxy(flask_request, path):
    """Forward a request under /mlflow/ to the server; (body iterator, status, headers)."""
    if not _alive():
        return [b"MLflow server is not running: open the MLflow tab to start it."], 503, {"Content-Type": "text/plain"}
    url = f"http://127.0.0.1:{_STATE['port']}{PREFIX}/{path}"
    headers = {k: v for k, v in flask_request.headers if k.lower() not in HOP}
    headers["Host"] = f"127.0.0.1:{_STATE['port']}"
    r = requests.request(flask_request.method, url, params=flask_request.args, data=flask_request.get_data(),
                         headers=headers, stream=True, allow_redirects=False, timeout=120)
    out = [(k, v) for k, v in r.raw.headers.items() if k.lower() not in HOP]
    out = [(k, v.replace(f"http://127.0.0.1:{_STATE['port']}", "")) if k.lower() == "location" else (k, v) for k, v in out]
    out = [(k, "SAMEORIGIN") if k.lower() == "x-frame-options" else (k, v) for k, v in out]     # shown in a frame
    return r.raw.stream(65536, decode_content=True), r.status_code, out
