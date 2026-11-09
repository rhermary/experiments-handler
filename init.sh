#!/usr/bin/env bash
# Set up experiments-handler for the project it is checked out in (<project>/tools/experiments-handler): the folder of
# its data (settings, script template, generated scripts, logs), reached from the project as tools/experiments.
# Then start it with start.sh (next to this script).
#
#   tools/experiments-handler/init.sh                                   # data in <project>/tools/experiments
#   tools/experiments-handler/init.sh --data /data/me/launcher/myproject --image me/myproject:1.0
#   tools/experiments-handler/init.sh --data /data/me/launcher/myproject --move    # tools/experiments has data already
#
#   --data DIR    where the data goes. In a container (start.sh), DIR must be on a disk the host's Docker sees at the
#                 same path, where unix sockets can be created (e.g. /data/...): not a project on sshfs. When DIR is
#                 elsewhere than <project>/tools/experiments, that one becomes a link to DIR.
#   --image IMG   the image start.sh runs the launcher in (project.mlflow_image; default: the IMAGE_NAME variable)
#   --move        <project>/tools/experiments is a folder with data already: copy it into DIR, keep the original as
#                 tools/experiments.old, and link tools/experiments to DIR
#
# Run it again at any time: what exists is kept (settings, template), only what is missing is created.
set -euo pipefail

handler="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project="${XP_PROJECT:-$(cd "$handler/../.." && pwd)}"
link="$project/tools/experiments"
py="${PYTHON:-python}"

data="" image="" move=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --data) data=$2; shift ;;
        --data=*) data=${1#--data=} ;;
        --image) image=$2; shift ;;
        --image=*) image=${1#--image=} ;;
        --move) move=true ;;
        -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
    shift
done

[[ -d "$project/.git" || -f "$project/.git" ]] || echo "warning: $project is not a git repository (the commits list and the launches use its git)" >&2

# the data folder, and tools/experiments pointing to it
if [[ -z "$data" ]]; then
    if [[ -L "$link" ]]; then data="$(cd "$link" && pwd -P)"; else data="$link"; fi
fi
mkdir -p "$data"
data="$(cd "$data" && pwd -P)"
if [[ "$data" != "$(cd "$project/tools" && pwd -P)/experiments" ]]; then
    if [[ -L "$link" ]]; then
        now="$(cd "$link" 2>/dev/null && pwd -P || readlink "$link")"
        if [[ "$now" != "$data" ]]; then
            echo "$link already points to $now: remove that link first to use $data" >&2; exit 1
        fi
    elif [[ -e "$link" ]]; then
        if ! $move; then
            echo "$link is a folder: --move to move its content into $data (it is kept as tools/experiments.old)" >&2; exit 1
        fi
        [[ -e "$link.old" ]] && { echo "$link.old exists already: remove or rename it first" >&2; exit 1; }
        rsync -a "$link/" "$data/"
        diff -rq "$link" "$data" >/dev/null || { echo "the copy of $link into $data differs: nothing moved" >&2; exit 1; }
        mv "$link" "$link.old"
        echo "moved $link to $data (the original is kept as $link.old)"
    fi
    [[ -L "$link" ]] || ln -s "$data" "$link"
    echo "$link -> $data"
fi

# the settings (created from defaults.yaml) and the template
"$py" - "$handler" "$data/experiments.yaml" "$project" "$image" <<'EOF'
import sys
handler, path, project, image = sys.argv[1:]
sys.path.insert(0, handler)
import os
import core
new = not os.path.exists(path)
core.configure(path)
cfg = core.load()                   # creates it from defaults.yaml when missing
p = cfg["project"]
if new:
    p["root"] = project             # absolute: the settings may be far from the project
    p["scripts_dir"] = "tools/experiments/scripts"
if image:
    p["mlflow_image"] = image
if new or image:
    core.save(cfg, backup=not new)
print(("created " if new else "kept ") + path + (f" (image: {image})" if image else ""))
EOF
if [[ ! -f "$data/template.sh" ]]; then
    cp "$handler/template.example.sh" "$data/template.sh"
    echo "created $data/template.sh (from template.example.sh: adapt it to your project, Template in the page)"
fi
if [[ ! -f "$data/.gitignore" ]]; then
    cat > "$data/.gitignore" <<'EOF'
# written by experiments-handler for itself
experiments.yaml.*
.gpu-servers.json
.mlflow-server.log
.queue.json*
actions/
actions.log
.launcher/
# passwd-format list of the servers' users (names GPU process owners): not for git
userlist.csv
EOF
fi
# the forwarder: tools/experiments/start.sh as before
{
    echo '#!/usr/bin/env bash'
    echo '# Written by experiments-handler/init.sh: the launcher'"'"'s start script is tools/experiments-handler/start.sh, found'
    echo '# next to the folder as reached (tools/experiments, a link), else where init.sh was run from (this real folder).'
    echo 'here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
    echo 'start="${here%/*}/experiments-handler/start.sh"'
    printf '[[ -f "$start" ]] || start=%q\n' "$handler/start.sh"
    echo 'exec "$start" "$@"'
} > "$data/start.sh"
chmod +x "$data/start.sh"

# can start.sh run the launcher in a container from here?
mkdir -p -m 700 "$data/.launcher/run"
if ! "$py" -c 'import os, socket, sys; p = os.path.join(sys.argv[1], ".probe"); s = socket.socket(socket.AF_UNIX); s.bind(p); s.close(); os.unlink(p)' "$data/.launcher/run" 2>/dev/null; then
    echo "warning: no unix socket can be created in $data (e.g. sshfs): start.sh can only run the launcher with" >&2
    echo "         --no-container. For a container, run init.sh again with --data on a local disk the host's Docker sees (and --move)." >&2
fi
echo "done: start it with $project/tools/experiments-handler/start.sh (or tools/experiments/start.sh)"
