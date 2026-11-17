"""Checkpoints cache: copies of synchronised checkpoints on the other servers, to test them where GPUs are free.

The checkpoints of the runs synced to the central server are in its synchronised folders
(<synchronised>/checkpoints/<experiment id>/<run>, and the run's config in
<synchronised>/artifacts/<experiment id>/<run>/artifacts/config.yaml). Another server keeps the ones you send in its
cache folder (Servers > Paths: Checkpoints cache, <base>/checkpoints-cache by default), with the same layout:
<cache>/checkpoints/<experiment id>/<run> (the whole checkpoint folder) and
<cache>/artifacts/<experiment id>/<run>/artifacts/config.yaml (with MLmodel, when it has one). A test XP reads
<cache>/checkpoints/1/$RUN_ID/last.ckpt and <cache>/artifacts/1/$RUN_ID/artifacts/config.yaml; on the central server,
the cache is linked to the synchronised folders (Link), so the same paths work there too.

Nothing is sent by itself: you read the runs (the synchronised database and folders, on the central server), read the
cache of the servers, choose runs, and Send. The servers cannot reach each other: a send goes through this machine
(tar on the central server | tar on the target). A run is written in <cache>/.incoming first and moved into place
once complete, then the files and bytes of both sides are compared. Remove deletes runs from a cache (only there).
"""
import re
import shlex
import subprocess
from datetime import datetime

import actions
import core
import remote
import sync

CATALOG = {}                        # the last read of the central server: {when, central, experiments, error}
RUN_KEY = re.compile(r"^[\w.-]+/[\w.-]+$")      # <experiment id>/<run id>

# the run folders under C (checkpoints) and whether A (artifacts) has their config: @@R <exp>/<run> <files> <bytes>,
# @@CFG <exp>/<run>; @@NODIR when C does not exist; @@LINK when C is a link (the central server's cache)
LIST_SCRIPT = r"""if [ -L "$C" ]; then echo "@@LINK $(readlink "$C")"; fi
if [ ! -d "$C" ]; then echo "@@NODIR $C"; exit 0; fi
cd "$C" || exit 1
for d in */*/; do
  d=${d%/}; [ -d "$d" ] || continue
  find -L "$d" -type f -printf '%s\n' | awk -v r="$d" '{n++; s+=$1} END {printf "@@R %s %d %.0f\n", r, n, s}'
  [ -f "$A/$d/artifacts/config.yaml" ] && echo "@@CFG $d"
done
exit 0"""


def paths(sv):
    """(checkpoints, artifacts) folders of a server's cache."""
    k = core.server_paths(sv)["ckpt_cache"]
    return f"{k}/checkpoints", f"{k}/artifacts"


def _list_script(c_root, a_root):
    return f"C={shlex.quote(c_root)}; A={shlex.quote(a_root)}\n{LIST_SCRIPT}"


def parse_list(text):
    """{folder_missing, link, runs: {<exp>/<run>: {files, bytes, config}}}."""
    out = {"folder_missing": "", "link": "", "runs": {}}
    for ln in text.splitlines():
        p = ln.split()
        if ln.startswith("@@NODIR "):
            out["folder_missing"] = ln[len("@@NODIR "):]
        elif ln.startswith("@@LINK "):
            out["link"] = ln[len("@@LINK "):]
        elif p[:1] == ["@@R"] and len(p) == 4:
            out["runs"].setdefault(p[1], {"config": False}).update(files=int(p[2]), bytes=int(float(p[3].replace(",", "."))))
        elif p[:1] == ["@@CFG"] and len(p) == 2:
            out["runs"].setdefault(p[1], {"files": 0, "bytes": 0})["config"] = True
    return out


def _list(sv, c_root, a_root):
    r = subprocess.run(remote.ssh_argv(sv, "bash -s"), input=_list_script(c_root, a_root), capture_output=True, text=True,
                       timeout=600, check=False)
    if r.returncode:
        raise RuntimeError(f"listing {c_root} on {sv['name']} failed: {r.stderr.strip()}")
    return parse_list(r.stdout)


def _central(cfg):
    c = core.central_server(cfg)
    if not c:
        raise ValueError("no central server: choose one (Servers > Edit servers)")
    if not remote.connected(c):
        raise ValueError(f"{c['name']} (the central server) is not connected: click Connect first")
    return c


def _keys(args):
    runs = [str(r) for r in args.get("runs") or []]
    bad = [r for r in runs if not RUN_KEY.match(r) or ".." in r]
    if bad:
        raise ValueError(f"not runs (<experiment id>/<run id>): {', '.join(bad[:3])}")
    if not runs:
        raise ValueError("no run chosen")
    return runs


# --------------------------------------------------------------------------- reading

def read_steps(central):
    """Read-only, on the central server: the runs of the synchronised database (experiment, name, status) and the
    checkpoint folders of the synchronised folder (files, bytes, config). The database is optional: when it does
    not answer, the runs are listed by their ids only."""
    cp = core.server_paths(central)
    what = f"the synchronised database on {central['name']}"

    def read():
        found = _list(central, cp["sync_checkpoints"], cp["sync_artifacts"])
        names, error = {}, ""
        try:
            with sync.connect(central, "sync") as conn:
                exps = dict(sync._q(conn, "SELECT experiment_id, name FROM experiments"))   # pylint: disable=protected-access
                for rid, eid, name, status, start in sync._q(                             # pylint: disable=protected-access
                        conn, "SELECT run_uuid, experiment_id, name, status, start_time FROM runs WHERE lifecycle_stage = 'active'"):
                    names[f"{eid}/{rid}"] = {"name": name or "", "status": status or "", "experiment": exps.get(eid, ""),
                                             "start": datetime.fromtimestamp(start / 1000).strftime("%Y-%m-%d %H:%M") if start else ""}
        except Exception as err:  # pylint: disable=broad-except  # the database is optional: ids only
            error = f"{type(err).__name__}: {err}".strip()
        by_exp = {}
        for key, v in sorted(found["runs"].items()):
            eid, rid = key.split("/", 1)
            meta = names.get(key) or {"name": "", "status": "", "start": "", "experiment": ""}
            x = by_exp.setdefault(eid, {"id": eid, "name": meta["experiment"] or "", "runs": []})
            x["name"] = x["name"] or meta["experiment"]
            x["runs"].append({"key": key, "id": rid, "in_db": key in names, **meta, **v})
        CATALOG.clear()
        CATALOG.update(central=central["name"], when=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), folder=cp["sync_checkpoints"],
                       folder_missing=found["folder_missing"], error=error,
                       experiments=sorted(by_exp.values(), key=lambda x: (x["name"] or "~", x["id"])))
        n = sum(len(x["runs"]) for x in by_exp.values())
        return (f"{n} run folder(s) in {cp['sync_checkpoints']}, {len(by_exp)} experiment(s)"
                + (f"\nthe database did not answer (runs listed by id): {error}" if error else "") + "\n")

    tunnel = [dict(s, allow_fail=True) for s in sync.tunnel_steps(central, "sync", what)]   # the names: optional
    shown = (f"read-only: the runs of {what} (id, experiment, name, status, start), through the tunnel,\n"
             f"and on {central['name']} (ssh ... bash -s), the run folders of {cp['sync_checkpoints']}:\n"
             + _list_script(cp["sync_checkpoints"], cp["sync_artifacts"]))
    return [*tunnel, actions.step(f"read the runs and their checkpoint folders on {central['name']} (read-only)", fn=read,
                                  display=shown, timeout=900),
            *sync.close_steps(central, "sync", what)]


def status_steps(sv):
    """Read-only: the runs in a server's cache (files, bytes, config)."""
    c_root, a_root = paths(sv)
    return [remote.ssh_script(f"read the checkpoints cache of {sv['name']} in {c_root} (read-only: run folders, sizes)",
                              sv, _list_script(c_root, a_root), timeout=600)]


# --------------------------------------------------------------------------- sending, removing, linking

def send_steps(cfg, dst, args):
    """Copy the runs `args["runs"]` (<experiment id>/<run id>) from the central server's synchronised folders into the
    cache of `dst`: each checkpoint folder (moved into place once complete), then the configs, then a check."""
    central = _central(cfg)
    if central["name"] == dst["name"]:
        raise ValueError(f"{dst['name']} is the central server: its cache is the synchronised folder (Link)")
    runs = _keys(args)
    cp = core.server_paths(central)
    sc, sa = cp["sync_checkpoints"], cp["sync_artifacts"]
    dc, da = paths(dst)
    inc = core.server_paths(dst)["ckpt_cache"] + "/.incoming"
    q = shlex.quote
    ssh = lambda sv, cmd: shlex.join(remote.ssh_argv(sv, cmd))     # noqa: E731
    steps = []
    for key in runs:
        eid, rid = key.split("/", 1)
        receive = (f"set -e; mkdir -p {q(inc)} {q(dc + '/' + eid)}; T=$(mktemp -d {q(inc)}/XXXXXX); "
                   f"tar -C \"$T\" -xf -; rm -rf {q(dc + '/' + key)}; mv \"$T\"/{q(key)} {q(dc + '/' + key)}; rm -rf \"$T\"")
        cmd = f"set -o pipefail; {ssh(central, f'tar -C {q(sc)} -cf - {q(key)}')} | {ssh(dst, receive)}"
        steps.append(actions.step(f"send the checkpoint folder of {key} from {central['name']} to {dst['name']} "
                                  "(tar | tar through this machine; moved into place once complete)",
                                  ["bash", "-c", cmd], timeout=12 * 3600))
    # the configs (and MLmodel): small, all at once; a run without config sends nothing here
    pick = (f"cd {q(sa)} && for r in {' '.join(q(k) for k in runs)}; do for f in config.yaml MLmodel; do "
            f"if [ -f \"$r/artifacts/$f\" ]; then echo \"$r/artifacts/$f\"; fi; done; done | tar -cf - -T -")
    cmd = f"set -o pipefail; {ssh(central, pick)} | {ssh(dst, f'mkdir -p {q(da)} && tar -C {q(da)} -xf -')}"
    steps.append(actions.step(f"send the configs of the {len(runs)} run(s) (artifacts/config.yaml, MLmodel)", ["bash", "-c", cmd], timeout=600))

    def check():
        a = _list(central, sc, sa)["runs"]
        b = _list(dst, dc, da)["runs"]
        lines, bad = [], False
        for key in runs:
            x, y = a.get(key), b.get(key)
            if not x:
                bad = True
                lines.append(f"{key}: not on {central['name']} any more")
            elif not y or (x["files"], x["bytes"]) != (y["files"], y["bytes"]) or x["config"] != y["config"]:
                bad = True
                lines.append(f"{key}: DIFFERS: {x['files']} files {x['bytes']} bytes on {central['name']}, "
                             + (f"{y['files']} files {y['bytes']} bytes on {dst['name']}" if y else f"missing on {dst['name']}")
                             + ("" if not y or x["config"] == y["config"] else ", config.yaml missing"))
            else:
                lines.append(f"{key}: same ({x['files']} files, {x['bytes'] / 1e9:.2f} GB{', config' if x['config'] else ', no config.yaml'})")
        if bad:
            raise RuntimeError("\n".join(lines))
        return "\n".join(lines) + "\n"
    steps.append(actions.step(f"check the {len(runs)} run(s) on {central['name']} and {dst['name']} (read-only: files and bytes)",
                              fn=check, display=f"on both servers (ssh ... bash -s, read-only):\n{LIST_SCRIPT}", timeout=900))
    return steps


def remove_steps(cfg, sv, args):
    """Delete runs from a server's cache (nothing else: the synchronised folders are not touched)."""
    if (core.central_server(cfg) or {}).get("name") == sv["name"]:
        raise ValueError(f"{sv['name']} is the central server: its cache is the synchronised folder, nothing is removed there")
    runs = _keys(args)
    dc, da = paths(sv)
    q = shlex.quote
    script = "\n".join([f"C={q(dc)}; A={q(da)}",
                        *(f'rm -rf "$C"/{q(k)} "$A"/{q(k)} && echo "removed {k}"' for k in runs),
                        'rmdir "$C"/* "$A"/* 2>/dev/null; exit 0'])
    return [remote.ssh_script(f"remove {len(runs)} run(s) from the checkpoints cache of {sv['name']}", sv, script, timeout=300)]


def link_steps(cfg, sv):
    """On the central server: its cache folders are links to the synchronised ones (the test XPs' paths work there)."""
    if (core.central_server(cfg) or {}).get("name") != sv["name"]:
        raise ValueError(f"{sv['name']} is not the central server: its cache is filled by Send")
    p = core.server_paths(sv)
    dc, da = paths(sv)
    q = shlex.quote
    script = "\n".join([
        f"mkdir -p {q(p['ckpt_cache'])}",
        *(f"if [ -e {q(d)} ] && [ ! -L {q(d)} ]; then echo '{d} exists and is not a link: move it away first' >&2; exit 1; fi; "
          f"ln -sfn {q(s)} {q(d)} && echo '{d} -> {s}'"
          for d, s in ((dc, p["sync_checkpoints"]), (da, p["sync_artifacts"])))])
    return [remote.ssh_script(f"link the checkpoints cache of {sv['name']} to its synchronised folders", sv, script, timeout=60)]
