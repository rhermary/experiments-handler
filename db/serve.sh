#!/usr/bin/env bash
# Inside the database container (started by launch.sh): Postgres on its unix socket, then the MLflow UI on port 5000.
# Stopping the container (docker container stop) stops the MLflow UI, then Postgres cleanly.
# Environment: POSTGRES_DATA, POSTGRES_HOST (socket folder), POSTGRES_PORT, POSTGRES_USER, POSTGRES_DB, ALLOW_MODIF.
set -u

PG_BIN=$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -n 1)
"$PG_BIN/pg_ctl" -o "-k $POSTGRES_HOST -p $POSTGRES_PORT" -D "$POSTGRES_DATA" -w start || exit 1

PUBLIC_USER=$POSTGRES_USER
if [[ "$ALLOW_MODIF" != true ]]; then
    # the MLflow UI reads only: a user with SELECT rights
    PUBLIC_USER=mlflow_server
    psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
REVOKE ALL ON DATABASE $POSTGRES_DB FROM PUBLIC;
DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$PUBLIC_USER') THEN CREATE USER $PUBLIC_USER; END IF;
END \$\$;
GRANT CONNECT ON DATABASE $POSTGRES_DB TO $PUBLIC_USER;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO $PUBLIC_USER;
SQL
fi

mlflow server --host 0.0.0.0 --port 5000 --cors-allowed-origins "*" \
    --backend-store-uri "postgresql:///$POSTGRES_DB?port=$POSTGRES_PORT&host=$POSTGRES_HOST&user=$PUBLIC_USER" &
MLFLOW_PID=$!

stop() {
    echo "Stopping the MLflow UI and Postgres..."
    kill "$MLFLOW_PID" 2>/dev/null
    wait "$MLFLOW_PID" 2>/dev/null
    "$PG_BIN/pg_ctl" -D "$POSTGRES_DATA" -m fast stop
    exit 0
}
trap stop SIGTERM SIGINT
wait "$MLFLOW_PID"
stop
