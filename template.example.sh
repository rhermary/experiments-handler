#!/usr/bin/env bash
# Example script template of experiments-handler: a docker container runs the command loop at a given commit.
# The placeholders (words between two double @) are filled by the app; keep them. Copy this file as template.sh next to your settings
# (or edit it in the page: Template), and adapt the docker options, mounts and copied folders to your project.
@@HEADER@@

# Run from the project root
cd @@WORKDIR@@ || exit 1

# Variables of the script (docker / generic, run, and variables that are not passed to the container)
@@SHELL_VARS@@

# Environment of the container (process variables, and docker-side variables flagged with -e)
@@CONTAINER_ENV@@

# Outside debug mode, the container checks out the commit of the XP first
PROCESS_GIT=$([[ $DEBUG -eq 0 ]] && echo "git stash >> /dev/null && git checkout $COMMIT && " || echo "")

# Command run in the container
CMD="$PROCESS_GIT  @@LOOP@@"

CONTAINER_ID=$(\
    docker create\
        --rm -it\
        --gpus all\
        --user $(id -u)\
        --workdir /app\
        "${CONTAINER_ENV[@]}"\
        $IMAGE_NAME bash -lc "$CMD"\
)

# The project, as the container sees it
docker cp . $CONTAINER_ID:/app/
# base configs written by the app (<scripts_dir>/<XP>/configs, not in git), when the XP uses one
@@COPY_FILES@@

# keep the output of the job after it ends (Jobs > Past jobs; before docker start); nothing when the line is removed
@@KEEP_LOGS@@
docker start $([[ $DEBUG -eq 1 ]] && echo "-ia") $CONTAINER_ID
