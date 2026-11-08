"""Sync the runs of a server's own database into the synchronised database of the central server, with their
artifacts and checkpoints. Nothing happens by itself: a preview (read-only) says what would be copied, then the sync
copies what you chose and checks it. Two separate syncs:
- results: the rows of the runs and their artifacts (config, figures, MLmodel: small);
- checkpoints: the checkpoint folders (large), of the runs already in the synchronised database.

The servers cannot reach each other: everything goes through this machine, over the ssh connections you opened.
- Databases: each Postgres socket is forwarded here (ssh -O forward -L <local socket>:<remote socket>, on the open
  connection), the rows are read from one and written into the other with psycopg2.
- Files: tar on the source server | tar on the central server (ssh to each), only the folders of the runs copied.

What is copied, per run: the run row, its params, metrics, latest metrics, tags, dataset inputs, and its model
versions (renumbered after the versions already there). Experiments are matched by name; an experiment missing from the
synchronised database is created there (with a new id: ids differ between databases, so the files are put under the
new id, and the runs point to them). A run already in the synchronised database is copied again only when it changed
(status, end time, params, tags or last metric values). Runs whose artifacts are not under the server's artifacts
folder (older paths) are left alone, and listed.
"""
import getpass
import shlex
import subprocess
import tempfile
from datetime import datetime

import actions
import core
import remote

DB_NAME = "mlruns_db"
PLANS = {}                                  # source server -> its last preview (shown in the Sync tab)
RUN_TABLES = ("params", "tags", "latest_metrics", "metrics")     # rows keyed by run_uuid, copied as they are
CHUNK = 400                                 # runs per COPY


# --------------------------------------------------------------------------- connections

def _tunnel(sv, which):
    """(local socket folder, local socket, remote socket, port) of a forwarded Postgres."""
    sock, port, _, _ = remote._db(sv, which)          # pylint: disable=protected-access
    folder = remote.CTL_DIR / f"pg-{core.slug(sv['name'])}-{which}"
    return folder, folder / f".s.PGSQL.{port}", f"{sock}/.s.PGSQL.{port}", port


def tunnel_steps(sv, which, what):
    folder, local, rem, _ = _tunnel(sv, which)
    base = ["ssh", "-o", f"ControlPath={remote.CTL_DIR}/%C"]
    spec = ["-L", f"{local}:{rem}", *remote._target(sv)]  # pylint: disable=protected-access
    prep = f"{shlex.join(base + ['-O', 'cancel'] + spec)} 2>/dev/null; mkdir -p -m 700 {shlex.quote(str(folder))} && rm -f {shlex.quote(str(local))}"
    return [actions.step(f"prepare the local end of the tunnel to {what} (a tunnel left open before is closed)", ["bash", "-c", prep], timeout=20),
            actions.step(f"open a tunnel to {what} (its Postgres socket, on the open ssh connection)", base + ["-O", "forward"] + spec, timeout=20)]


def close_steps(sv, which, what):
    _, local, rem, _ = _tunnel(sv, which)
    return [actions.step(f"close the tunnel to {what}",
                         ["ssh", "-o", f"ControlPath={remote.CTL_DIR}/%C", "-O", "cancel", "-L", f"{local}:{rem}", *remote._target(sv)],  # pylint: disable=protected-access
                         timeout=20, always=True)]


def connect(sv, which):
    import psycopg2                                         # pylint: disable=import-outside-toplevel
    folder, _, _, port = _tunnel(sv, which)
    return psycopg2.connect(host=str(folder), port=port, dbname=DB_NAME, user=sv.get("user") or getpass.getuser(), connect_timeout=15)


# --------------------------------------------------------------------------- reading

def _q(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _dicts(conn, sql, args=None):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _columns(conn, table):
    return [r[0] for r in _q(conn, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position", (table,))]


def _has(conn, table):
    return bool(_columns(conn, table))


def _version(conn):
    try:
        return _q(conn, "SELECT version_num FROM alembic_version")[0][0]
    except Exception:                                       # pylint: disable=broad-except
        conn.rollback()
        return None


def _digests(conn, runs):
    """run -> (params digest, tags digest, last metrics digest, dataset inputs): what a run changes when it goes on."""
    out = {r: ["", "", "", ""] for r in runs}
    if not runs:
        return {r: tuple(v) for r, v in out.items()}
    for i, (table, expr) in enumerate((("params", "key || '=' || value"), ("tags", "key || '=' || coalesce(value, '')"),
                                       ("latest_metrics", "key || '=' || value::text || '@' || step::text || '@' || timestamp::text"))):
        for run, dig in _q(conn, f"SELECT run_uuid, md5(string_agg({expr}, '|' ORDER BY key)) FROM {table} WHERE run_uuid = ANY(%s) GROUP BY run_uuid", (list(runs),)):
            out[run][i] = dig
    if _has(conn, "inputs"):
        for run, n in _q(conn, "SELECT destination_id, count(*) FROM inputs WHERE destination_type = 'RUN' AND destination_id = ANY(%s) GROUP BY destination_id", (list(runs),)):
            out[run][3] = str(n)
    return {r: tuple(v) for r, v in out.items()}


def _local(uri):
    return (uri or "")[7:] if (uri or "").startswith("file://") else (uri or "")


def compute(src, dst, sp, dp):
    """What a sync would do. src/dst: connections; sp/dp: paths of the source server and of the central one."""
    v_src, v_dst = _version(src), _version(dst)
    blocking = []
    if v_src != v_dst:
        blocking.append(f"the databases have different MLflow schemas ({v_src} here, {v_dst} in the synchronised one): "
                        "upgrade the older one first (mlflow db upgrade), nothing is copied until they match")
    sx = {str(e["experiment_id"]): e for e in _dicts(src, "SELECT * FROM experiments")}
    dx = {e["name"]: e for e in _dicts(dst, "SELECT * FROM experiments")}
    runs = _dicts(src, "SELECT run_uuid, experiment_id, status, end_time, lifecycle_stage, artifact_uri FROM runs")
    there = {r["run_uuid"]: r for r in _dicts(dst, "SELECT run_uuid, status, end_time, lifecycle_stage FROM runs WHERE run_uuid = ANY(%s)", ([r["run_uuid"] for r in runs],))}
    d_src = _digests(src, [r["run_uuid"] for r in runs if r["run_uuid"] in there])
    d_dst = _digests(dst, list(there))
    root = sp["artifacts"].rstrip("/") + "/"
    exps = {}
    for r in runs:
        e = sx.get(str(r["experiment_id"]), {})
        name = e.get("name", f"? (id {r['experiment_id']})")
        x = exps.setdefault(name, {"name": name, "src_id": str(r["experiment_id"]), "dst_id": str(dx[name]["experiment_id"]) if name in dx else None,
                                   "deleted": e.get("lifecycle_stage") == "deleted", "new": [], "changed": [], "synced": 0, "synced_runs": [], "old_path": [], "bytes": 0, "files": 0})
        uri = _local(r["artifact_uri"])
        rid = r["run_uuid"]
        if not uri.startswith(root + f"{r['experiment_id']}/{rid}/"):
            x["old_path"].append({"run": rid, "artifact_uri": r["artifact_uri"]})
            continue
        if rid not in there:
            x["new"].append(rid)
            continue
        t = there[rid]
        if (t["status"], t["end_time"], t["lifecycle_stage"]) != (r["status"], r["end_time"], r["lifecycle_stage"]) or d_src.get(rid) != d_dst.get(rid):
            x["changed"].append(rid)
        else:
            x["synced"] += 1
            x["synced_runs"].append(rid)
    extra = {}
    for table, col in (("logged_models", "source_run_id"), ("trace_info", None)):
        if _has(src, table) and col:
            n = _q(src, f"SELECT count(*) FROM {table} WHERE {col} IS NOT NULL")[0][0]
            if n:
                extra[table] = n
    return {"schema": v_src, "schema_dst": v_dst, "blocking": blocking, "experiments": sorted(exps.values(), key=lambda x: x["name"]),
            "not_copied": extra, "src_artifacts": sp["artifacts"], "src_checkpoints": sp["checkpoints"],
            "dst_artifacts": dp["sync_artifacts"], "dst_checkpoints": dp["sync_checkpoints"]}


SIZE_SCRIPT = """# for each run folder (kind, path relative to its root), on stdin: whether it exists, its files and bytes
# (C locale and %.0f: awk would print a large total as 2,15047e+09)
export LC_ALL=C
while read -r kind rel; do
  case "$kind" in a) d="$A/$rel" ;; c) d="$C/$rel" ;; esac
  if [ -d "$d" ]; then find "$d" -type f ! -name MLmodel -printf '%s\\n' | awk -v k="$kind" -v r="$rel" '{n++; s+=$1} END {printf "%s %s %d %.0f\\n", k, r, n, s}'; else echo "$kind $rel -"; fi
done"""


def _sizes(sv, a_root, c_root, items):
    """{(kind, rel): (files, bytes) or None} on a server, for run folders (kind a: artifacts, c: checkpoints)."""
    if not items:
        return {}, ""
    script = f"A={shlex.quote(a_root)}; C={shlex.quote(c_root)}\n" + SIZE_SCRIPT
    argv = remote.ssh_argv(sv, "bash -c " + shlex.quote(script))
    r = subprocess.run(argv, input="".join(f"{k} {rel}\n" for k, rel in items), capture_output=True, text=True, timeout=1800, check=False)
    if r.returncode:
        raise RuntimeError(f"measuring the run folders on {sv['name']} failed: {r.stderr.strip()}")
    out = {}
    for ln in r.stdout.splitlines():
        p = ln.split()
        if len(p) == 4:
            out[(p[0], p[1])] = (int(p[2]), int(float(p[3].replace(',', '.'))))
        elif len(p) == 3:
            out[(p[0], p[1])] = None
    return out, shlex.join(argv) + " <<'EOF'\n" + "\n".join(f"{k} {rel}" for k, rel in items[:5]) + (f"\n... ({len(items)} folders)" if len(items) > 5 else "") + "\nEOF"


# --------------------------------------------------------------------------- preview

def preview_steps(src_sv, dst_sv):
    if not dst_sv:
        raise ValueError("no central server: choose one (Servers > Edit servers)")
    sp, dp = core.server_paths(src_sv), core.server_paths(dst_sv)
    s_what, d_what = f"the database of {src_sv['name']}", f"the synchronised database on {dst_sv['name']}"

    def read():
        with connect(src_sv, "own") as src, connect(dst_sv, "sync") as dst:
            plan = compute(src, dst, sp, dp)
            dst_dir = _q(dst, "SHOW data_directory")[0][0]
        ex = plan["experiments"]
        # results: the artifact folders of the runs to copy; checkpoints: every run's folder, here and (runs already
        # synchronised) on the central server
        items = [("a", f"{x['src_id']}/{r}") for x in ex for r in x["new"] + x["changed"]]
        items += [("c", f"{x['src_id']}/{r}") for x in ex for r in x["new"] + x["changed"] + x["synced_runs"]]
        sizes, _ = _sizes(src_sv, sp["artifacts"], sp["checkpoints"], items)
        there = [("c", f"{x['dst_id']}/{r}") for x in ex if x["dst_id"] for r in x["changed"] + x["synced_runs"]]
        sizes_c, _ = _sizes(dst_sv, dp["sync_artifacts"], dp["sync_checkpoints"], there)
        for x in ex:
            x["folders"], x["ckpt"], x["ckpt_wait"], x["ckpt_done"] = {}, {}, 0, 0
            for r in x["new"] + x["changed"]:
                v = sizes.get(("a", f"{x['src_id']}/{r}"))
                if v:
                    x["folders"][r] = {"a": v}
                    x["files"] += v[0]
                    x["bytes"] += v[1]
            for r in x["new"] + x["changed"] + x["synced_runs"]:
                v = sizes.get(("c", f"{x['src_id']}/{r}"))
                if not v:
                    continue
                if r in x["new"]:
                    x["ckpt_wait"] += 1                    # its run is not in the synchronised database yet
                elif sizes_c.get(("c", f"{x['dst_id']}/{r}")) == v:
                    x["ckpt_done"] += 1
                else:
                    x["ckpt"][r] = v
        plan.update(source=src_sv["name"], central=dst_sv["name"], dst_folder=dst_dir, when=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        PLANS[src_sv["name"]] = plan
        lines = [f"synchronised database: {dst_dir}", f"MLflow schema: {plan['schema']} / {plan['schema_dst']}", *plan["blocking"]]
        for x in plan["experiments"]:
            lines.append(f"{x['name']}: results: {len(x['new'])} new, {len(x['changed'])} changed, {x['synced']} already synced, "
                         f"{len(x['old_path'])} on older paths (left alone), artifacts {x['files']} files {x['bytes'] / 1e9:.2f} GB"
                         + ("" if x["dst_id"] else " (experiment created in the synchronised database)")
                         + f"; checkpoints: {len(x['ckpt'])} to copy ({sum(v[1] for v in x['ckpt'].values()) / 1e9:.2f} GB), "
                         f"{x['ckpt_wait']} waiting for their results, {x['ckpt_done']} already there")
        return "\n".join(lines) + "\n"

    shown = ("read-only, through the two tunnels:\n"
             f"  {s_what}: experiments, runs (id, experiment, status, end time, artifact uri), and per run a digest of its params, tags and last metrics\n"
             f"  {d_what}: the same, for the runs found in both\n"
             f"then on {src_sv['name']}, the files and bytes of {sp['artifacts']}/<experiment id>/<run> (runs to copy) and {sp['checkpoints']}/<experiment id>/<run> (all runs),\n"
             f"and on {dst_sv['name']}, of {dp['sync_checkpoints']}/<experiment id>/<run> (runs already synchronised):\n"
             + shlex.join(remote.ssh_argv(src_sv, "bash -c <script>")) + "  # script:\n" + SIZE_SCRIPT)
    return [*tunnel_steps(src_sv, "own", s_what), *tunnel_steps(dst_sv, "sync", d_what),
            actions.step("read both databases and measure the run folders (read-only)", fn=read, display=shown, timeout=1800),
            *close_steps(src_sv, "own", s_what), *close_steps(dst_sv, "sync", d_what)]


# --------------------------------------------------------------------------- copy

def _copy_rows(src, dst, table, cols, runs, key="run_uuid"):
    """COPY the rows of `runs` from src to dst (same columns), in chunks. Returns the number of rows."""
    n, cl = 0, ", ".join(cols)
    for i in range(0, len(runs), CHUNK):
        part = runs[i:i + CHUNK]
        with src.cursor() as cur:
            sel = cur.mogrify(f"SELECT {cl} FROM {table} WHERE {key} = ANY(%s)", (part,)).decode()
        with tempfile.SpooledTemporaryFile(max_size=64 << 20, mode="w+b") as buf:
            with src.cursor() as cur:
                cur.copy_expert(f"COPY ({sel}) TO STDOUT", buf)
            buf.seek(0)
            with dst.cursor() as cur:
                cur.copy_expert(f"COPY {table} ({cl}) FROM STDIN", buf)
                n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return n


def _insert(conn, table, row):
    cols = list(row)
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) ON CONFLICT DO NOTHING", [row[c] for c in cols])
        return cur.rowcount


def _common(src, dst, table):
    d = set(_columns(dst, table))
    return [c for c in _columns(src, table) if c in d]


def _rebase(path, old, new):
    """`path` under `old` (a run's artifact folder): the same under `new`; else unchanged."""
    p = _local(path)
    return new + p[len(old):] if old and p.startswith(old) else path


def _files_cmd(src_sv, dst_sv, kind, src_id, d_id, runs):
    """Copy of run folders of one experiment and one kind (a: artifacts, c: checkpoints), renamed under the experiment
    id of the synchronised database: (bash pipeline, its stdin: the folders)."""
    sp, dp = core.server_paths(src_sv), core.server_paths(dst_sv)
    s_root, d_root = (sp["artifacts"], dp["sync_artifacts"]) if kind == "a" else (sp["checkpoints"], dp["sync_checkpoints"])
    q = shlex.quote
    unpack = f"mkdir -p {q(d_root)} && tar -C {q(d_root)} -xf - --transform=s,^{src_id}/,{d_id}/,"
    pack = f"tar -C {q(s_root)} -cf - -T -"
    ssh = lambda sv: shlex.join(remote.ssh_argv(sv))     # noqa: E731  (the remote commands below: paths and tar only)
    if src_sv["name"] == dst_sv["name"]:
        cmd = f'{ssh(src_sv)} "{pack} | {unpack}"'
    else:
        cmd = f'{ssh(src_sv)} "{pack}" | {ssh(dst_sv)} "{unpack}"'
    return "set -o pipefail; " + cmd, "".join(f"{src_id}/{r}\n" for r in runs)


def _copy_step(src_sv, dst_sv, kind, name, src_id, get_id, get_runs, n, size):
    """A step copying run folders (get_id, get_runs: resolved when it runs: the experiment id may be created by then)."""
    what = "artifacts" if kind == "a" else "checkpoints"

    def go():
        runs = get_runs()
        if not runs:
            return f"no {what} folder to copy\n"
        cmd, stdin = _files_cmd(src_sv, dst_sv, kind, src_id, get_id(), runs)
        r = subprocess.run(["bash", "-c", cmd], input=stdin, capture_output=True, text=True, timeout=24 * 3600, check=False)
        if r.returncode:
            raise RuntimeError(f"copy failed (exit {r.returncode}): {r.stderr.strip()}")
        return f"$ {cmd} <<'EOF'\n{stdin}EOF\n{r.stdout}{r.stderr}copied {len(runs)} folder(s)\n"
    runs = get_runs()
    cmd, stdin = _files_cmd(src_sv, dst_sv, kind, src_id, get_id() or "<new id>", runs) if runs else (None, None)
    shown = (f"{cmd} <<'EOF'\n{stdin}EOF" if cmd else f"no {what} folder to copy") + \
            ("\n# <new id>: the id the experiment gets in the synchronised database (step above)" if cmd and "<new id>" in cmd else "")
    return actions.step(f"copy the {what} of {name} ({n} run folders, {size / 1e9:.2f} GB) to {dst_sv['name']}", fn=go, display=shown, timeout=24 * 3600)


def _check_folders(src_sv, dst_sv, groups):
    """groups: [(name, kind, src_id, d_id, runs)]: the folders on both servers must have the same files and bytes."""
    sp, dp = core.server_paths(src_sv), core.server_paths(dst_sv)
    out, bad = [], []
    for name, kind, src_id, d_id, runs in groups:
        if not runs:
            continue
        s, _ = _sizes(src_sv, sp["artifacts"], sp["checkpoints"], [(kind, f"{src_id}/{r}") for r in runs])
        d, _ = _sizes(dst_sv, dp["sync_artifacts"], dp["sync_checkpoints"], [(kind, f"{d_id}/{r}") for r in runs])
        what = "artifacts" if kind == "a" else "checkpoints"
        for r in runs:
            a, b = s.get((kind, f"{src_id}/{r}")), d.get((kind, f"{d_id}/{r}"))
            if a != b:
                bad.append(f"{name} {what} {src_id}/{r}: {a} on {src_sv['name']}, {b} on {dst_sv['name']} (files, bytes)")
        out.append(f"{name}: {len(runs)} {what} folders identical (files and bytes{', MLmodel aside' if kind == 'a' else ''})")
    if bad:
        raise RuntimeError("\n".join(bad))
    return "\n".join(out) + "\n"


def sync_steps(src_sv, dst_sv, names):
    plan = PLANS.get(src_sv["name"])
    if not plan:
        raise ValueError(f"preview the sync of {src_sv['name']} first")
    if plan["central"] != (dst_sv or {}).get("name"):
        raise ValueError("the central server changed since the preview: preview again")
    if plan["blocking"]:
        raise ValueError(plan["blocking"][0])
    chosen = [x for x in plan["experiments"] if x["name"] in set(names or []) and (x["new"] or x["changed"])]
    if not chosen:
        raise ValueError("nothing to copy in the experiments chosen")
    sp, dp = core.server_paths(src_sv), core.server_paths(dst_sv)
    s_what, d_what = f"the database of {src_sv['name']}", f"the synchronised database on {dst_sv['name']}"
    ctx = {"exp": {x["name"]: x["dst_id"] for x in chosen}}

    def experiments():
        """Check the preview still holds; create the experiments missing from the synchronised database."""
        out = []
        with connect(src_sv, "own") as src, connect(dst_sv, "sync") as dst:
            now = {x["name"]: x for x in compute(src, dst, sp, dp)["experiments"]}
            for x in chosen:
                y = now.get(x["name"])
                todo = set(y["new"] + y["changed"]) if y else set()
                gone = [r for r in x["new"] + x["changed"] if r not in todo]
                if gone:
                    out.append(f"{x['name']}: {len(gone)} run(s) of the preview are already synchronised now: skipped")
                x["runs"] = [r for r in x["new"] + x["changed"] if r in todo]
                x["changed_now"] = [r for r in x["changed"] if r in todo]
                found = _q(dst, "SELECT experiment_id FROM experiments WHERE name = %s", (x["name"],))
                if found:
                    ctx["exp"][x["name"]] = str(found[0][0])
                    continue
                e = _dicts(src, "SELECT * FROM experiments WHERE experiment_id = %s", (int(x["src_id"]),))[0]
                cols = [c for c in _common(src, dst, "experiments") if c != "experiment_id"]
                with dst.cursor() as cur:
                    cur.execute(f"INSERT INTO experiments ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING experiment_id", [e[c] for c in cols])
                    new_id = str(cur.fetchone()[0])
                    cur.execute("UPDATE experiments SET artifact_location = %s WHERE experiment_id = %s", (f"{dp['sync_artifacts']}/{new_id}", int(new_id)))
                tag_cols = _common(src, dst, "experiment_tags")
                for t in _dicts(src, f"SELECT {', '.join(tag_cols)} FROM experiment_tags WHERE experiment_id = %s", (int(x["src_id"]),)):
                    _insert(dst, "experiment_tags", {**t, "experiment_id": int(new_id)})
                ctx["exp"][x["name"]] = new_id
                out.append(f"{x['name']}: created in the synchronised database with id {new_id} (id {x['src_id']} on {src_sv['name']})")
            dst.commit()
        for x in chosen:
            out.append(f"{x['name']}: {len(x['runs'])} run(s) to copy, experiment id {x['src_id']} -> {ctx['exp'][x['name']]}")
        return "\n".join(out) + "\n"

    def mlmodel_script(x):
        """MLmodel names its checkpoint by a path under the experiment id: on the central server, the new id."""
        d_id = ctx["exp"].get(x["name"]) or "<new id>"
        runs = [r for r in x.get("runs", x["new"] + x["changed"]) if "a" in x["folders"].get(r, {})]
        root = dp["sync_artifacts"]
        return "\n".join([f"cd {shlex.quote(root)}/{d_id}", f"for r in {' '.join(runs)}; do",
                          f"  [ -f \"$r/artifacts/MLmodel\" ] && sed -i \"s,checkpoints/{x['src_id']}/$r/,checkpoints/{d_id}/$r/,\" \"$r/artifacts/MLmodel\"",
                          "done", "exit 0"])

    def mlmodel_step(x):
        def go():
            s = mlmodel_script(x)
            r = subprocess.run(remote.ssh_argv(dst_sv, "bash -s"), input=s + "\n", capture_output=True, text=True, timeout=600, check=False)
            if r.returncode:
                raise RuntimeError(r.stderr.strip())
            return f"$ {shlex.join(remote.ssh_argv(dst_sv, 'bash -s'))} <<'EOF'\n{s}\nEOF\n{r.stdout}"
        return actions.step(f"point the MLmodel files of {x['name']} to the checkpoints' new folder (experiment id {x['src_id']} -> {x['dst_id'] or 'new id'})",
                            fn=go, display=f"{shlex.join(remote.ssh_argv(dst_sv, 'bash -s'))} <<'EOF'\n{mlmodel_script(x)}\nEOF", timeout=600)

    def rows():
        out = []
        with connect(src_sv, "own") as src, connect(dst_sv, "sync") as dst:
            run_cols = _common(src, dst, "runs")
            tcols = {t: _common(src, dst, t) for t in RUN_TABLES}
            has_inputs = all(_has(c, t) for c in (src, dst) for t in ("inputs", "datasets"))
            has_registry = all(_has(c, "model_versions") for c in (src, dst))
            for x in chosen:
                runs, d_id = x["runs"], int(ctx["exp"][x["name"]])
                if not runs:
                    continue
                if x["changed_now"]:                         # copied again: their old rows go first
                    with dst.cursor() as cur:
                        for t in RUN_TABLES:
                            cur.execute(f"DELETE FROM {t} WHERE run_uuid = ANY(%s)", (x["changed_now"],))
                        if has_inputs:
                            cur.execute("DELETE FROM input_tags WHERE input_uuid IN (SELECT input_uuid FROM inputs WHERE destination_type = 'RUN' AND destination_id = ANY(%s))", (x["changed_now"],))
                            cur.execute("DELETE FROM inputs WHERE destination_type = 'RUN' AND destination_id = ANY(%s)", (x["changed_now"],))
                        cur.execute("DELETE FROM runs WHERE run_uuid = ANY(%s)", (x["changed_now"],))
                for r in _dicts(src, f"SELECT {', '.join(run_cols)} FROM runs WHERE run_uuid = ANY(%s)", (runs,)):
                    old = _local(r["artifact_uri"])
                    r["artifact_uri"] = f"{dp['sync_artifacts']}/{d_id}/{old[len(sp['artifacts'].rstrip('/')) + 1:].split('/', 1)[1]}"
                    r["experiment_id"] = d_id
                    _insert(dst, "runs", r)
                counts = {t: _copy_rows(src, dst, t, tcols[t], runs) for t in RUN_TABLES}
                if has_inputs:
                    # an input links a dataset (source) to a run (destination)
                    ins = _dicts(src, "SELECT * FROM inputs WHERE destination_type = 'RUN' AND destination_id = ANY(%s)", (runs,))
                    uuids = list({i["source_id"] for i in ins if i.get("source_type") == "DATASET"})
                    remap = {}
                    for d in (_dicts(src, "SELECT * FROM datasets WHERE dataset_uuid = ANY(%s)", (uuids,)) if uuids else []):
                        hit = _q(dst, "SELECT dataset_uuid FROM datasets WHERE experiment_id = %s AND name = %s AND digest = %s", (d_id, d["name"], d["digest"]))
                        if hit:
                            remap[d["dataset_uuid"]] = hit[0][0]
                        else:
                            _insert(dst, "datasets", {c: d[c] for c in _common(src, dst, "datasets")} | {"experiment_id": d_id})
                    icols = _common(src, dst, "inputs")
                    for i in ins:
                        _insert(dst, "inputs", {c: i[c] for c in icols} | {"source_id": remap.get(i["source_id"], i["source_id"])})
                    if ins and _has(src, "input_tags"):
                        _copy_rows(src, dst, "input_tags", _common(src, dst, "input_tags"), [i["input_uuid"] for i in ins], key="input_uuid")
                    counts["inputs"] = len(ins)
                if has_registry:
                    counts["model versions"] = _versions(src, dst, runs, sp, dp, d_id)
                out.append(f"{x['name']}: {len(runs)} runs ({len(x['changed_now'])} replaced) · " + ", ".join(f"{k} {v}" for k, v in counts.items()))
            dst.commit()
        return "\n".join(out) + "\n"

    def check_rows():
        bad, out = [], []
        with connect(src_sv, "own") as src, connect(dst_sv, "sync") as dst:
            for x in chosen:
                runs = x["runs"]
                if not runs:
                    continue
                if _digests(src, runs) != _digests(dst, runs):
                    bad.append(f"{x['name']}: params, tags, last metrics or dataset inputs differ")
                for t in RUN_TABLES:
                    q = f"SELECT run_uuid, count(*) FROM {t} WHERE run_uuid = ANY(%s) GROUP BY run_uuid"
                    if dict(_q(src, q, (runs,))) != dict(_q(dst, q, (runs,))):
                        bad.append(f"{x['name']}: the {t} rows differ")
                q = "SELECT run_uuid, status, end_time, lifecycle_stage FROM runs WHERE run_uuid = ANY(%s)"
                if sorted(_q(src, q, (runs,))) != sorted(_q(dst, q, (runs,))):
                    bad.append(f"{x['name']}: the runs differ")
                out.append(f"{x['name']}: {len(runs)} runs checked")
        if bad:
            raise RuntimeError("\n".join(bad))
        return "\n".join(out) + "\nrows identical\n"

    steps = [*tunnel_steps(src_sv, "own", s_what), *tunnel_steps(dst_sv, "sync", d_what),
             actions.step("check that the preview still holds, create the experiments missing from the synchronised database", fn=experiments,
                          display="through the tunnels: the preview again (read-only); then in the synchronised database, for each experiment it lacks:\n"
                                  "  INSERT INTO experiments (...) RETURNING experiment_id; UPDATE experiments SET artifact_location = "
                                  f"'{dp['sync_artifacts']}/<its id>'; INSERT INTO experiment_tags ...\n"
                                  + "\n".join(f"  {x['name']}: " + (f"id {x['dst_id']} there" if x["dst_id"] else "created") for x in chosen))]
    for x in chosen:
        arts = [r for r in x["new"] + x["changed"] if "a" in x["folders"].get(r, {})]
        steps.append(_copy_step(src_sv, dst_sv, "a", x["name"], x["src_id"], lambda x=x: ctx["exp"].get(x["name"]),
                                lambda x=x: [r for r in x.get("runs", x["new"] + x["changed"]) if "a" in x["folders"].get(r, {})],
                                len(arts), sum(x["folders"][r]["a"][1] for r in arts)))
        if x["src_id"] != x["dst_id"] and any("a" in x["folders"].get(r, {}) for r in x["new"] + x["changed"]):
            steps.append(mlmodel_step(x))
    steps += [actions.step("copy the rows of the runs (one transaction: all or nothing)", fn=rows, timeout=24 * 3600,
                           display="through the tunnels, in one transaction on the synchronised database, per experiment:\n"
                                   "  runs changed since their last sync: DELETE their rows (params, tags, latest_metrics, metrics, inputs, runs)\n"
                                   f"  INSERT the runs (experiment id of the synchronised database, artifact_uri under {dp['sync_artifacts']})\n"
                                   "  COPY params, tags, latest_metrics, metrics (as they are); datasets, inputs, input_tags\n"
                                   "  model versions of these runs: renumbered after the versions there, source under the new artifact folder\n"
                                   "COMMIT\n" + "\n".join(f"  {x['name']}: {len(x['new'])} new, {len(x['changed'])} changed" for x in chosen)),
              actions.step("check the rows: counts per run and table, digests of params, tags and last metrics", fn=check_rows, timeout=3600,
                           display="read-only, both databases: per run, the number of rows of params, tags, latest_metrics, metrics and dataset inputs, "
                                   "and the run's status, end time, stage: they must be identical"),
              actions.step(f"check the artifacts: the folders copied, on {src_sv['name']} and on {dst_sv['name']}", timeout=3600,
                           fn=lambda: _check_folders(src_sv, dst_sv, [(x["name"], "a", x["src_id"], ctx["exp"][x["name"]],
                                                                       [r for r in x["runs"] if "a" in x["folders"].get(r, {})]) for x in chosen]),
                           display=f"on both servers (ssh, read-only): for each folder copied, its number of files and bytes (find -printf %s, MLmodel aside): they must be identical\n{SIZE_SCRIPT}"),
              *close_steps(src_sv, "own", s_what), *close_steps(dst_sv, "sync", d_what)]
    return steps


def ckpt_steps(src_sv, dst_sv, names):
    """Copy the checkpoint folders the preview found missing or different on the central server (runs already in the
    synchronised database), then check them. No database is read or written."""
    plan = PLANS.get(src_sv["name"])
    if not plan:
        raise ValueError(f"preview the sync of {src_sv['name']} first")
    if plan["central"] != (dst_sv or {}).get("name"):
        raise ValueError("the central server changed since the preview: preview again")
    chosen = [x for x in plan["experiments"] if x["name"] in set(names or []) and x["ckpt"] and x["dst_id"]]
    if not chosen:
        raise ValueError("no checkpoint to copy in the experiments chosen")
    steps = [_copy_step(src_sv, dst_sv, "c", x["name"], x["src_id"], lambda x=x: x["dst_id"], lambda x=x: list(x["ckpt"]),
                        len(x["ckpt"]), sum(v[1] for v in x["ckpt"].values())) for x in chosen]
    steps.append(actions.step(f"check the checkpoints: the folders copied, on {src_sv['name']} and on {dst_sv['name']}", timeout=3600,
                              fn=lambda: _check_folders(src_sv, dst_sv, [(x["name"], "c", x["src_id"], x["dst_id"], list(x["ckpt"])) for x in chosen]),
                              display=f"on both servers (ssh, read-only): for each folder copied, its number of files and bytes (find -printf %s): they must be identical\n{SIZE_SCRIPT}"))
    return steps


def ckpt_done(src_name, names):
    """After a checkpoint sync that went through: those folders are there now (the preview stays usable)."""
    for x in (PLANS.get(src_name) or {}).get("experiments", []):
        if x["name"] in set(names or []):
            x["ckpt_done"] += len(x["ckpt"])
            x["ckpt"] = {}


def _versions(src, dst, runs, sp, dp, d_id):
    """Model versions of the runs: added after the versions there (a run already registered is skipped)."""
    cols = _common(src, dst, "model_versions")
    n = 0
    for v in _dicts(src, f"SELECT {', '.join(cols)} FROM model_versions WHERE run_id = ANY(%s) ORDER BY name, version", (runs,)):
        if _q(dst, "SELECT 1 FROM model_versions WHERE name = %s AND run_id = %s", (v["name"], v["run_id"])):
            continue
        if not _q(dst, "SELECT 1 FROM registered_models WHERE name = %s", (v["name"],)):
            rm = _dicts(src, "SELECT * FROM registered_models WHERE name = %s", (v["name"],))
            if rm:
                _insert(dst, "registered_models", {c: rm[0][c] for c in _common(src, dst, "registered_models")})
        old_v = v["version"]
        v["version"] = (_q(dst, "SELECT coalesce(max(version), 0) FROM model_versions WHERE name = %s", (v["name"],))[0][0] or 0) + 1
        sa, da = sp["artifacts"].rstrip("/"), dp["sync_artifacts"].rstrip("/")
        for k in ("source", "storage_location"):
            if v.get(k):
                p = _local(v[k])
                if p.startswith(sa + "/"):
                    v[k] = f"{da}/{d_id}/" + p[len(sa) + 1:].split("/", 1)[1]
        _insert(dst, "model_versions", v)
        if _has(src, "model_version_tags"):
            for t in _dicts(src, "SELECT * FROM model_version_tags WHERE name = %s AND version = %s", (v["name"], old_v)):
                _insert(dst, "model_version_tags", {**t, "version": v["version"]})
        n += 1
    return n


def light(plan):
    """A preview for the page: counts per experiment, no run lists."""
    if not plan:
        return None
    keep = ("source", "central", "dst_folder", "when", "schema", "schema_dst", "blocking", "not_copied",
            "src_artifacts", "src_checkpoints", "dst_artifacts", "dst_checkpoints")
    return {**{k: plan.get(k) for k in keep},
            "experiments": [{"name": x["name"], "src_id": x["src_id"], "dst_id": x["dst_id"], "deleted": x["deleted"], "new": len(x["new"]),
                             "changed": len(x["changed"]), "synced": x["synced"], "old_path": len(x["old_path"]), "files": x["files"], "bytes": x["bytes"],
                             "ckpt": len(x["ckpt"]), "ckpt_bytes": sum(v[1] for v in x["ckpt"].values()), "ckpt_files": sum(v[0] for v in x["ckpt"].values()),
                             "ckpt_wait": x["ckpt_wait"], "ckpt_done": x["ckpt_done"],
                             "old_example": x["old_path"][0]["artifact_uri"] if x["old_path"] else None} for x in plan["experiments"]]}

