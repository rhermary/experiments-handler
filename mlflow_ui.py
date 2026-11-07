"""The MLflow UI inside the launcher: an `mlflow server` started on demand on a local port, reached through the
launcher at /mlflow/ (same origin, so the page can show it in a frame and nothing else has to be reachable).
It stops with the launcher."""
import atexit
import os
import socket
import subprocess
import sys
import threading
import time

import requests

PREFIX = "/mlflow"
_LOCK = threading.Lock()
_STATE = {"proc": None, "port": None, "uri": None, "log": None, "started": None}
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


def status():
    """{state: off | starting | running | failed, uri, log (last lines when it failed)}."""
    if not _alive():
        if _STATE["proc"] is not None:
            return {"state": "failed", "uri": _STATE["uri"], "log": _tail()}
        return {"state": "off"}
    try:
        r = requests.get(f"http://127.0.0.1:{_STATE['port']}{PREFIX}/health", timeout=1)
        ok = r.status_code == 200
    except requests.RequestException:
        ok = False
    return {"state": "running" if ok else "starting", "uri": _STATE["uri"], "since": _STATE["started"]}


def _tail(n=15):
    try:
        return "\n".join(LOG_PATH().read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def start(uri):
    """Start the server for the tracking URI `uri` (restart when the URI changed)."""
    with _LOCK:
        if _alive() and _STATE["uri"] == uri:
            return status()
        stop()
        port = _free_port()
        log = open(LOG_PATH(), "w", encoding="utf8")          # pylint: disable=consider-using-with  # kept while it runs
        env = {**os.environ, "MLFLOW_DISABLE_AGENT_HINT": "1"}
        cmd = [sys.executable, "-m", "mlflow", "server", "--backend-store-uri", uri, "--host", "127.0.0.1",
               "--port", str(port), "--static-prefix", PREFIX, "--workers", "2", "--x-frame-options", "SAMEORIGIN"]
        _STATE.update(proc=subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True),
                      port=port, uri=uri, log=log, started=time.time())
    return status()


def stop():
    p = _STATE["proc"]
    if p is not None and p.poll() is None:
        try:
            os.killpg(p.pid, 15)
            p.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            p.kill()
    if _STATE["log"]:
        _STATE["log"].close()
    _STATE.update(proc=None, port=None, log=None)


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
    return r.raw.stream(65536, decode_content=True), r.status_code, out
