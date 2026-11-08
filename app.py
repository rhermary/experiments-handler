#!/usr/bin/env python3
"""XP launcher: pick an XP, one option per block, the command loop and the variables;
the app writes <root>/scripts/<XP>/<timestamp>.sh and can run it.

    python app.py [--host 127.0.0.1] [--port 8000]

The project root (where the scripts run) is set in the page (Settings) or in experiments.yaml.
"""
import argparse
import re
import signal
import sys
import copy
import subprocess
import threading

import yaml
from flask import Flask, Response, jsonify, request, send_from_directory

import checker
import core
import actions
import diffview
import remote
import xpqueue
import gpus
import mlflow_ui
import picker
import preview
import results
import xpconfigs

LAUNCH_WAIT_S = 30  # how long a launch waits for the script before reporting "still running"
PAGES = {"index.html", "app.js", "editors.js", "argpicker.js", "gpus.js", "results.js", "router.js", "queue.js", "servers.js", "app.css", "favicon.svg", "apple-touch-icon.png"}

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False  # key order matters (order of variables, XPs, options)


@app.errorhandler(ValueError)
def bad_request(err):
    return jsonify(error=str(err)), 400


@app.errorhandler(yaml.YAMLError)
def bad_yaml(err):
    return jsonify(error=f"experiments.yaml: {err}"), 400


@app.errorhandler(OSError)
def os_error(err):
    return jsonify(error=str(err)), 400


def public(cfg):
    """The config for the page: each option also carries `full`, its variables with complete names."""
    out = copy.deepcopy(cfg)
    prefix = cfg["project"]["env_prefix"]
    for b in (b for x in out["xps"] for b in x["blocks"]):
        for r in b["rows"]:
            r["full"] = {core.full_name(b, k, prefix): v for k, v in r["vars"].items()}
            r["pretty"] = {k: preview.pretty(v) for k, v in r["vars"].items()}
    for g in out["variables"].values():
        for v in g:
            v["pretty"] = preview.pretty(v["value"])
    return out


def _rel(cfg, path):
    try:
        return str(path.relative_to(core.project_root(cfg)))
    except ValueError:
        return str(path)


@app.get("/")
def index():
    return send_from_directory(core.HERE, "index.html")


@app.get("/<name>")
def page_file(name):
    if name not in PAGES:
        return jsonify(error="not found"), 404
    return send_from_directory(core.HERE, name)


@app.get("/api/config")
def api_config():
    return jsonify(public(core.load()))


@app.put("/api/config")
def api_config_put():
    """Replace the config. It is validated first, so a bad edit never reaches the file."""
    return jsonify(public(core.save(request.get_json(force=True))))


@app.get("/api/template")
def api_template():
    return jsonify(text=core.read_template())


@app.put("/api/template")
def api_template_put():
    core.write_template(request.get_json(force=True)["text"])
    return jsonify(ok=True)


@app.post("/api/render")
def api_render():
    script, table, info = core.render(core.load(), request.get_json(force=True))
    return jsonify(script=script, vars=table, loop=info)


DRAFT = "(this option, not saved)"


def with_draft(cfg, body):
    """(cfg, selection, names of the draft's variables) with the option being edited in place of the selected one.

    body["draft"]: {block, row (name of the option being edited, or null for a new one), vars: [[key, value], ...]}."""
    sel, draft = copy.deepcopy(body.get("sel") or {}), body.get("draft")
    if not draft:
        return cfg, sel, None
    cfg, bid = copy.deepcopy(cfg), str(draft.get("block"))
    xp = core.xp_of(cfg, sel.get("xp"))
    b, prefix = core.block_of(xp, bid), cfg["project"]["env_prefix"]
    pairs = [(str(k).strip(), str(v)) for k, v in draft.get("vars") or [] if str(k).strip()]
    bad = [core.full_name(b, k, prefix) for k, _ in pairs if not core.NAME_RE.match(core.full_name(b, k, prefix))]
    if bad:
        raise ValueError(f"not a valid variable name: {bad[0]}")
    row = {"name": DRAFT, "vars": dict(pairs)}
    at = next((i for i, r in enumerate(b["rows"]) if r["name"] == draft.get("row")), None)
    if at is None:
        b["rows"].append(row)
    else:
        b["rows"][at] = row
    sel.setdefault("blocks", {})[bid] = DRAFT
    return cfg, sel, [core.full_name(b, k, prefix) for k, _ in pairs]


def _analysis_errors(fn):
    """A bug in the source analysis must show up in the page, not as an HTML error page."""
    try:
        return fn()
    except (ValueError, OSError, yaml.YAMLError):
        raise
    except Exception as err:  # pylint: disable=broad-except
        app.logger.exception("source analysis failed")
        return jsonify(error=f"argument check failed: {type(err).__name__}: {err}"), 500


@app.post("/api/diff")
def api_diff():
    """What changes between two selections of an XP: body {a: selection, b: selection}."""
    body = request.get_json(force=True)
    try:
        return jsonify(diffview.diff(core.load(), body["a"], body["b"]))
    except (ValueError, KeyError) as err:
        return jsonify(error=str(err)), 400


@app.post("/api/preview")
def api_preview():
    """Base config with the APP_* variables of the selection applied (errors are shown in the pane, not as failures)."""
    cfg = core.load()
    sel = request.get_json(force=True)
    table, _ = core.collect(cfg, sel)
    try:
        try:
            state = checker.analyse(cfg, sel)
            out = preview.build(cfg, table, state=state)
            out["checks"] = {n: {"level": r["level"], "notes": [x["text"] for x in r["notes"] if x["level"] != "info"]}
                             for n, r in state.results.items()}
            return jsonify(out)
        except ValueError:
            raise
        except Exception as err:  # pylint: disable=broad-except  # e.g. no source folder: preview without the checks
            app.logger.exception("argument check failed")
            return jsonify(preview.build(cfg, table, warn=[f"argument check unavailable: {err}"]))
    except (ValueError, OSError) as err:
        return jsonify(error=str(err), lines=[], warnings=[])


@app.post("/api/args")
def api_args():
    """One node of the program's argument tree, from the classes found in the source folder.

    Body: {sel: the page's selection, draft: the option being edited (optional), path: argument names from the root,
    fresh: scan the sources again}."""
    body = request.get_json(force=True)

    def run():
        cfg, sel, focus = with_draft(core.load(), body)
        state = checker.analyse(cfg, sel, focus, bool(body.get("fresh")))
        out = picker.browse(cfg, state, [str(n) for n in body.get("path") or []])
        out["checks"] = checker.report(state)
        out["draft_source"] = (f"{core.block_of(core.xp_of(cfg, sel['xp']), str(body['draft']['block']))['label']}: {DRAFT}"
                               if body.get("draft") else None)
        return jsonify(out)
    return _analysis_errors(run)


@app.post("/api/check")
def api_check():
    """What the program does with each variable of the draft option (or of the selection): body {sel, draft}."""
    body = request.get_json(force=True)

    def run():
        cfg, sel, focus = with_draft(core.load(), body)
        return jsonify(checker.report(checker.analyse(cfg, sel, focus)))
    return _analysis_errors(run)


@app.post("/api/set")
def api_set():
    """How to set a variable so that the program uses it: body {sel, draft, name, value} -> {set, remove, message}."""
    body = request.get_json(force=True)

    def run():
        cfg, sel, focus = with_draft(core.load(), body)
        return jsonify(checker.assign(checker.analyse(cfg, sel, focus), str(body["name"]), str(body["value"])))
    return _analysis_errors(run)


@app.post("/api/switch")
def api_switch():
    """How to give the node at `path` the class `cls`: body {sel, draft, path, cls} -> {set, remove, message}."""
    body = request.get_json(force=True)

    def run():
        cfg, sel, focus = with_draft(core.load(), body)
        state = checker.analyse(cfg, sel, focus)
        return jsonify(checker.switch(state, [str(n) for n in body.get("path") or []], str(body.get("cls") or "")))
    return _analysis_errors(run)


def _git(cfg, *args):
    r = subprocess.run(["git", "-C", str(core.project_root(cfg)), *args], capture_output=True, text=True, timeout=10,
                       check=False)
    if r.returncode != 0:
        raise ValueError(f"git {args[0]}: {r.stderr.strip() or 'failed'}")
    return r.stdout


@app.get("/api/commits")
def api_commits():
    """Latest commits of all local branches (the script copies .git into the container and checks the commit out)."""
    cfg = core.load()
    n = min(int(request.args.get("n", 60)), 500)
    out = _git(cfg, "log", "--all", f"-n{n}", "--date=format:%Y-%m-%d %H:%M",
               "--format=%H%x1f%h%x1f%ad%x1f%an%x1f%s%x1f%D")
    head = _git(cfg, "rev-parse", "HEAD").strip()
    dirty = bool(_git(cfg, "status", "--porcelain", "--untracked-files=no").strip())
    commits = []
    for line in out.splitlines():
        full, abbrev, date, author, subject, refs = (line.split("\x1f") + [""] * 6)[:6]
        commits.append({"hash": full, "short": abbrev, "date": date, "author": author, "subject": subject,
                        "refs": [r.strip().replace("HEAD -> ", "") for r in refs.split(",") if r.strip()],
                        "head": full == head})
    return jsonify(commits=commits, dirty=dirty)


@app.post("/api/files")
def api_files():
    """YAML files under configs/ as the program will see them: at the selected commit when the script checks it out
    (DEBUG=0), else in the working tree. Body: the selection."""
    cfg = core.load()
    table, _ = core.collect(cfg, request.get_json(force=True))
    folder = "configs"
    commit = (table.get(cfg["project"]["commit_var"]) or {}).get("value", "").strip()
    debug = (table.get("DEBUG") or {}).get("value", "0").strip()
    if commit and debug in ("0", "") and "$" not in commit:
        files = _git(cfg, "ls-tree", "-r", "--name-only", commit, "--", folder + "/").splitlines()
        where = f"commit {commit[:8]}"
    else:
        root = core.project_root(cfg)
        files = [str(f.relative_to(root)) for f in sorted((root / folder).rglob("*")) if f.is_file()]
        where = "working tree"
    files = sorted(f for f in files if f.endswith((".yaml", ".yml")))
    return jsonify(files=files, source=where)


@app.get("/api/gpus/servers")
def api_gpu_servers():
    """The servers of the config and which one the launcher runs on (?refresh=1 to look again)."""
    cfg = core.load()
    me = gpus.identify(core.active_servers(cfg), refresh=request.args.get("refresh") == "1")
    return jsonify(servers=core.active_servers(cfg), listed=cfg["servers"], current=me["server"], hostname=me["hostname"], how=me["how"],
                   local_gpus=me["gpus"])


@app.get("/api/gpus/all")
def api_gpus_all():
    """Every server of the config at once (in parallel), for the summary view."""
    return jsonify(gpus.state_all(core.active_servers(core.load())))


@app.get("/api/gpus")
def api_gpus():
    """GPUs of a server with the processes using them: ?server=NAME (default: the current one)."""
    try:
        return jsonify(gpus.state(core.active_servers(core.load()), request.args.get("server") or None))
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        return jsonify(error=str(err)), 400


@app.post("/api/yaml")
def api_yaml():
    """Block-YAML versions of values: body {values: [text, ...]} -> {values: [text, ...]}."""
    return jsonify(values=[preview.pretty(str(v)) for v in request.get_json(force=True).get("values") or []])


def _report_errors(fn):
    try:
        return fn()
    except (ValueError, OSError) as err:
        return jsonify(error=str(err)), 400
    except Exception as err:  # pylint: disable=broad-except  # MLflow / database errors: shown in the page
        app.logger.exception("report failed")
        return jsonify(error=f"{type(err).__name__}: {err}"), 400


@app.post("/api/report/preview")
def api_report_preview():
    """Runs of the XP labelled by its dimensions, with the summary, the LaTeX and the script (not run yet).
    Body {sel, dimensions (optional: try dimensions before saving them), fresh}."""
    body = request.get_json(force=True)

    def run():
        out = results.report_preview(core.load(), body["sel"], body.get("dimensions"), bool(body.get("fresh")))
        out["script"] = results.script_text(out["config"])
        return jsonify(out)
    return _report_errors(run)


@app.post("/api/report/run")
def api_report_run():
    """Run the report script of the XP (written from the saved settings) for table | plot | preview."""
    body = request.get_json(force=True)

    def run():
        what = str(body.get("what") or "")
        if what not in ("table", "plot", "preview") and not re.fullmatch(r"view:[a-z0-9_]+", what):
            raise ValueError("what: table, plot, preview or view:<id>")
        conf = results.report_preview(core.load(), body["sel"])["config"]
        text = results.script_text(conf)
        return jsonify(script=text, **results.run_script(text, what))
    return _report_errors(run)


@app.post("/api/report/curves")
def api_report_curves():
    """Data of a curves view (metric histories averaged per group): body {sel, view, fresh}."""
    body = request.get_json(force=True)
    return _report_errors(lambda: jsonify(results.curves_preview(core.load(), body["sel"], str(body.get("view") or ""),
                                                                 bool(body.get("fresh")))))


@app.post("/api/latex")
def api_latex():
    """SVG rendering of a LaTeX tabular: body {tex}."""
    try:
        return jsonify(svg=results.render_latex(str(request.get_json(force=True).get("tex") or "")))
    except (ValueError, OSError, subprocess.SubprocessError) as err:
        return jsonify(error=str(err)), 400


@app.post("/api/mlflow/start")
def api_mlflow_start():
    """Start (or reuse) the MLflow server for the project's tracking URI; the UI is then at /mlflow/."""
    cfg = core.load()
    uri = cfg["project"].get("tracking_uri") or ""
    if not uri:
        raise ValueError("set the MLflow tracking URI in Settings")
    return jsonify(mlflow_ui.start(uri))


@app.get("/api/mlflow/status")
def api_mlflow_status():
    return jsonify(mlflow_ui.status())


@app.route("/mlflow/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/mlflow/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def mlflow_proxy(path):
    """The MLflow UI and its API, forwarded to the server started by /api/mlflow/start."""
    body, code, headers = mlflow_ui.proxy(request, path)
    return Response(body, status=code, headers=headers)


@app.post("/api/xpconfig/create")
def api_xpconfig_create():
    """Write the generated base config of the selected XP: body {sel, source: empty | copy, from}."""
    body = request.get_json(force=True)
    rel, cfg = xpconfigs.create(core.load(), body["sel"], body.get("source", ""), body.get("from", ""))
    return jsonify(path=rel, config=public(core.save(cfg)))


@app.post("/api/xpconfig/read")
def api_xpconfig_read():
    rel = str(request.get_json(force=True).get("path") or "")
    return jsonify(path=rel, text=xpconfigs.read(core.load(), rel))


@app.post("/api/xpconfig/write")
def api_xpconfig_write():
    body = request.get_json(force=True)
    return jsonify(path=xpconfigs.write(core.load(), str(body.get("path") or ""), str(body.get("text") or "")))


@app.post("/api/xpconfig/set")
def api_xpconfig_set():
    """Write one field into the generated base config of the XP: body {sel, name (APP_* variable), value | unset}."""
    body = request.get_json(force=True)
    cfg = core.load()
    table, _ = core.collect(cfg, body["sel"])
    rel = (table.get(cfg["project"]["config_var"]) or {}).get("value", "").strip()
    if not xpconfigs.is_generated(cfg, rel):
        raise ValueError(f"the base config of this XP ({rel}) is not written by the launcher: use New... first")
    if body.get("unset"):
        xpconfigs.unset_value(cfg, rel, str(body["name"]))
    else:
        xpconfigs.set_value(cfg, rel, str(body["name"]), str(body["value"]))
    return jsonify(path=rel)


@app.get("/api/mlflow/experiments")
def api_mlflow_experiments():
    try:
        return jsonify(experiments=xpconfigs.experiments(core.load()))
    except Exception as err:  # pylint: disable=broad-except
        return jsonify(error=f"cannot list the MLflow experiments: {err}"), 400


@app.post("/api/import")
def api_import():
    """New XP from an MLflow experiment (or its new values added to the XP of that name): body {experiment, xp}."""
    body = request.get_json(force=True)
    try:
        cfg, summary = xpconfigs.import_xp(core.load(), str(body["experiment"]), (body.get("xp") or "").strip() or None)
    except ValueError:
        raise
    except Exception as err:  # pylint: disable=broad-except
        app.logger.exception("import failed")
        return jsonify(error=f"import failed: {type(err).__name__}: {err}"), 400
    return jsonify(summary=summary, config=public(core.save(cfg)))


@app.post("/api/blocks/merge")
def api_blocks_merge():
    body = request.get_json(force=True)
    cfg = xpconfigs.merge(core.load(), str(body["xp"]), str(body["first"]), str(body["second"]))
    return jsonify(config=public(core.save(cfg)))


@app.post("/api/generate")
def api_generate():
    cfg = core.load()
    return jsonify(path=_rel(cfg, core.generate(cfg, request.get_json(force=True))))


@app.post("/api/launch")
def api_launch():
    cfg = core.load()
    path = core.generate(cfg, request.get_json(force=True))
    proc = subprocess.Popen(
        ["bash", str(path)], cwd=core.project_root(cfg), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=LAUNCH_WAIT_S)
    except subprocess.TimeoutExpired:  # e.g. DEBUG=1 stays attached to the container
        threading.Thread(target=proc.communicate, daemon=True).start()  # keep draining the pipe
        return jsonify(path=_rel(cfg, path), running=True, pid=proc.pid, output="")
    return jsonify(path=_rel(cfg, path), running=False, returncode=proc.returncode, output=out)


# --------------------------------------------------------------------------- servers (remote launches and syncs)

_STATUS = {}                                                 # server name -> its state from the last "status" action


def _server_view(sv, cfg=None):
    return {**sv, "paths_all": core.server_paths(sv), "connected": remote.connected(sv), "status": _STATUS.get(sv["name"]),
            "db": _DB.get(sv["name"]), "image_default": _image(cfg or core.load(), sv)}


@app.get("/api/servers")
def api_servers():
    """The servers of the settings, their paths, whether a connection is open (asked to the local ssh only), and
    what the last status action read."""
    cfg = core.load()
    listed = [s for s in cfg["servers"] if "name" in s]
    return jsonify(servers=[_server_view(s, cfg) for s in listed], central=(core.central_server(cfg) or {}).get("name"),
                   path_keys=list(core.PATH_KEYS))


_DB = {}                                                     # server name -> its databases from the last "db_status"


def _image(cfg, sv=None):
    """The default image of a server's databases: its db_image, else the Docker image variable."""
    return ((core.server_paths(sv)["db_image"] if sv else "")
            or next((v["value"] for v in cfg["variables"]["docker"] if v["name"] == "IMAGE_NAME"), ""))


SERVER_ACTIONS = {
    "connect": ("open the connection", lambda cfg, sv, a: remote.connect_steps(sv)),
    "disconnect": ("close the connection", lambda cfg, sv, a: remote.disconnect_steps(sv)),
    "status": ("read the state", lambda cfg, sv, a: remote.status_steps(sv)),
    "db_status": ("read the databases", lambda cfg, sv, a: remote.db_status_steps(sv, _image(cfg, sv))),
    "db_start": ("start a database", lambda cfg, sv, a: remote.db_start_steps(sv, a.get("which", "own"), a.get("folder"), bool(a.get("allow_modif")),
                                                                         str(a.get("image") or "").strip() or _image(cfg, sv))),
    "db_pull": ("pull an image", lambda cfg, sv, a: remote.db_pull_steps(sv, str(a.get("image") or "").strip())),
    "db_stop": ("stop a database", lambda cfg, sv, a: remote.db_stop_steps(sv, a.get("which", "own"), a.get("container"))),
    "db_new": ("create a database folder", lambda cfg, sv, a: remote.db_new_steps(sv, a.get("which", "own"), __import__("datetime").date.today().strftime("%Y.%m.%d"))),
    "db_sizes": ("measure the database folders", lambda cfg, sv, a: remote.db_sizes_steps(sv, a.get("which", "own"))),
    "db_scripts": ("show the database scripts", lambda cfg, sv, a: remote.db_scripts_steps(sv)),
}


@app.post("/api/servers/run")
def api_servers_run():
    """Body {server, action, dry}: dry -> the commands it would run; else run them (logged) and return the result."""
    body = request.get_json(force=True)
    cfg = core.load()
    try:
        sv = remote.server(cfg, str(body.get("server")))
        what, build = SERVER_ACTIONS[str(body.get("action"))]
    except KeyError:
        return jsonify(error=f"unknown action '{body.get('action')}'"), 400
    except ValueError as err:
        return jsonify(error=str(err)), 400
    try:
        steps = build(cfg, sv, body.get("args") or {})
    except ValueError as err:
        return jsonify(error=str(err)), 400
    if body.get("dry"):
        return jsonify(plan=actions.plan(steps), title=f"{sv['name']}: {what}")
    if body["action"] != "connect" and not remote.connected(sv):
        return jsonify(error=f"{sv['name']} is not connected: click Connect first"), 400
    res = actions.run(body["action"], sv["name"], f"{sv['name']}: {what}", steps)
    if body["action"] == "status" and res["ok"]:
        _STATUS[sv["name"]] = {**remote.parse_status(res["steps"][0]["out"]), "when": res["when"]}
    if body["action"] == "db_status" and res["ok"]:
        _DB[sv["name"]] = {**remote.parse_db_status(sv, res["steps"][0]["out"]), "when": res["when"]}
    if body["action"] == "db_sizes" and res["ok"]:
        sizes = {ln.split("\t", 1)[1]: int(ln.split("\t", 1)[0]) for ln in res["steps"][0]["out"].splitlines() if "\t" in ln}
        _DB.setdefault(sv["name"], {}).setdefault("sizes", {}).update(sizes)
    if body["action"] == "disconnect":
        _STATUS.pop(sv["name"], None)
        _DB.pop(sv["name"], None)
    return jsonify(result={k: v for k, v in res.items() if k != "steps"}, steps=res["steps"], server=_server_view(sv, cfg))


@app.get("/api/actions")
def api_actions():
    """The action log, newest first (?server=, ?kind=)."""
    return jsonify(actions=actions.recent(200, request.args.get("server") or None, request.args.get("kind") or None))


@app.get("/api/actions/<action_id>")
def api_action(action_id):
    try:
        return jsonify(actions.read(action_id))
    except ValueError as err:
        return jsonify(error=str(err)), 404


@app.get("/api/queue")
def api_queue():
    """The queue of experiments (launched one after the other when their GPUs are free)."""
    xpqueue.start_thread()
    return jsonify(xpqueue.view(request.args.get("gpus") == "1"))


@app.post("/api/queue/add")
def api_queue_add():
    """Queue the selection: body {sel, mode: auto | fixed, count}. Its script is written now, the devices at start."""
    body = request.get_json(force=True)
    xpqueue.start_thread()
    try:
        item = xpqueue.add(core.load(), body["sel"], body.get("mode") or "auto", body.get("count"), bool(body.get("manual")))
    except (ValueError, KeyError) as err:
        return jsonify(error=str(err)), 400
    return jsonify(item={k: v for k, v in item.items() if k != "script"}, queue=xpqueue.view())


@app.post("/api/queue/act")
def api_queue_act():
    """Body {action: remove | up | down | retry | start | manual | auto | pause | resume | clear, id, devices}."""
    body = request.get_json(force=True)
    try:
        xpqueue.act(str(body.get("action")), body.get("id"), body.get("devices"))
    except ValueError as err:
        return jsonify(error=str(err)), 400
    return jsonify(xpqueue.view(True))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 only on a network you trust: the app runs shell scripts")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--config", help="the project's settings (experiments.yaml; created from an example when missing). "
                                     "Default: $XP_CONFIG, else ./experiments.yaml")
    ap.add_argument("--template", help="the script template. Default: $XP_TEMPLATE, else template.sh next to the settings, "
                                       "else the example shipped with the app")
    args = ap.parse_args()
    core.configure(args.config, args.template)
    print(f"settings: {core.CONFIG_PATH}\ntemplate: {core.template_path()}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # so that the MLflow server started here stops too
    xpqueue.start_thread()
    app.run(host=args.host, port=args.port, threaded=True)
