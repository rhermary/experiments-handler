"""Launches on another server: the script the launcher renders for an XP runs there, over the open ssh connection
(Servers > Connect), as a logged action (its commands shown and kept like the other actions).

On the server, in its launcher folder (Servers > paths):
- <launcher>/repo: a copy of the project, what the script's docker cp lines copy into the container. It is copied
  again only when the project changed: a fingerprint of the copied files (names, sizes, times) is kept with it.
  What is copied: project.snapshot of the settings (as start.sh), else everything but what .gitignore files exclude.
- <launcher>/launches/<time>_<XP>_<4 random hex>/: run.sh (the script, its working folder set to the repo) and output.log.
The XP's base configs written by the launcher (<scripts_dir>/<XP>/configs) are copied into the repo at each launch.

One launch at a time per server: the repo is not replaced while a script copies it into its container.
The image (the IMAGE_NAME variable) must be on the server already: the launch fails at once otherwise, instead of
docker pulling it (pull it there yourself, or with Databases > Pull image).
"""
import hashlib
import os
import shlex
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

import actions
import core
import remote

FINGERPRINT = ".athanor-fingerprint"
SKIP_DIRS = {"__pycache__", ".mypy_cache"}
LAUNCHER_DIRS = ("tools/experiments", "tools/experiments.old", "tools/experiments-handler")   # never copied
_LOCKS = {}
_LOCKS_LOCK = threading.Lock()


def _lock(name):
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(name, threading.Lock())


def paths(sv):
    """(repo, launches) folders of a server."""
    p = core.server_paths(sv)
    return p["repo"], p["launches"]


def workdir(sv):
    """The folder a script launched on `sv` runs from (its @@WORKDIR@@)."""
    return paths(sv)[0]


def logdir(sv):
    """Where the output of the jobs launched on `sv` is kept (@@KEEP_LOGS@@): <launcher>/job-logs there."""
    return core.server_paths(sv)["launcher"] + "/" + core.JOB_LOGS


def _items(cfg):
    """What is copied of the project: the project.snapshot list, else None (all but what .gitignore excludes)."""
    items = str(cfg["project"].get("snapshot") or "").split()
    return [i for i in items if i not in LAUNCHER_DIRS] or None


def fingerprint(root, items):
    """A digest of the files to copy (relative name, size, modification time): it changes when the project does."""
    h = hashlib.sha1()
    tops = [Path(root) / i for i in items] if items else [Path(root)]
    for top in tops:
        if top.is_file():
            st = top.stat()
            h.update(f"{top.relative_to(root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
            continue
        for d, dirs, files in os.walk(top):
            rel_d = os.path.relpath(d, root)
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS
                             and os.path.normpath(os.path.join(rel_d, x)) not in LAUNCHER_DIRS)
            for f in sorted(files):
                full = os.path.join(d, f)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                h.update(f"{os.path.join(rel_d, f)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:20]


def _ssh(sv, command):
    return shlex.join(remote.ssh_argv(sv, command))


def _copy_command(cfg, root, sv, fp):
    """bash pipeline: tar of the project here | tar into <repo>.new there, then swapped with <repo>."""
    repo, _ = paths(sv)
    q = shlex.quote
    skip = " ".join(f"--exclude={x}" for x in sorted(SKIP_DIRS))
    items = _items(cfg)
    if items:
        what = " ".join(q(i) for i in items if (Path(root) / i).exists())
        local = f"tar -C {q(str(root))} {skip} -cf - {what}"
    else:
        launcher = " ".join(f"--exclude=./{x}" for x in LAUNCHER_DIRS)
        local = f"tar -C {q(str(root))} {skip} {launcher} --exclude-vcs-ignores -cf - ."
    there = (f"set -e; rm -rf {q(repo + '.new')}; mkdir -p {q(repo + '.new')}; tar -C {q(repo + '.new')} -xf -; "
             f"printf %s {q(fp)} > {q(repo + '.new/' + FINGERPRINT)}; "
             f"if [ -d {q(repo)} ]; then mv {q(repo)} {q(repo + '.old')}; fi; mv {q(repo + '.new')} {q(repo)}; "
             f"rm -rf {q(repo + '.old')}")
    return f"set -o pipefail; {local} | {_ssh(sv, there)}"


def _remote_fingerprint(sv):
    repo, _ = paths(sv)
    r = subprocess.run(remote.ssh_argv(sv, f"cat {shlex.quote(repo + '/' + FINGERPRINT)} 2>/dev/null || true"),
                       capture_output=True, text=True, timeout=30, check=False, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ValueError(f"cannot read the copy of the project on {sv['name']}: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


IMAGE_VAR = "IMAGE_NAME"


def image_of(table):
    """The image the selection's script runs (the IMAGE_NAME variable, its $references expanded), or ""."""
    e = table.get(IMAGE_VAR)
    return core.shell_expand(e["value"], table).strip() if e else ""


def steps(cfg, sv, script, xp, config="", image=""):
    """The steps of a launch of `script` (rendered with workdir(sv)) on `sv`, and the folder of the launch there.
    config: the XP's base config when the launcher wrote it (copied with its folder); image: checked first."""
    root = core.project_root(cfg)
    repo, launches = paths(sv)
    folder = f"{launches}/{datetime.now().strftime('%Y%m%d_%H%M%S')}_{core.slug(xp)}_{uuid.uuid4().hex[:4]}"
    q = shlex.quote
    fp = fingerprint(root, _items(cfg))
    copy = _copy_command(cfg, root, sv, fp)

    def copy_project():
        there = _remote_fingerprint(sv)
        if there == fp:
            return f"{repo} is up to date (fingerprint {fp})"
        r = subprocess.run(["bash", "-c", copy], capture_output=True, text=True, timeout=1800, check=False,
                           stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"the copy failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-600:]}")
        return f"copied the project to {repo} (fingerprint {fp}; before: {there or 'none'})"

    out = []
    if image:
        out.append(remote.ssh_script(f"check that {image} is on {sv['name']} (it is not pulled: the launch stops here)", sv,
                                     f"docker image inspect {q(image)} >/dev/null 2>&1 || {{ echo {q(f'{image} is not on ' + sv['name'] + f': pull it there first (docker pull {image})')} >&2; exit 1; }}",
                                     timeout=30))
    out += [actions.step(f"copy the project to {repo} on {sv['name']}, if it changed since the last copy", fn=copy_project,
                        display=f"# only when the fingerprint of the files differs from {repo}/{FINGERPRINT} ({fp})\n"
                                + copy, timeout=1800)]
    if config and core.is_generated(cfg, config):
        gen = os.path.dirname(os.path.normpath(config))
        out.append(actions.step(f"copy the base configs of {xp} ({gen}) into {repo}",
                                ["bash", "-c", f"set -o pipefail; tar -C {q(str(root))} -cf - {q(gen)} | "
                                               + _ssh(sv, f"mkdir -p {q(repo)} && tar -C {q(repo)} -xf -")], timeout=120))
    out.append(actions.step(f"write the script to {folder}/run.sh", remote.ssh_argv(sv, f"mkdir -p {q(folder)} && cat > {q(folder + '/run.sh')}"),
                            stdin=script, display=_ssh(sv, f"mkdir -p {q(folder)} && cat > {q(folder + '/run.sh')}") + "  < the script",
                            timeout=60))
    out.append(remote.ssh_script(f"run it on {sv['name']} (its output: {folder}/output.log)", sv,
                                 f"cd {q(folder)} && set -o pipefail && bash run.sh 2>&1 | tee output.log", timeout=1800))
    return out, folder


def launch(cfg, sv, script, xp, config="", image=""):
    """Run the launch (one at a time per server). Returns (actions result, folder on the server)."""
    if not remote.connected(sv):
        raise ValueError(f"{sv['name']} is not connected: click Connect in Servers first")
    with _lock(sv["name"]):
        st, folder = steps(cfg, sv, script, xp, config, image)
        res = actions.run("launch", sv["name"], f"{sv['name']}: launch {xp}", st, meta={"xp": xp, "folder": folder})
    return res, folder


def output_of(res):
    """What the script printed (the last step), or the error of the step that failed."""
    if not res["steps"]:
        return ""
    last = res["steps"][-1]
    bad = next((s for s in res["steps"] if s["rc"]), None)
    if bad is not None and bad is not last:
        return f"{bad['label']}: failed\n{(bad['err'] or bad['out']).strip()}"
    return (last["out"] + last["err"]).strip()


def check_selection(table):
    """A remote launch cannot attach a terminal: DEBUG must be 0."""
    if (table.get("DEBUG") or {}).get("value", "0").strip() not in ("", "0"):
        raise ValueError("DEBUG=1 attaches the container to a terminal: on another server, launch with DEBUG=0")
