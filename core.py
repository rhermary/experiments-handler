"""XP launcher engine: config model (v3), script rendering, migration from the v1 and v2 configs.

Model
  project     root dir, scripts dir, src dir, env prefix/separator, name of the variable holding the base config
  variables   three groups of named variables, each with a default value and optional preset options
                docker   generic / docker side (COMMIT, CONFIG, IMAGE_NAME, POSTGRES_*, ...)
                run      run side (NB_RUNS, WINDOW_SIZES, ...)
                process  the program's own environment (APP_*, read by jsonargparse)
  loop        options: command loops (raw text pasted into the template at @@LOOP@@);
              env: variables set inside the loop at @@LOOP_ENV@@ (on top of the env variables whose value uses a loop
              variable such as $window, which always go there)
  xps         named experiments, each with its own ordered list of blocks (copying a block to another XP makes an
              independent copy). A block has an id (unique in its XP), a label, an optional `path` (an env-var
              prefix such as APP_TASK__INIT_ARGS__MODEL__INIT_ARGS__EXTRACTOR__INIT_ARGS__LLM__) and options; an
              option is a set of variables {suffix: value}. A key that does not start with the env prefix is
              appended to `path`.
"""
import copy
import os
import posixpath
import re
import shlex
import shutil
from datetime import datetime
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
# The settings of a project live in the project, not here: --config (or XP_CONFIG) names its experiments.yaml; the
# script template is --template (XP_TEMPLATE), else template.sh next to it, else the example shipped here. Files
# the app writes (backups, caches, logs) go next to the settings.
CONFIG_PATH = Path(os.environ.get("XP_CONFIG", "experiments.yaml")).expanduser().resolve()
TEMPLATE_PATH = Path(os.environ["XP_TEMPLATE"]).expanduser().resolve() if os.environ.get("XP_TEMPLATE") else None
DEFAULTS_PATH = HERE / "defaults.yaml"
EXAMPLE_TEMPLATE = HERE / "template.example.sh"


def configure(config=None, template=None):
    """Set the settings file and the script template (command line of app.py)."""
    global CONFIG_PATH, TEMPLATE_PATH  # pylint: disable=global-statement
    if config:
        CONFIG_PATH = Path(config).expanduser().resolve()
    if template:
        TEMPLATE_PATH = Path(template).expanduser().resolve()


def template_path():
    if TEMPLATE_PATH is not None:
        return TEMPLATE_PATH
    local = CONFIG_PATH.parent / "template.sh"
    return local if local.exists() else EXAMPLE_TEMPLATE


def state_path(name):
    """A file the app writes for itself (cache, log), next to the settings."""
    return CONFIG_PATH.parent / name

GROUPS = ("docker", "run", "process")
DEFAULT_SERVERS = []                    # GPU servers: listed in the settings (Servers... in the GPU view)
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOOP_REF = re.compile(r"\$\{?[a-z_]")      # $window, ${seed}: a loop variable, evaluated inside the container
LOOP_VAR = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
PLACEHOLDER = re.compile(r"@@(\w+)@@")
PROJECT_DEFAULTS = {"root": ".", "scripts_dir": "scripts", "src_dir": "src",
                    "env_prefix": "APP_", "env_sep": "__", "config_var": "CONFIG", "commit_var": "COMMIT",
                    "tracking_uri": "sqlite:///mlflow.db"}     # MLflow tracking URI (sqlite:///..., postgresql://..., http://...)

HEADER_COMMENT = """\
# XP launcher configuration (version 3). The page edits this file; the previous version is kept
# as experiments.yaml.bak. Hand edits are fine, use "Reload" in the page. Comments are not preserved on save.
#
# project     root: directory the scripts run from, scripts_dir, src_dir (scanned for classes),
#             env_prefix / env_sep (how config fields map to environment variables),
#             config_var (variable holding the base config, used for the preview and the argument browser)
# variables   docker / run / process: name, label, value, options, env (true = passed to the container with -e;
#             names starting with env_prefix are always passed)
# loop        options: command loops (name + shell text for @@LOOP@@); env: names of variables set inside the loop at
#             @@LOOP_ENV@@ (variables whose value uses a loop variable such as $window always are)
#             results (optional): metrics [{key, label}] shown in the Results tab, experiment (default: the value
#             of APP_SETTINGS__EXPERIMENT)
# servers     GPU servers for the GPU view (name, host, port, user); reached with ssh
# xps         name + its own ordered list of blocks: id, label, path (env-var prefix), rows;
#             a row sets variables {key: value}. Blocks are not shared between XPs.
#             kind: list -> var, items [{value, label}], vars (built from $VAR), loop: the options are one per value,
#             and "all (N)" that runs the command loop once per value (for var.lower() in values).
"""


def _text(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


# Where things are on a server. Defaults from one base folder; each can be changed per server (Settings > Servers).
PATH_KEYS = ("base", "db_pattern", "psql_sock", "pg_port", "artifacts", "checkpoints", "launcher",
             "synchronised", "sync_sock", "sync_port", "scripts", "empty_db", "db_image", "ui_port", "sync_ui_port")
DEFAULT_BASE = "/data/experiments"


def server_paths(sv):
    """Every path the launcher uses on a server: its own Postgres (data folders named <server>_<YYYY.MM.DD>_db,
    socket, port), artifacts, checkpoints, the launcher's folder (repo mirror, launches, queue), and, on the central
    server, the synchronised data (database folders db_<YYYY.MM.DD>, its own socket and port, artifacts,
    checkpoints)."""
    over = sv.get("paths") or {}
    base = over.get("base") or DEFAULT_BASE
    sync = over.get("synchronised") or f"{base}/synchronised"
    out = {"base": base, "db_pattern": f"{base}/{sv['name'].lower()}_*_db", "psql_sock": f"{base}/psql_sock",
           "pg_port": "5555", "artifacts": f"{base}/artifacts", "checkpoints": f"{base}/checkpoints",
           "launcher": f"{base}/launcher", "synchronised": sync, "sync_sock": f"{sync}/psql_sock", "sync_port": "5556",
           "scripts": f"{base}/scripts", "empty_db": f"{base}/_empty_db", "db_image": "", "ui_port": "5923",
           "sync_ui_port": "5924"}
    out.update({k: v for k, v in over.items() if v})
    out.update({"repo": f"{out['launcher']}/repo", "launches": f"{out['launcher']}/launches", "queue": f"{out['launcher']}/queue",
                "sync_db_pattern": f"{out['synchronised']}/db_*", "sync_artifacts": f"{out['synchronised']}/artifacts",
                "sync_checkpoints": f"{out['synchronised']}/checkpoints"})
    return out


def central_server(cfg):
    return next((s for s in cfg["servers"] if s.get("central") and "name" in s), None)


def active_servers(cfg):
    """The GPU servers to ask: not commented out in the list (# // or %), not comment lines."""
    return [s for s in cfg["servers"] if "name" in s and not s.get("disabled")]


def xp_dir(cfg, name):
    """Folder of an XP, relative to the project root: <scripts_dir>/<XP>. The launcher writes the XP's scripts there,
    and its base configs in <scripts_dir>/<XP>/configs (outside git)."""
    return posixpath.normpath(cfg["project"]["scripts_dir"]) + "/" + (re.sub(r"[^\w.\-]+", "_", name).strip("._") or "xp")


def gen_dir(cfg, name):
    return xp_dir(cfg, name) + "/configs"


def is_generated(cfg, path):
    """Whether `path` (relative to the project root) is a base config written by the launcher: <scripts_dir>/<XP>/configs/<file>."""
    p = posixpath.normpath(path or "")
    return ".." not in p.split("/") and bool(re.fullmatch(re.escape(posixpath.normpath(cfg["project"]["scripts_dir"])) + r"/[^/]+/configs/[^/]+", p))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "x"


# --------------------------------------------------------------------------- normalisation

def full_name(block, key, prefix):
    path = block.get("path") or ""
    return key if (not path or key.startswith(prefix)) else path + key


def normalize(raw):
    """Validate a raw config and return it in canonical form (plain strings, lists, dicts)."""
    raw = raw or {}
    p = {k: _text((raw.get("project") or {}).get(k, d)) for k, d in PROJECT_DEFAULTS.items()}
    for k in ("root", "scripts_dir", "env_prefix", "env_sep", "config_var", "commit_var"):
        if not p[k].strip():
            raise ValueError(f"project.{k} cannot be empty")
    prefix = p["env_prefix"]

    seen, variables = set(), {}
    for g in GROUPS:
        out = []
        for v in (raw.get("variables") or {}).get(g) or []:
            name = _text(v.get("name")).strip()
            if not NAME_RE.match(name):
                raise ValueError(f"variables.{g}: '{name}' is not a valid variable name")
            if name in seen:
                raise ValueError(f"variable '{name}' is defined twice")
            seen.add(name)
            out.append({"name": name, "label": _text(v.get("label")), "value": _text(v.get("value")),
                        "options": [_text(o) for o in v.get("options") or []], "env": bool(v.get("env"))})
        variables[g] = out

    raw_loop = raw.get("loop") or {}
    options = raw_loop.get("options")
    if options is None:                                     # before: generic `choices` with one LOOP slot
        legacy = raw.get("choices") or []
        other = [_text(c.get("slot")) for c in legacy if _text(c.get("slot")) != "LOOP"]
        if other:
            raise ValueError(f"choice slots other than LOOP are not supported any more: {', '.join(other)}")
        options = next((c.get("rows") for c in legacy), [])
    loops, names = [], set()
    for r in options or []:
        name = _text(r.get("name")).strip()
        if not name or name in names:
            raise ValueError("command loops: names must be non-empty and unique")
        names.add(name)
        loops.append({"name": name, "value": _text(r.get("value"))})
    if not loops:
        raise ValueError("at least one command loop is needed")
    loop_env = []
    for n in raw_loop.get("env") or []:
        n = _text(n).strip()
        if not NAME_RE.match(n):
            raise ValueError(f"loop.env: '{n}' is not a valid variable name")
        if n not in loop_env:
            loop_env.append(n)
    loop = {"options": loops, "env": loop_env}

    xps, xp_names = [], set()
    for x in raw.get("xps") or []:
        name = _text(x.get("name")).strip()
        if not name:
            raise ValueError("an XP has no name")
        if name in xp_names:
            raise ValueError(f"duplicate XP name '{name}'")
        xp_names.add(name)
        blocks, ids = [], set()
        for b in x.get("blocks") or []:
            if not isinstance(b, dict):
                raise ValueError(f"XP '{name}': a block must be a mapping (id, label, path, rows), got {b!r}")
            blocks.append(_block(b, name, ids, prefix))
        res = x.get("results") or {}
        results = {k: v for k, v in (("experiment", _text(res.get("experiment")).strip()),
                                     ("dimensions", _dimensions(res.get("dimensions"), name))) if v}
        if res.get("links") is not None:                # [] = no links (else: detected from the runs)
            results["links"] = _links(res["links"], name)
        # selections: what is shown (filter, values kept, rows/columns, metrics) and its views; the settings of
        # version 3 before selections (at the top of results) become the first one
        raw_sel = res.get("selections")
        if raw_sel is None and any(k in res for k in ("metrics", "filter", "layout", "views", "readme")):
            raw_sel = [{"id": "main", "title": "Main", **{k: res[k] for k in ("metrics", "filter", "layout", "views", "readme") if k in res}}]
        selections = _selections(raw_sel, name)
        if selections:
            results["selections"] = selections
        xps.append({"name": name, "blocks": blocks, **({"results": results} if results else {})})
    servers, seen = [], set()
    for sv in raw.get("servers") if raw.get("servers") is not None else DEFAULT_SERVERS:
        if sv.get("comment") is not None:                    # a comment line of the list, kept as written
            servers.append({"comment": _text(sv["comment"]).strip()})
            continue
        name, host = _text(sv.get("name")).strip(), _text(sv.get("host")).strip()
        if not name or not host or not re.fullmatch(r"[\w.\-]+", host):
            raise ValueError(f"servers: '{name}' needs a name and a host (name or address)")
        if name in seen:
            raise ValueError(f"servers: '{name}' is listed twice")
        seen.add(name)
        port = _text(sv.get("port") or 22).strip()
        if not port.isdigit():
            raise ValueError(f"servers: port of '{name}' must be a number")
        user = _text(sv.get("user")).strip()
        if user and not re.fullmatch(r"[\w.\-]+", user):
            raise ValueError(f"servers: user of '{name}' is not valid")
        paths = {}
        for k, v in (sv.get("paths") or {}).items():
            if k not in PATH_KEYS:
                raise ValueError(f"servers: '{name}' has an unknown path '{k}' (known: {', '.join(PATH_KEYS)})")
            v = _text(v).strip()
            if v and v != server_paths({"name": name}).get(k):     # only what differs from the defaults is kept
                if k.endswith("_port"):
                    if not v.isdigit():
                        raise ValueError(f"servers: {k} of '{name}' must be a number")
                elif not v.startswith("/") and k not in ("db_pattern", "db_image"):
                    raise ValueError(f"servers: path '{k}' of '{name}' must be absolute ({v})")
                paths[k] = v
        servers.append({"name": name, "host": host, "port": int(port), "user": user,
                        **({"disabled": True} if sv.get("disabled") else {}),
                        **({"central": True} if sv.get("central") else {}),
                        **({"paths": paths} if paths else {})})
    if sum(1 for s in servers if s.get("central")) > 1:
        raise ValueError("servers: only one server can be the central one (where the synchronised data lives)")
    return {"version": 3, "project": p, "variables": variables, "loop": loop, "xps": xps, "servers": servers}


def _dimensions(raw, xp):
    """Results dimensions: {name, values: [{label, rule}]} (rules: MLflow filters) or {name, param, labels}."""
    out, names = [], set()
    for d in raw or []:
        name = _text(d.get("name")).strip()
        if not name or name in names:
            raise ValueError(f"XP '{xp}', results: dimension names must be non-empty and unique ('{name}')")
        names.add(name)
        if d.get("param"):
            labels = {_text(k): _text(v) for k, v in (d.get("labels") or {}).items() if _text(v).strip()}
            out.append({"name": name, "param": _text(d["param"]).strip(), **({"labels": labels} if labels else {})})
            continue
        values, seen = [], set()
        for v in d.get("values") or []:
            label = _text(v.get("label")).strip()
            if not label or label in seen:
                raise ValueError(f"XP '{xp}', dimension '{name}': value labels must be non-empty and unique")
            seen.add(label)
            values.append({"label": label, "rule": _text(v.get("rule")).strip()})
        out.append({"name": name, "values": values, **({"block": _text(d["block"])} if d.get("block") else {})})
    return out


def _selections(raw, xp):
    """Results selections: [{id, title, filter, metrics, layout, views, readme}]."""
    out, ids = [], set()
    for s in raw or []:
        sid = slug(_text(s.get("id")) or _text(s.get("title")) or "selection")
        if sid in ids:
            raise ValueError(f"XP '{xp}', results selections: id '{sid}' is used twice")
        ids.add(sid)
        metrics = []
        for m in s.get("metrics") or []:
            key = _text(m.get("key") if isinstance(m, dict) else m).strip()
            if key:
                metrics.append({"key": key, "label": _text(m.get("label") if isinstance(m, dict) else "").strip() or key})
        item = {"id": sid, "title": _text(s.get("title")).strip() or sid}
        if s.get("filter") is not None:
            item["filter"] = _text(s.get("filter")).strip()
        for k, v in (("metrics", metrics), ("layout", _layout(s.get("layout"))), ("views", _views(s.get("views"), xp)),
                     ("readme", {_text(a): _text(b).strip() for a, b in (s.get("readme") or {}).items() if _text(b).strip()})):
            if v:
                item[k] = v
        out.append(item)
    return out


def _views(raw, xp):
    """Results views (more figures): [{id, type: curves, title, metrics, x, source, color, panel, smooth, std, logy}]."""
    out, ids = [], set()
    for v in raw or []:
        vid = slug(_text(v.get("id")) or _text(v.get("title")) or "view")
        if vid in ids:
            raise ValueError(f"XP '{xp}', results views: id '{vid}' is used twice")
        ids.add(vid)
        if _text(v.get("type")) != "curves":
            raise ValueError(f"XP '{xp}', results view '{vid}': unknown type '{v.get('type')}' (curves)")
        try:
            smooth = min(0.99, max(0.0, float(v.get("smooth") or 0)))
        except (TypeError, ValueError):
            smooth = 0.0
        out.append({"id": vid, "type": "curves", "title": _text(v.get("title")).strip() or vid,
                    "metrics": [_text(m).strip() for m in v.get("metrics") or [] if _text(m).strip()],
                    "x": "epoch" if v.get("x") == "epoch" else "step", "source": _text(v.get("source")).strip(),
                    "color": _text(v.get("color")).strip(), "panel": _text(v.get("panel")).strip(), "smooth": smooth,
                    "std": bool(v.get("std", True)), "logy": bool(v.get("logy", False))})
    return out


def _links(raw, xp):
    """Results links: [{name, param}], the parameter holding the MLflow run id of the run it uses (a checkpoint)."""
    out, names = [], set()
    for l in raw or []:
        name, param = _text(l.get("name")).strip(), _text(l.get("param")).strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", name) or name in names or name.lower() in ("params", "metrics", "tags", "attributes", "run"):
            raise ValueError(f"XP '{xp}', results links: '{name}' is not a valid link name (letters, digits, _; unique)")
        if not param:
            raise ValueError(f"XP '{xp}', results link '{name}': give the parameter that holds the run id")
        names.add(name)
        out.append({"name": name, "param": param})
    return out


def _layout(raw):
    if not raw:
        return {}
    return {"panel": _text(raw.get("panel")), "column": _text(raw.get("column")),
            "rows": [_text(r) for r in raw.get("rows") or [] if _text(r)],
            "hide": {_text(k): [_text(x) for x in v or []] for k, v in (raw.get("hide") or {}).items() if v},
            "scale": 100 if raw.get("scale", 100) == 100 else 1, "digits": max(0, min(4, int(raw.get("digits", 1)))),
            "std": bool(raw.get("std", True)), "missing": bool(raw.get("missing", True))}


ITEM_RE = re.compile(r"^[\w.:/@+=,-]+$")            # a list value: usable as a word of a shell for loop


def _list_block(b, block, where, prefix):
    """A list block: a variable (var) and its values (items: [{value, label}]), plus variables built from it (vars,
    using $VAR). Its options are generated: one per value, and "all (N)" (loop: true) that runs the command loop
    once per value (the variable becomes the shell loop variable var.lower())."""
    var = _text(b.get("var")).strip()
    if not NAME_RE.match(full_name(block, var, prefix) if var else ""):
        raise ValueError(f"{where}: the list needs a variable name (letters, digits, _)")
    items, seen = [], set()
    for it in b.get("items") or []:
        value = _text(it.get("value") if isinstance(it, dict) else it).strip()
        label = _text(it.get("label") if isinstance(it, dict) else "").strip()
        if not value:
            continue
        if not ITEM_RE.match(value):
            raise ValueError(f"{where}: '{value}' cannot be a list value (no spaces or quotes: letters, digits and ._:/@+=,-)")
        if (label or value) in seen:
            raise ValueError(f"{where}: '{label or value}' is in the list twice")
        seen.add(label or value)
        items.append({"value": value, **({"label": label} if label else {})})
    if not items:
        raise ValueError(f"{where}: the list is empty")
    templates = {}
    for k, val in (b.get("vars") or {}).items():
        k = _text(k).strip()
        if not NAME_RE.match(full_name(block, k, prefix)):
            raise ValueError(f"{where}: '{full_name(block, k, prefix)}' is not a valid variable name")
        templates[k] = _text(val)
    loop = bool(b.get("loop", True))
    lv = full_name(block, var, prefix).lower()
    block.update(kind="list", var=var, items=items, vars=templates, loop=loop)
    rows = [{"name": f"all ({len(items)})", "vars": {var: "${" + lv + "}", **templates}, "loop_all": True}] if loop and len(items) > 1 else []
    rows += [{"name": it.get("label") or it["value"], "vars": {var: it["value"], **templates}} for it in items]
    block["rows"] = rows
    return block


def _block(b, xp, ids, prefix):
    bid = _text(b.get("id")).strip() or slug(b.get("label") or "block")
    if bid in ids:
        raise ValueError(f"XP '{xp}': two blocks with the id '{bid}'")
    ids.add(bid)
    block = {"id": bid, "label": _text(b.get("label")) or bid, "path": _text(b.get("path")), "rows": []}
    where = f"XP '{xp}', block '{block['label']}'"
    if b.get("kind") == "list":
        return _list_block(b, block, where, prefix)
    names = set()
    for r in b.get("rows") or []:
        name = _text(r.get("name")).strip()
        if not name:
            raise ValueError(f"{where}: an option has no name")
        if name in names:
            raise ValueError(f"{where}: duplicate option '{name}'")
        names.add(name)
        vs = {}
        for k, val in (r.get("vars") or {}).items():
            k = _text(k).strip()
            if not NAME_RE.match(full_name(block, k, prefix)):
                raise ValueError(f"{where}, option '{name}': '{full_name(block, k, prefix)}' is not a valid variable name")
            vs[k] = _text(val)
        block["rows"].append({"name": name, "vars": vs})
    if not block["rows"]:
        raise ValueError(f"{where} needs at least one option")
    return block


def xp_of(cfg, name):
    xp = next((x for x in cfg["xps"] if x["name"] == name), None)
    if xp is None:
        raise ValueError(f"unknown XP '{name}'")
    return xp


def block_of(xp, bid):
    b = next((b for b in xp["blocks"] if b["id"] == bid), None)
    if b is None:
        raise ValueError(f"XP '{xp['name']}' has no block '{bid}' (reload?)")
    return b


# --------------------------------------------------------------------------- load / save

class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


_Dumper.add_representer(str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None))


def dump(cfg):
    # the options of a list block are made from its values when the file is read: not written
    cfg = {**cfg, "xps": [{**x, "blocks": [{k: v for k, v in b.items() if not (b.get("kind") == "list" and k == "rows")}
                                           for b in x["blocks"]]} for x in cfg["xps"]]}
    return HEADER_COMMENT + "\n" + yaml.dump(cfg, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)


def _write(path, text, backup=True):
    if backup and path.exists():
        shutil.copyfile(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def save(cfg, backup=True):
    cfg = normalize(cfg)
    _write(CONFIG_PATH, dump(cfg), backup)
    return cfg


def load():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULTS_PATH, CONFIG_PATH)
    raw = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    if raw.get("version") == 3:
        return normalize(raw)
    old = "v2" if raw.get("version") == 2 else "v1"
    shutil.copyfile(CONFIG_PATH, CONFIG_PATH.with_name(CONFIG_PATH.name + f".{old}.bak"))
    return save(migrate_v2(raw if old == "v2" else migrate_v1(raw)), backup=False)


def read_template():
    return template_path().read_text()


def write_template(text):
    if not text.strip():
        raise ValueError("the template is empty")
    # edits go to the project's template (never to the example shipped with the code)
    _write(TEMPLATE_PATH or CONFIG_PATH.parent / "template.sh", text.replace("\r\n", "\n"))


def project_root(cfg):
    """The project the scripts run in: project.root, relative to the settings file when it is not absolute."""
    root = Path(cfg["project"]["root"]).expanduser()
    return (root if root.is_absolute() else CONFIG_PATH.parent / root).resolve()


# --------------------------------------------------------------------------- rendering

def dq(value):
    """Escape for use between double quotes; `$` is kept so that shell variables expand."""
    return re.sub(r'([\\"`])', r"\\\1", value)


def outer_escape(s):
    """Escape a string that will sit inside CMD="...": every `$` is deferred to the container
    except references to script variables ($UPPER), which expand when the script builds CMD."""
    s = re.sub(r'([\\"`])', r"\\\1", s)
    return re.sub(r"\$(?!\{?[A-Z_])", r"\\$", s)


def pick(rows, name, what):
    if name is None:
        return rows[0]
    for r in rows:
        if r["name"] == name:
            return r
    raise ValueError(f"unknown option '{name}' in {what} (reload?)")


def collect(cfg, sel):
    """Resolve every variable for a selection: defaults < blocks of the XP (in order) < manual edits."""
    prefix = cfg["project"]["env_prefix"]
    xp = xp_of(cfg, sel.get("xp"))
    table = {}

    def put(name, value, group, env, source):
        old = table.get(name)
        if old:
            group, env = old["group"], old["env"]
        table[name] = {"name": name, "value": value, "group": group,
                       "env": bool(env) or name.startswith(prefix), "source": source}

    put("XP", xp["name"], "run", False, "XP name")
    for g in GROUPS:
        for v in cfg["variables"][g]:
            put(v["name"], v["value"], g, v["env"], "default")
    header = [f"# Generated by XP launcher on {datetime.now():%Y-%m-%d %H:%M:%S}", f"# XP: {xp['name']}"]
    chosen = sel.get("blocks") or {}
    for b in xp["blocks"]:
        row = list_row(b, chosen.get(b["id"])) if b.get("kind") == "list" else pick(b["rows"], chosen.get(b["id"]), b["label"])
        header.append(f"# {b['label']}: {row['name']}")
        for k, val in row["vars"].items():
            put(full_name(b, k, prefix), val, "entity", False, f"{b['label']}: {row['name']}")
    for name, val in (sel.get("values") or {}).items():
        if name in table:
            table[name].update(value=_text(val), source="edited")
    looped = set()
    for name, lv, items in list_loops(cfg, sel):              # "all (N)" of a list block: $VAR -> ${var} of the loop
        header.append(f"# {name}: looped over {len(items)} values (${{{lv}}})")
        ref = re.compile(r"\$(?:\{" + name + r"\}|" + name + r"(?![A-Za-z0-9_]))")
        for e in table.values():
            e["value"] = ref.sub("${" + lv + "}", e["value"])
        looped.add(name)
    for e in table.values():
        e["uses_loop"] = bool(LOOP_REF.search(e["value"]))
        e["loop"] = bool(e["env"] and e["uses_loop"]) or e["name"] in cfg["loop"]["env"] or e["name"] in looped
        e["scope"] = "loop" if e["loop"] else "container" if e["env"] else "script"
    return table, header


def list_row(b, choice):
    """The option of a list block for a selection: a row name ("all (N)" or one value), or a list of row names
    (selected values: one value as its row, several as a loop over them, like "all" but on those values only)."""
    if not isinstance(choice, list):
        return pick(b["rows"], choice, b["label"])
    names = [n for n in dict.fromkeys(choice) if any(r["name"] == n and not r.get("loop_all") for r in b["rows"])]
    if not names:
        raise ValueError(f"no value selected in the list {b['label']}")
    if len(names) == 1:
        return pick(b["rows"], names[0], b["label"])
    loop = next((r for r in b["rows"] if r.get("loop_all")), None)
    if loop is None:
        raise ValueError(f"the list {b['label']} does not loop: select one value")
    values = [it["value"] for it in b["items"] if (it.get("label") or it["value"]) in names]
    return {**loop, "name": f"{len(values)} selected", "selected": values}


def list_loops(cfg, sel):
    """[(variable, loop variable, values)] of the list blocks looped over: "all (N)" or several selected values
    (outer loop first)."""
    xp = xp_of(cfg, sel.get("xp"))
    prefix, out = cfg["project"]["env_prefix"], []
    chosen = sel.get("blocks") or {}
    for b in xp["blocks"]:
        if b.get("kind") != "list":
            continue
        row = list_row(b, chosen.get(b["id"]))
        if row.get("loop_all"):
            name = full_name(b, b["var"], prefix)
            out.append((name, name.lower(), row.get("selected") or [it["value"] for it in b["items"]]))
    return out


def _inject_loop_env(text, lines):
    out = []
    for ln in text.splitlines():
        if ln.strip() == "@@LOOP_ENV@@":
            indent = ln[: len(ln) - len(ln.lstrip())]
            if lines:
                out.append(indent + ("\n" + indent).join(lines))
        else:
            out.append(ln.replace("@@LOOP_ENV@@", " ".join(lines)))
    return "\n".join(out)


SHELL_REF = re.compile(r"\$(?:\{([A-Z_][A-Z0-9_]*)\}|([A-Z_][A-Z0-9_]*))")


def shell_expand(value, table, depth=4):
    """$NAME / ${NAME} replaced as the script's shell would: by the variable's value, or nothing when it is not set."""
    for _ in range(depth):
        new = SHELL_REF.sub(lambda m: (table.get(m.group(1) or m.group(2)) or {}).get("value", ""), value)
        if new == value:
            break
        value = new
    return value


def is_subconfig_var(cfg, name):
    return name.startswith(cfg["project"]["env_prefix"]) and name.split(cfg["project"]["env_sep"])[-1].upper().startswith("SUBCONFIG")


def empty_subconfigs(cfg, table):
    """..._SUBCONFIG variables that come out empty in the script ($SUBCONFIG not set for this XP): the program would
    fail to open them, so they are not passed."""
    return {n for n, e in table.items() if is_subconfig_var(cfg, n) and not shell_expand(e["value"], table).strip()}


def loop_vars(text):
    """Names the command loop defines (for window in ...)."""
    return list(dict.fromkeys(LOOP_VAR.findall(text)))


def render(cfg, sel):
    """(script, variables, loop info). Variables: script ones become shell assignments, container ones -e flags,
    loop ones (value uses a loop variable, or listed in loop.env) are set inside the loop at @@LOOP_ENV@@."""
    table, header = collect(cfg, sel)
    tpl = read_template()
    if "@@SCALARS@@" in tpl or "@@COLUMNS@@" in tpl:
        raise ValueError("template.sh is from the previous version: use the template.sh shipped with this version")
    shell, env, loop_env = [], [], []
    skip = empty_subconfigs(cfg, table)
    for name, e in table.items():
        if name in skip:
            header.append(f"# {name} not passed: it is empty for this XP")
            continue
        v = e["value"]
        if not e["env"] and not (e["loop"] and e["uses_loop"]):   # set inside the loop only (${window}, ${run_id})
            shell.append(f'{name}="{dq(v)}"')
        if e["loop"]:
            loop_env.append(outer_escape(f'{name}="{dq(v)}"') + " \\")
        elif e["env"]:
            env.append(f'    -e "{name}={dq(v)}"')
    slots = {"SHELL_VARS": "\n".join(shell), "CONTAINER_ENV": "CONTAINER_ENV=(\n" + "\n".join(env) + "\n)",
             "WORKDIR": shlex.quote(str(project_root(cfg)))}
    row = pick(cfg["loop"]["options"], sel.get("loop"), "command loops")
    header.append(f"# Command loop: {row['name']}")
    if loop_env and "@@LOOP_ENV@@" not in row["value"]:
        names = [e["name"] for e in table.values() if e["loop"]]
        raise ValueError(f"{', '.join(names)} must be set inside the loop but the command loop \"{row['name']}\" "
                         "has no @@LOOP_ENV@@ line")
    text = row["value"].strip("\n")
    shown = text.replace("\\$", "$")                      # as the container runs it (\$ only defers it to the container)
    lists = list_loops(cfg, sel)
    labels = {full_name(b, b["var"], cfg["project"]["env_prefix"]): b["label"]
              for b in xp_of(cfg, sel["xp"])["blocks"] if b.get("kind") == "list"}
    for name, lv, items in reversed(lists):                  # list blocks looped over: around the command loop
        ref = re.compile(r"\$(?:\{" + name + r"\}|" + name + r"(?![A-Za-z0-9_]))")
        text = ref.sub(lambda _m, lv=lv: "\\${" + lv + "}", text)
        shown = ref.sub(lambda _m, lv=lv: "${" + lv + "}", shown)
        head = f"for {lv} in {' '.join(items)}; do"
        text = head + "\n" + "\n".join("    " + ln for ln in text.splitlines()) + "\ndone"
        if len(items) > 2:                                    # shown: one value per line (the same shell command)
            head = f"for {lv} in \\\n" + " \\\n".join("        " + i for i in items) + "; do"
        shown = (f"# list \"{labels.get(name, name)}\": {len(items)} values\n" + head + "\n"
                 + "\n".join("    " + ln for ln in shown.splitlines()) + "\ndone")
    slots["LOOP"] = _inject_loop_env(text, loop_env)
    # the loop as it runs for this selection, readable (no escaping for CMD): for the page
    effective = _inject_loop_env(shown, [f'{n}="{e["value"]}" \\' for n, e in table.items()
                                         if e["loop"] and n not in skip])
    config = (table.get(cfg["project"]["config_var"]) or {}).get("value", "").strip()
    slots["COPY_FILES"] = ""
    if is_generated(cfg, config):
        # a base config written by the launcher is not in git: copy it in, and put it back after the checkout
        if "@@COPY_FILES@@" not in tpl:
            raise ValueError(f"{config} is written by the launcher: template.sh needs a line @@COPY_FILES@@ "
                             "after its docker cp lines (see the template shipped with the launcher)")
        gen = posixpath.dirname(posixpath.normpath(config))                      # scripts/<XP>/configs
        q, parent = shlex.quote(gen), shlex.quote(posixpath.dirname(gen))
        slots["COPY_FILES"] = f"docker cp {q} $CONTAINER_ID:/tmp/xp-configs"
        slots["LOOP"] = f"mkdir -p {parent} && rm -rf {q} && cp -r /tmp/xp-configs {q} && " + slots["LOOP"]
    defined = loop_vars(row["value"]) + [lv for _, lv, _ in list_loops(cfg, sel)]
    unknown = {}
    for e in table.values():
        for m in re.finditer(r"\$\{?([a-z_][a-z0-9_]*)", e["value"]):
            if e["loop"] and m.group(1) not in defined:
                unknown.setdefault(m.group(1), []).append(e["name"])
    slots["HEADER"] = "\n".join(header)
    for slot in ("SHELL_VARS", "CONTAINER_ENV", "LOOP"):
        if f"@@{slot}@@" not in tpl:
            raise ValueError(f"template.sh has no @@{slot}@@ placeholder")
    out = PLACEHOLDER.sub(lambda m: slots.get(m.group(1), m.group(0)), tpl)
    left = PLACEHOLDER.findall(out)
    if left:
        raise ValueError(f"unknown placeholder @@{left[0]}@@ in template.sh")
    info = {"loop": row["name"], "loop_vars": defined, "has_loop_env": "@@LOOP_ENV@@" in row["value"],
            "effective": effective,
            "lists": [{"var": n, "loop_var": lv, "values": len(items), "block": labels.get(n, n)} for n, lv, items in lists],
            "undefined": [{"var": k, "used_by": v} for k, v in unknown.items()]}
    return out, list(table.values()), info


def generate(cfg, sel):
    root = project_root(cfg)
    if not root.is_dir():
        raise ValueError(f"project root does not exist: {root}")
    script, _, _ = render(cfg, sel)
    folder = root / xp_dir(cfg, sel["xp"])
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path, n = folder / f"{stamp}.sh", 1
    while path.exists():
        n += 1
        path = folder / f"{stamp}_{n}.sh"
    path.write_text(script)
    path.chmod(0o755)
    return path


# --------------------------------------------------------------------------- migrations

def migrate_v2(old):
    """v2 (one bank of blocks, XPs list block ids) -> v3 (each XP has its own copy of its blocks).
    Blocks that no XP uses are kept in an XP named "unused blocks" so that nothing is lost."""
    bank = old.get("blocks") or {}
    new = {k: v for k, v in old.items() if k != "blocks"}
    new["version"], used, xps = 3, set(), []
    for x in old.get("xps") or []:
        blocks = []
        for bid in x.get("blocks") or []:
            bid = _text(bid)
            if bid not in bank:
                raise ValueError(f"XP '{x.get('name')}': unknown block '{bid}'")
            used.add(bid)
            blocks.append({"id": bid, **copy.deepcopy(bank[bid] or {})})
        xps.append({"name": x.get("name"), "blocks": blocks})
    rest = [bid for bid in bank if bid not in used]
    if rest:
        names = {_text(x.get("name")) for x in xps}
        name, i = "unused blocks", 2
        while name in names:
            name, i = f"unused blocks {i}", i + 1
        xps.append({"name": name, "blocks": [{"id": bid, **copy.deepcopy(bank[bid] or {})} for bid in rest]})
    new["xps"] = xps
    return new


V1_PROCESS = {"DEVICES": "APP_SETTINGS__DEVICES", "BATCH_SIZE": "APP_TASK__INIT_ARGS__BATCH_SIZE",
              "LR": "APP_TASK__INIT_ARGS__LR", "ACC_GRAD": "APP_TASK__INIT_ARGS__ACC_GRAD"}
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def parse_assignments(text):
    """NAME=value tokens of a v1 text (\\" quotes, ' quotes, ; separators) -> {NAME: value}."""
    out, i = {}, 0
    while True:
        m = _ASSIGN.search(text, i)
        if not m:
            return out
        name, i = m.group(0)[:-1], m.end()
        if text.startswith('\\"', i):
            j = text.find('\\"', i + 2)
            value, i = text[i + 2: j], j + 2
        elif text.startswith('"', i):
            j = text.find('"', i + 1)
            while j > 0 and text[j - 1] == "\\":
                j = text.find('"', j + 1)
            value, i = text[i + 1: j], j + 1
        elif text.startswith("'", i):
            j = text.find("'", i + 1)
            value, i = text[i + 1: j], j + 1
        else:
            m2 = re.compile(r"[^\s;\\]*").match(text, i)
            value, i = m2.group(0), m2.end()
        out[name] = re.sub(r"\\\n\s*", " ", value.replace('\\"', '"').replace("\\$", "$"))


def _convert_loop(text, known):
    lines, out, placed = text.strip("\n").splitlines(), [], False
    for ln in lines:
        s = ln.strip()
        lone_var = re.fullmatch(r"\$[A-Z_]+ \\", s)
        app = re.match(r"(APP_\w+)=", s)
        if lone_var or (app and app.group(1) in known and not re.search(r"\\\$[a-z]", s)):
            if not placed:
                out.append(ln[: len(ln) - len(ln.lstrip())] + "@@LOOP_ENV@@")
                placed = True
            continue
        out.append(ln)
    return "\n".join(out) + "\n"


def migrate_v1(old):
    new = yaml.safe_load(DEFAULTS_PATH.read_text())
    byname = {v["name"]: v for g in GROUPS for v in new["variables"][g]}
    for s in old.get("scalars") or []:
        name = V1_PROCESS.get(s["var"], s["var"])
        v = byname.get(name)
        if v is None:
            v = byname[name] = {"name": name, "label": "", "value": "", "options": [], "env": False}
            new["variables"]["run"].append(v)
        v["value"] = _text(s.get("default"))
        if s.get("options"):
            v["options"] = [_text(o) for o in s["options"]]
        if s.get("label"):
            v["label"] = s["label"]
    if old.get("workdir"):
        new["project"]["root"] = _text(old["workdir"])
    new["project"]["scripts_dir"] = _text(old.get("scripts_dir") or "scripts")

    def convert(cid, col):
        rows = []
        for r in col.get("rows") or []:
            vs = {}
            for text in (r["vars"].values() if "vars" in r else [r.get("value", "")]):
                vs.update(parse_assignments(_text(text)))
            rows.append({"name": _text(r["name"]), "vars": vs})
        return {"label": col.get("label", cid), "path": "", "rows": rows}

    blocks, xps = {}, []
    for cid, col in (old.get("shared_columns") or {}).items():
        blocks[cid] = convert(cid, col)
    for x in old.get("xps") or []:
        ids = []
        for c in x.get("columns") or []:
            if isinstance(c, str):
                ids.append(c)
                continue
            cid = slug(c.get("id") or c.get("var") or c.get("label"))
            bid = cid if cid not in blocks else f"{slug(x['name'])}_{cid}"
            blocks[bid] = convert(bid, c)
            ids.append(bid)
        xps.append({"name": _text(x["name"]), "blocks": ids})
    loops = new["choices"]
    for ch in old.get("choices") or []:
        if ch.get("slot") == "DATASET":
            blocks["dataset"] = {"label": ch.get("label", "Dataset"), "path": "",
                                 "rows": [{"name": _text(r["name"]), "vars": parse_assignments(_text(r.get("value")))} for r in ch["rows"]]}
            for x in xps:
                if "dataset" not in x["blocks"]:
                    x["blocks"].insert(0, "dataset")
        elif ch.get("slot") == "LOOP":
            have = {r["name"] for r in loops[0]["rows"]}
            known = set(byname)
            for r in ch["rows"]:
                if r["name"] not in have:
                    loops[0]["rows"].append({"name": _text(r["name"]), "value": _convert_loop(_text(r.get("value")), known)})
    new["blocks"], new["xps"], new["version"] = blocks, xps, 2
    return new
