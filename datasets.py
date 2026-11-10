"""Datasets: the processed folders of your datasets on each server (<datasets>/<name>/processed, its version in
processed/digest.sha256), compared with the ones of a source server (the central one by default: it has the latest),
and sent from it to another server when you click.

The servers cannot reach each other: a send goes through this machine (tar on the source | tar on the target, over
the open ssh connections). On the target, the copy is written next to the old folder (processed.new) and swapped in
at the end, so a send that fails or is interrupted leaves the old folder as it was. Only processed/ is touched (the raw
files of the dataset are not). Then the digests are compared.
"""
import shlex
import subprocess

import actions
import core
import remote

DIGEST = "digest.sha256"


def folder(sv):
    return core.server_paths(sv)["datasets"]


def status_steps(sv):
    """Read-only: for each dataset with a processed folder: its digest (first word of digest.sha256), size, date."""
    d = shlex.quote(folder(sv))
    script = "\n".join([
        f"D={d}",
        'if [ ! -d "$D" ]; then echo "@@NODIR $D"; exit 0; fi',
        'for p in "$D"/*/processed; do',
        '  [ -d "$p" ] || continue',
        '  n=$(basename "$(dirname "$p")"); f="$p/' + DIGEST + '"',
        '  if [ -f "$f" ]; then dg=$(awk \'NR==1{print $1}\' "$f"); when=$(date -r "$f" "+%Y-%m-%d %H:%M"); else dg=-; when=-; fi',
        '  echo "@@D|$n|$dg|$(du -sk "$p" 2>/dev/null | cut -f1)|$when"',
        "done",
        "exit 0"])
    return [remote.ssh_script(f"read the datasets of {sv['name']} in {folder(sv)} (read-only: digests, sizes)", sv, script, timeout=300)]


def parse_status(text):
    """{folder_missing, datasets: {name: {digest, kb, when}}}."""
    out = {"folder_missing": "", "datasets": {}}
    for ln in text.splitlines():
        if ln.startswith("@@NODIR "):
            out["folder_missing"] = ln[len("@@NODIR "):]
        elif ln.startswith("@@D|"):
            _, name, digest, kb, when = (ln.split("|") + [""] * 5)[:5]
            out["datasets"][name] = {"digest": "" if digest == "-" else digest,
                                     "kb": int(kb) if kb.isdigit() else None, "when": "" if when == "-" else when}
    return out


def _check_name(name):
    if not name or name in (".", "..") or "/" in name or name.startswith("-"):
        raise ValueError(f"not a dataset name: '{name}'")
    return name


def send_steps(cfg, dst, args):
    """Send the processed folders `args["names"]` from the server `args["source"]` to `dst`, then check the digests."""
    src = remote.server(cfg, str(args.get("source") or ""))
    if src["name"] == dst["name"]:
        raise ValueError("the source and the target are the same server")
    if not remote.connected(src):
        raise ValueError(f"{src['name']} (the source) is not connected: click Connect first")
    names = [_check_name(str(n)) for n in args.get("names") or []]
    if not names:
        raise ValueError("no dataset to send")
    q = shlex.quote
    sd, dd = folder(src), folder(dst)
    steps = []
    for n in names:
        there = f"{dd}/{n}"
        receive = (f"set -e; mkdir -p {q(there)}; cd {q(there)}; rm -rf processed.new; mkdir processed.new; "
                   "tar -C processed.new --strip-components=1 -xf -; "
                   "if [ -d processed ]; then mv processed processed.old; fi; mv processed.new processed; rm -rf processed.old")
        cmd = (f"set -o pipefail; {shlex.join(remote.ssh_argv(src, f'tar -C {q(sd + '/' + n)} -cf - processed'))} | "
               f"{shlex.join(remote.ssh_argv(dst, receive))}")
        steps.append(actions.step(f"send {n}/processed from {src['name']} to {dst['name']} (tar | tar through this machine; "
                                  "the old folder is replaced only once the copy is complete)", ["bash", "-c", cmd], timeout=6 * 3600))

    def check():
        def digests(sv):
            script = "; ".join(f"echo \"{n}|$(awk 'NR==1{{print $1}}' {q(folder(sv) + '/' + n + '/processed/' + DIGEST)} 2>/dev/null)\""
                               for n in names)
            r = subprocess.run(remote.ssh_argv(sv, script), capture_output=True, text=True, timeout=60, check=False,
                               stdin=subprocess.DEVNULL)
            return dict(ln.split("|", 1) for ln in r.stdout.splitlines() if "|" in ln)
        a, b = digests(src), digests(dst)
        lines, bad = [], False
        for n in names:
            x, y = a.get(n, ""), b.get(n, "")
            if x == y:
                lines.append(f"{n}: same digest {x[:16] or '(no digest file)'}")
            else:
                bad = True
                lines.append(f"{n}: DIFFERS: {x or '(none)'} on {src['name']}, {y or '(none)'} on {dst['name']}")
        if bad:
            raise RuntimeError("\n".join(lines))
        return "\n".join(lines)
    steps.append(actions.step(f"check the digests of the {len(names)} dataset(s) on {src['name']} and {dst['name']} (read-only)",
                              fn=check, timeout=120))
    return steps
