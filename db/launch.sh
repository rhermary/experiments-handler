#!/usr/bin/env bash
# Start a Postgres database and its MLflow UI in a container, on this server (copied here by experiments-handler).
# The container runs serve.sh (next to this file): Postgres on its unix socket, then the MLflow UI on --ui-port.
#
#   launch.sh --image IMAGE --name NAME --folder DATA_FOLDER --socket SOCKET_DIR --port PG_PORT --ui-port UI_PORT
#             [--allow-modif] [--label own|sync] [--files DIR]...
#
#   --allow-modif   the MLflow UI connects as the database owner (it can change runs); else as a read-only user
#   --files DIR     a folder holding the runs' artifacts, mounted read-only so the MLflow UI can show them (repeatable)
set -euo pipefail

image="" name="" folder="" socket="" port="" ui_port="" allow_modif=false label=own files=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --image) image=$2; shift ;;
        --name) name=$2; shift ;;
        --folder) folder=$2; shift ;;
        --socket) socket=$2; shift ;;
        --port) port=$2; shift ;;
        --ui-port) ui_port=$2; shift ;;
        --label) label=$2; shift ;;
        --allow-modif) allow_modif=true ;;
        --files) files+=(--volume "$2:$2:ro"); shift ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
    shift
done
for v in image name folder socket port ui_port; do
    [[ -n "${!v}" ]] || { echo "missing --${v//_/-}" >&2; exit 1; }
done
[[ -d "$folder" ]] || { echo "no database folder $folder" >&2; exit 1; }

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$socket"

# a socket left by a Postgres that was killed: nothing answers on it, remove it (and its lock file)
sock_file="$socket/.s.PGSQL.$port"
if [[ -S "$sock_file" ]]; then
    if python3 -c 'import socket,sys; c=socket.socket(socket.AF_UNIX); c.settimeout(3); c.connect(sys.argv[1])' "$sock_file" 2>/dev/null; then
        echo "a Postgres already answers on $sock_file: not starting another one" >&2; exit 1
    fi
    echo "removing the stale socket $sock_file and its lock file (nothing answers on it)"
    rm -f "$sock_file" "$sock_file.lock"
fi

docker run --detach --rm --pull never \
    --name "$name" --label "launcher.db=$label" \
    --user "$(id -u)" \
    --volume /etc/passwd:/etc/passwd:ro --volume /etc/group:/etc/group:ro \
    "${files[@]}" \
    --volume "$folder:$folder" --volume "$socket:$socket" --volume "$here:$here:ro" \
    --publish "$ui_port:5000" \
    -e POSTGRES_DATA="$folder" -e POSTGRES_HOST="$socket" -e POSTGRES_PORT="$port" \
    -e POSTGRES_USER="$(id -un)" -e POSTGRES_DB=mlruns_db -e ALLOW_MODIF="$allow_modif" \
    "$image" /usr/bin/bash -l "$here/serve.sh"
