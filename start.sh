#!/usr/bin/env bash
# Start experiments-handler for the project it is checked out in (<project>/tools/experiments-handler), with the data
# that init.sh set up (<project>/tools/experiments: a folder, or a link to it).
#
#   tools/experiments-handler/start.sh                # in the background, on port 5925 of this server; the terminal is free
#   tools/experiments-handler/start.sh --port 5930    # another port (other options are passed to app.py: --template)
#   tools/experiments-handler/start.sh logs           # its output (docker logs -f: Ctrl+C leaves it running)
#   tools/experiments-handler/start.sh status | stop  # is it running / stop it (and the MLflow server it started)
#   tools/experiments-handler/start.sh agent          # lend the ssh agent of this terminal again (no restart)
#   tools/experiments-handler/start.sh --attach       # in this terminal, until Ctrl+C
#   tools/experiments-handler/start.sh --no-container # with this machine's python, in this terminal (PYTHON=...)
#   LAUNCHER_IMAGE=me/myproject:1.0 LAUNCHER_PORT=5930 LAUNCHER_BIND=127.0.0.1 tools/experiments-handler/start.sh
#   LAUNCHER_VOLUMES="/data/other /scratch" ...       # more folders to mount in the container (same paths)
# Starting again replaces the running one (with a new copy of the project). tools/experiments/start.sh does the same.
#
# The launcher runs in a Docker container of the project's image, so its MLflow is the one that wrote the databases.
# The image: $LAUNCHER_IMAGE, else project.mlflow_image of the settings, else the IMAGE_NAME variable ("local", or no
# image: this machine's python). It must be here already (docker pull IMAGE): it is never pulled.
#
# The project may be on a mount the host's Docker does not see (sshfs in a dev container), so everything the
# container uses is in the data folder, which must be on a disk the host shares at the same path (init.sh --data):
#   <data>/                    the settings, template, scripts, actions
#   <data>/.launcher/snapshot/ a copy of the project made at each start (project.snapshot of the settings: what to
#                              copy; empty: everything but what .gitignore files exclude), with the launcher's code:
#                              what the previews, the argument browser and the launches use. Changes made in the
#                              project after the start are seen at the next start.
#   <data>/.launcher/home/     the container's home: your ~/.ssh/known_hosts and ~/.ssh/config, copied at each start
#   <data>/.launcher/run/      (mode 700) the socket that lends your ssh agent to the container (agent_relay.py), and
#                              the container's /etc/passwd (your home there: home/, where ssh looks for ~/.ssh)
#   <data>/.launcher/bin/      the docker command (the launches run docker on the host, through its socket)
# Also mounted: the socket folder of the local database (tracking_uri), and read-only, the runs' files on this
# machine (each server's artifacts and checkpoints, own and synchronised, as Servers > paths say) for the MLflow UI.
# The GPUs are given to the container (--gpus all, when the host's Docker has the NVIDIA runtime; LAUNCHER_GPUS=none
# to skip): nothing runs on them, the GPU view and the queue read their state with nvidia-smi.
# The container runs as you. Its port is published on the server as the database containers' are (-p): port 5925,
# on every interface: http://<server>:5925. Nothing asks who you are, and the launcher can run commands on your
# servers and has the host's Docker (root on this server): anyone who reaches that port can do the same.
# LAUNCHER_BIND=127.0.0.1 publishes it on the server's loopback only (then: ssh -L 5925:127.0.0.1:5925 <server>).
# Its ssh connections are its own: click Connect in Servers after each start.
#
# In the background, the container keeps running when the terminal closes; the relay of the ssh agent stops with it.
# It uses the agent of the terminal it was started from; when that one stops answering (the VS Code window closed), the
# relay uses another VS Code agent of this machine that answers. "start.sh agent" points it at this terminal's agent.
set -euo pipefail

handler="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="${XP_PROJECT:-$(cd "$handler/../.." && pwd)}"
link="$root/tools/experiments"
py="${PYTHON:-$(command -v python3 || command -v python)}"

if [[ ! -f "$link/experiments.yaml" ]]; then
    echo "no settings in $link: run $handler/init.sh first" >&2
    exit 1
fi
data="$(cd "$link" && pwd -P)"
state="$data/.launcher"
name="${LAUNCHER_NAME:-launcher-app-$(id -u)}"
relay_pidfile="$state/run/relay-$name.pid"

stop_relay() {
    if [[ -f "$relay_pidfile" ]]; then
        pid=$(cat "$relay_pidfile")
        kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true     # its session: the relay with it
        rm -f "$relay_pidfile"
    fi
}
running() { [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == true ]]; }
# the agent as the container sees it (the one of the running container: it may have been started by an older start.sh)
lent=$( (docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$name" 2>/dev/null || true) | sed -n 's/^SSH_AUTH_SOCK=//p' | head -n 1)
lent="${lent:-$state/run/ssh-agent-$name.sock}"
# its first name (one launcher, before the files were per container): the default launcher's
if [[ "$name" == "launcher-app-$(id -u)" && -f "$state/run/relay.pid" && ! -f "$relay_pidfile" ]]; then mv "$state/run/relay.pid" "$relay_pidfile"; fi
start_relay() {
    [[ -n "$lent" ]] || return 0
    # its own session: it outlives this terminal; it waits for the container and stops with it. Its messages (an
    # agent that stopped answering, the one used instead) go to run/relay.log
    setsid nohup bash -c '"$1" "$2" "$3" "$4" 2>>"$7" & r=$!; docker wait "$5" >/dev/null 2>&1; kill $r 2>/dev/null; rm -f "$6"' \
        relay "$py" "$handler/agent_relay.py" "$lent" "$SSH_AUTH_SOCK" "$name" "$relay_pidfile" "$state/run/relay.log" >/dev/null 2>&1 &
    echo $! > "$relay_pidfile"
}

case "${1:-}" in
    stop)
        if running; then docker stop --time 20 "$name" >/dev/null; echo "stopped $name"; else echo "$name is not running"; fi
        docker rm --force "$name" >/dev/null 2>&1 || true
        stop_relay
        exit 0 ;;
    status)
        if running; then
            docker ps --filter "name=^$name\$" --format "$name: {{.Status}} ({{.Image}}, {{.Ports}})"
        else
            echo "$name is not running"; exit 1
        fi
        exit 0 ;;
    logs)
        exec docker logs --follow --tail 100 "$name" ;;
    agent)
        # lend the agent of this terminal again, without restarting the launcher
        if ! running; then echo "$name is not running: start it first" >&2; exit 1; fi
        if [[ -z "${SSH_AUTH_SOCK:-}" || ! -S "$SSH_AUTH_SOCK" ]]; then echo "no ssh agent in this terminal (SSH_AUTH_SOCK)" >&2; exit 1; fi
        stop_relay
        start_relay
        sleep 1
        if SSH_AUTH_SOCK="$lent" ssh-add -l >/dev/null 2>&1; then
            echo "the launcher uses the ssh agent of this terminal ($SSH_AUTH_SOCK): Connect again in Servers"
        else
            echo "the relay started, but the agent does not answer through it (see $state/run/relay.log)" >&2; exit 1
        fi
        exit 0 ;;
esac

# args: for app.py without a container (as given); cargs: in the container, without --host/--port (it listens on
# 0.0.0.0:8000 inside, published on $bind:$port)
container=true attach=false port="${LAUNCHER_PORT:-5925}" bind="${LAUNCHER_BIND:-0.0.0.0}" args=() cargs=()
argv=("$@") i=0
while (( i < ${#argv[@]} )); do
    a=${argv[i]}
    case "$a" in
        --no-container) container=false ;;
        --attach) attach=true ;;
        --port) port=${argv[i+1]:-$port}; args+=("$a" "${argv[i+1]:-}"); i=$((i + 1)) ;;
        --port=*) port=${a#--port=}; args+=("$a") ;;
        --host) args+=("$a" "${argv[i+1]:-}"); i=$((i + 1)) ;;
        --host=*) args+=("$a") ;;
        *) args+=("$a"); cargs+=("$a") ;;
    esac
    i=$((i + 1))
done

# from the settings: the image (line 1), what to copy of the project (line 2), the folders of the local database
# (line 3: the socket folder of a Postgres, the folder of a SQLite file; mounted in the container), the folders of the
# runs' files on this machine (line 4: artifacts and checkpoints of the servers, own and synchronised, as Servers >
# paths say, when they exist here; mounted read-only: the MLflow UI reads the artifacts from them)
mapfile -t conf < <("$py" - "$data/experiments.yaml" "$handler" <<'EOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
p = cfg.get("project") or {}
img = str(p.get("mlflow_image") or "").strip()
if not img:
    img = next((str(v.get("value") or "") for v in (cfg.get("variables") or {}).get("docker") or [] if v.get("name") == "IMAGE_NAME"), "")
print("" if img.lower() == "local" else img)
print(str(p.get("snapshot") or "").strip())
import os, urllib.parse
uri = str(p.get("tracking_uri") or "")
parts = urllib.parse.urlsplit(uri)
dirs = [h for v in urllib.parse.parse_qs(parts.query).get("host", []) for h in v.split(",") if h.startswith("/")]
if parts.scheme.startswith("sqlite") and ":///" in uri:
    dirs.append(os.path.dirname(os.path.abspath(uri.split(":///", 1)[1].split("?")[0])))
print(" ".join(sorted(set(dirs))))
sys.path.insert(0, sys.argv[2])
import core                                     # server_paths: the paths of a server, defaults included
files = set()
for sv in cfg.get("servers") or []:
    if "name" not in sv or sv.get("disabled"):
        continue
    sp = core.server_paths(sv)
    files.update(sp[k] for k in ("artifacts", "checkpoints", "sync_artifacts", "sync_checkpoints") if os.path.isdir(sp[k]))
files = sorted(files)
print(" ".join(f for f in files if not any(f != g and f.startswith(g.rstrip("/") + "/") for g in files)))
EOF
)
image="${LAUNCHER_IMAGE:-${conf[0]:-}}"
snapshot="${conf[1]:-}"
volumes=()
for v in ${conf[2]:-} ${LAUNCHER_VOLUMES:-}; do volumes+=(--volume "$v:$v"); done      # LAUNCHER_VOLUMES: more folders
for v in ${conf[3]:-}; do volumes+=(--volume "$v:$v:ro"); done

if ! $container || [[ -z "$image" ]]; then
    cd "$root"
    XP_ROOT="$root" exec "$py" "$handler/app.py" --config "$data/experiments.yaml" "${args[@]}"
fi

if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "no image $image here: docker pull $image (or start.sh --no-container)" >&2
    exit 1
fi

snap="$state/snapshot/$(basename "$root")"
if running; then echo "replacing the running $name"; docker stop --time 20 "$name" >/dev/null; fi
docker rm --force "$name" >/dev/null 2>&1 || true          # left by a launcher that stopped or was killed
stop_relay

# the copy of the project, with the launcher's code, and tools/experiments: the data folder itself. Started from that
# copy (a terminal on the server, where the project itself is not reachable): it is used as it is
if [[ "$(cd "$root" && pwd -P)" == "$(mkdir -p "$snap" && cd "$snap" && pwd -P)" ]]; then
    echo "using the last copy of the project ($snap, of $(date -r "$snap/tools/experiments-handler" '+%Y-%m-%d %H:%M')): start from the project to refresh it"
else
echo "copying the project to $snap..."
mkdir -p "$snap/tools"
skip=(--exclude __pycache__ --exclude .mypy_cache)
if [[ -n "$snapshot" ]]; then
    for item in $snapshot; do
        if [[ -e "$root/$item" ]]; then
            mkdir -p "$snap/$(dirname "$item")"
            rsync -a --delete "${skip[@]}" "$root/$item" "$snap/$(dirname "$item")/"
        fi
    done
else
    rsync -a --delete "${skip[@]}" --filter=':- .gitignore' \
        --exclude /tools/experiments --exclude /tools/experiments.old --exclude /tools/experiments-handler "$root/" "$snap/"
fi
rsync -a --delete "${skip[@]}" "$handler" "$snap/tools/"
ln -sfn "$data" "$snap/tools/experiments"
fi

# the container's home, the docker command
mkdir -p -m 700 "$state/home/.ssh"
for f in known_hosts config; do
    if [[ -f "$HOME/.ssh/$f" ]]; then install -m 600 "$HOME/.ssh/$f" "$state/home/.ssh/$f"; fi
done
mkdir -p "$state/bin"
install -m 755 "$(command -v docker)" "$state/bin/docker"
# ssh reads ~/.ssh from the home of /etc/passwd, not from $HOME: the container gets a passwd whose home is home/
mkdir -p -m 700 "$state/run"
chmod 700 "$state/run"
awk -F: -v u="$(id -u)" '$3 != u' /etc/passwd > "$state/run/passwd"
echo "$(id -un):x:$(id -u):$(id -g)::$state/home:/bin/bash" >> "$state/run/passwd"
sock=/var/run/docker.sock
[[ -S /var/run/docker-host.sock ]] && sock=/var/run/docker-host.sock    # the host's socket, as a dev container sees it
docker_gid=$(stat -c %g "$sock")

# the ssh agent, lent through a socket in run/ (the relay starts once the container exists, and stops with it)
agent=() lent=""
if [[ -n "${SSH_AUTH_SOCK:-}" && -S "$SSH_AUTH_SOCK" ]]; then
    lent="$state/run/ssh-agent-$name.sock"
    agent=(-e "SSH_AUTH_SOCK=$lent")
else
    echo "no ssh agent (SSH_AUTH_SOCK): the launcher will not be able to connect to the servers" >&2
fi

mode=(--detach)
if $attach; then
    mode=(--rm)
    if [[ -t 0 && -t 1 ]]; then mode+=(-it); fi
    trap 'docker rm --force "$name" >/dev/null 2>&1 || true; stop_relay' EXIT
    (until docker inspect "$name" >/dev/null 2>&1; do sleep 0.2; done; start_relay) &
fi

gpus=()
if [[ "${LAUNCHER_GPUS:-auto}" != none ]] && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
    gpus=(--gpus all)
fi

out=/dev/null                                               # detached: docker prints only the container id
if $attach; then out=/dev/stdout; fi
echo "launcher in a container of $image ($name)"
# detached: no --rm, so that "start.sh logs" still shows why it stopped (the next start removes it)
docker run "${mode[@]}" --init --pull never --name "$name" --publish "$bind:$port:8000" "${gpus[@]}" \
    --user "$(id -u):$(id -g)" --group-add "$docker_gid" \
    --volume "$state/run/passwd:/etc/passwd:ro" --volume /etc/group:/etc/group:ro \
    --volume "$data:$data" --volume /var/run/docker.sock:/var/run/docker.sock \
    "${volumes[@]}" \
    -e HOME="$state/home" -e XP_ROOT="$snap" -e LAUNCHER_IMAGE="$image" -e LAUNCHER_BIN="$state/bin" "${agent[@]}" \
    "$image" /usr/bin/bash -lc 'export PATH="$LAUNCHER_BIN:$PATH"; cd "$XP_ROOT" && exec python tools/experiments-handler/app.py "$@"' \
    launcher --config "$data/experiments.yaml" --host 0.0.0.0 --port 8000 "${cargs[@]}" > "$out"
$attach && exit 0

start_relay
for _ in $(seq 60); do
    if ! running; then
        echo "the launcher stopped:" >&2; docker logs --tail 30 "$name" >&2; exit 1
    fi
    if docker logs "$name" 2>&1 | grep -q "Running on"; then break; fi
    sleep 1
done
echo "running in the background, published on $bind:$port of this server  (start.sh logs | status | stop)"
if [[ "$bind" == 127.0.0.1 ]]; then
    echo "from your machine: ssh -L $port:127.0.0.1:$port <this server>, then http://127.0.0.1:$port"
else
    echo "open to the network: anyone who reaches this port can use the launcher (LAUNCHER_BIND=127.0.0.1 to restrict)"
fi
