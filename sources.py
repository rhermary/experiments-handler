"""Where the Results and MLflow tabs read the runs: the project's tracking URI (local), the synchronised database
of the central server, or the own database of another server. A remote one is reached through an ssh tunnel from a
TCP port of 127.0.0.1 here to its Postgres socket, opened when you choose it (on the open connection to that server)
and closed when you go back to local or choose another. A TCP port and not a local socket: the MLflow server container shares the launcher's network, not its
files (mlflow_ui.py). That tunnel is the viewer's own: the syncs open and close theirs without touching it.

The choice is kept while the launcher runs (it starts on local). The runs' files (artifacts) of a remote database are
on its server: with a server's own database, the Results tab does not open them (no artifact view) and the MLflow UI
lists them but cannot open them (files: False in view()).
"""
import getpass
import re
import socket
import threading
import time
import urllib.parse

import actions
import core
import mlflow_ui
import remote
import sync

_LOCK = threading.Lock()
_STATE = {"which": "local", "server": None, "port": None}           # which: local | sync | own; port: local end of the tunnel
_OK = {}                                # tracking URI -> when it last answered
OK_S = 30                               # how long an answer is trusted
TIMEOUT_S = 5


def _shown(uri):
    """The URI without its query and its password."""
    return re.sub(r"//([^:/@]+):[^@/]*@", r"//\1:***@", uri.split("?")[0])


def _reachable(uri):
    """Fail fast when the database of `uri` does not answer: MLflow itself retries a database it cannot reach for
    about an hour (create_sqlalchemy_engine_with_retry), and the tabs would wait as long. SQLite and file stores are
    local files: not checked."""
    if time.time() - _OK.get(uri, 0) < OK_S:
        return
    scheme = uri.split(":", 1)[0].lower()
    try:
        if scheme.startswith(("postgresql", "mysql", "mssql")):
            import sqlalchemy                               # pylint: disable=import-outside-toplevel
            args = {"connect_timeout": TIMEOUT_S} if scheme.startswith(("postgresql", "mysql")) else {}
            engine = sqlalchemy.create_engine(uri, connect_args=args, poolclass=sqlalchemy.pool.NullPool)
            try:
                with engine.connect() as conn:
                    conn.execute(sqlalchemy.text("SELECT 1"))
            finally:
                engine.dispose()
        elif scheme in ("http", "https"):
            import requests                                 # pylint: disable=import-outside-toplevel
            requests.get(uri.rstrip("/") + "/health", timeout=TIMEOUT_S).raise_for_status()
    except Exception as err:                                # pylint: disable=broad-except  # driver / HTTP errors
        raise ValueError(f"the database {_shown(uri)} does not answer: {str(err).strip().splitlines()[0][:300]}") from err
    _OK[uri] = time.time()


def _alive(port):
    """Whether the local end of the tunnel accepts connections (ssh is forwarding it)."""
    with socket.socket() as c:
        c.settimeout(3)
        try:
            c.connect(("127.0.0.1", int(port)))
        except (OSError, TypeError, ValueError):
            return False
    return True


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _forward(sv, port, op, which="sync"):
    """ssh -O forward | cancel of 127.0.0.1:<port> to the Postgres socket of a server's database (sync | own)."""
    sock, pg_port, _, _ = remote._db(sv, which)             # pylint: disable=protected-access
    return ["ssh", "-o", f"ControlPath={remote.CTL_DIR}/%C", "-O", op, "-L", f"127.0.0.1:{port}:{sock}/.s.PGSQL.{pg_port}",
            *remote._target(sv)]                            # pylint: disable=protected-access


def _central(cfg):
    sv = core.central_server(cfg)
    if sv is None:
        raise ValueError("no central server in the settings (Servers): it keeps the synchronised database")
    return sv


def _user(sv):
    return sv.get("user") or getpass.getuser()


def _sync_uri(sv, port):
    return f"postgresql+psycopg2://{urllib.parse.quote(_user(sv))}@127.0.0.1:{port}/{sync.DB_NAME}"


def tracking_uri(cfg):
    """The tracking URI the Results tab reads: the MLflow server of the launcher when it runs in a container (its
    MLflow matches the database schema; started if needed), else the database itself."""
    uri = db_uri(cfg)
    img = mlflow_ui.image(cfg)
    return mlflow_ui.ensure(uri, img) if img else uri


def _what(which, name):
    return f"the synchronised database on {name}" if which == "sync" else f"the database of {name}"


def files_readable():
    """Whether the runs' files (artifacts) of the database read are reachable from here: not with a server's own."""
    return _STATE["which"] != "own"


def db_uri(cfg):
    """The tracking URI of the database chosen for the Results and MLflow tabs (checked: it answers)."""
    if _STATE["which"] == "local":
        uri = cfg["project"].get("tracking_uri") or ""
        if not uri:
            raise ValueError("set the MLflow tracking URI in Settings")
        _reachable(uri)
        return uri
    sv = _central(cfg) if _STATE["which"] == "sync" else remote.server(cfg, _STATE["server"])
    if sv["name"] != _STATE["server"] or not _alive(_STATE["port"]):
        what = _what(_STATE["which"], sv["name"])
        raise ValueError(f"the tunnel to {what} is closed (was the connection to {sv['name']} closed?): "
                         f"choose it again to reopen it, or Local")
    uri = _sync_uri(sv, _STATE["port"])
    _reachable(uri)
    return uri


def view(cfg):
    """{which: local | sync | own, server (own: whose database), central: name of the central server or None,
    servers: [the servers whose own database can be read], label: what is read, files: whether the runs' files can
    be opened from here}."""
    sv = core.central_server(cfg)
    if _STATE["which"] != "local":
        label = _what(_STATE["which"], _STATE["server"])
    else:
        label = "local: " + _shown(cfg["project"].get("tracking_uri") or "(no tracking URI)")
    return {"which": _STATE["which"], "server": _STATE["server"] if _STATE["which"] == "own" else None,
            "central": sv["name"] if sv else None, "servers": [s["name"] for s in core.active_servers(cfg)],
            "label": label, "files": files_readable()}


def _close(cfg):
    if _STATE["which"] != "local" and _STATE["server"]:
        sv = next((s for s in cfg["servers"] if s.get("name") == _STATE["server"]), None)
        if sv is not None and remote.connected(sv):
            _close_tunnel(sv, _STATE["port"], _STATE["which"])
    _STATE.update(which="local", server=None, port=None)


def _close_tunnel(sv, port, which="sync"):
    actions.run("view_close", sv["name"], f"{sv['name']}: close the tunnel of the Results and MLflow tabs",
                [actions.step(f"close the tunnel from 127.0.0.1:{port} to {_what(which, sv['name'])}",
                              _forward(sv, port, "cancel", which), timeout=20, always=True)])


def select(cfg, which, server=None):
    """Read the runs from `which` (local | sync | own, with the server whose database): opens the tunnel to that
    database (checked: its Postgres must answer), or closes it."""
    if which not in ("local", "sync", "own"):
        raise ValueError("which: local, sync or own")
    with _LOCK:
        if which == "local":
            _close(cfg)
            return view(cfg)
        if which == "sync":
            sv = _central(cfg)
            if not remote.connected(sv):
                raise ValueError(f"{sv['name']} (the central server) is not connected: click Connect in Servers first")
        else:
            sv = remote.server(cfg, str(server or ""))
            if not remote.connected(sv):
                raise ValueError(f"{sv['name']} is not connected: click Connect in Servers first")
        what = _what(which, sv["name"])
        if _STATE["which"] != "local" and _STATE["server"]:
            _close(cfg)                                     # a tunnel left open: replaced
        port = _free_port()

        def check():
            import psycopg2                                 # pylint: disable=import-outside-toplevel
            conn = psycopg2.connect(host="127.0.0.1", port=port, dbname=sync.DB_NAME, user=_user(sv), connect_timeout=15)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM experiments WHERE lifecycle_stage = 'active'")
                    return f"Postgres answers: {cur.fetchone()[0]} active experiments"
            finally:
                conn.close()
        res = actions.run("view_open", sv["name"], f"{sv['name']}: open a tunnel for the Results and MLflow tabs",
                          [actions.step(f"open a tunnel from 127.0.0.1:{port} to {what} (its Postgres socket, on the "
                                        "open ssh connection)", _forward(sv, port, "forward", which), timeout=20),
                           actions.step(f"check that {what} answers (read-only)", fn=check, timeout=30)])
        if not res["ok"]:
            bad = next((s for s in res["steps"] if s.get("rc")), {})
            _close_tunnel(sv, port, which)
            hint = " (is it started? see Databases)" if bad.get("label", "").startswith("check") else ""
            raise ValueError(f"could not read {what}{hint}: {(bad.get('err') or bad.get('out') or '').strip()[:400]}")
        _STATE.update(which=which, server=sv["name"], port=port)
        return view(cfg)
