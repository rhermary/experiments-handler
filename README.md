# Athanor

*(repository: experiments-handler)*

A small web app to build, launch and analyse the experiments of a Python research project configured with
[jsonargparse](https://jsonargparse.readthedocs.io) and tracked with [MLflow](https://mlflow.org).

- **Launcher**: an *XP* is a list of *blocks*; each block has *options* that set environment variables
  (`APP_TASK__INIT_ARGS__LR=0.001`). The variables are shared by the XPs, but what you change in an XP stays in it:
  a value typed in a field is saved with the XP (*reset* drops it), and Edit / Delete / + Add variable (and the
  argument browser) change the selected XP only, unless *every XP* is chosen in Edit; such variables are tagged
  *edited for this XP* or *this XP only*. Picking one option per block gives a shell script, generated from a template
  (typically: a docker container that checks out a commit and runs a command loop). The page shows the config the
  program will get, checks every variable against the classes of the source folder (ignored, overwritten, missing
  arguments), compares two selections (Diff), and can write base configs from scratch with an argument browser.
  A *list* block holds values (run ids, window sizes...): pick one or several, or loop over all of them around the
  command loop. Every view has its own address (bookmark it, open it in another tab).
- **Results**: the MLflow runs of an XP, labelled by *dimensions* (from the blocks, or MLflow filters), shown as a table,
  dot plots and a list of runs, with a report script (LaTeX table, PDF figures) that queries MLflow itself. Several
  *selections* per XP (filter, values kept, rows and columns), each with its own *views* and a text explaining each
  view: *training curves* (metric histories averaged per group), or an *artifact* view, one file the runs logged
  (chosen among their artifacts) shown for each run side by side, one row per value of a dimension: a PDF as its first
  page (Ghostscript; click for the whole file), an image, or a text file. Runs that use another run (e.g. a checkpoint) are linked to it, and what differs between those
  runs becomes dimensions. The Runs view shows the runs the filter leaves out (and why), and the block options that no
  run matches.
- **Run on** (next to Launch): this machine, or one of your servers (connected in Servers). On a server, the launch is
  a logged action: it stops at once when the image (`IMAGE_NAME`) is not on that server (it is never pulled: pull it
  yourself); the project is copied to `<launcher>/repo` there when it changed since the last copy (a
  fingerprint of its files; what is copied: `project.snapshot`, else everything but what `.gitignore` excludes), with
  the XP's base configs; the script is written to `<launcher>/launches/<time>_<XP>/run.sh`, runs from that copy,
  and keeps its output in `output.log` next to it. One launch at a time per server; `DEBUG=1` only here.
- **Queue**: "Add to queue" writes the script of the current selection at once, for where "Run on" points; the queue
  starts the items one after the other, each when enough GPUs are free (or when you start them, on the GPUs you
  pick), and only sets the devices variable (`…DEVICES`) then. Each server has its own line (the GPUs of another
  server are read over ssh). It runs in the app (kept in `.queue.json` next to the settings).
- **Servers** and **Databases**: your GPU servers (add, order, paths of each), one ssh connection per server opened
  when you click Connect, their state (Postgres, GPUs, disk), and the Postgres database of each server (start on a
  dated folder, stop, new dated database). Every action shows its commands before running them, and is logged with
  its output. Nothing connects or starts by itself.
- **Sync**: the runs of each server's database copied into the synchronised database of the central server, when you
  click, in two separate syncs. A preview (read-only) shows per experiment the new runs, the runs changed since their
  last sync, the checkpoints missing on the central server, and their sizes. *Results*: the artifacts (tar | tar
  through this machine), then the rows (one transaction), then both are checked. *Checkpoints*: the checkpoint folders
  of the runs already synchronised, then checked. Experiments are matched by name (their ids differ between
  databases); runs on older artifact paths are left alone; nothing is deleted.
- **MLflow** UI in a tab, and the **GPU** usage of your servers (ssh). The MLflow server runs in a Docker container
  (Settings: *MLflow server image*; empty: the Docker image variable; `local`: the launcher's own mlflow), so its
  MLflow matches the databases; the Results tab reads the runs through it. An image whose MLflow writes another schema
  than the database's is refused (MLflow would try to migrate the database).
- **Local | Synchronised** (Results and MLflow tabs): read the runs from the tracking URI of the Settings, or from
  the synchronised database of the central server (a tunnel to its Postgres, opened when you choose it; the central
  server must be connected and its synchronised database started).

## Screenshots

The screenshots show a made-up demo project (`model-comparison`: two datasets, three models, two learning rates).

**Launcher**: blocks of options, a list looped over, the command loop as it runs, the variables (only the ones used by
the selection are shown).

![Launcher](docs/screenshots/launcher.png)

![Run and variables](docs/screenshots/run-and-variables.png)

**Diff** of two selections: the variables and the config the program gets.

![Diff](docs/screenshots/diff.png)

**Results**: the data of a selection, how it is arranged, and the dot plot (left: computed in the page; right: the
report script that makes the PDF figures).

![Results: plot](docs/screenshots/results-plot.png)

| Table | Training curves |
|---|---|
| ![Results: table](docs/screenshots/results-table.png) | ![Results: training curves](docs/screenshots/results-curves.png) |

**Runs**: every run behind the table, grouped by configuration, with the block options that no run matches.

![Results: runs](docs/screenshots/results-runs.png)

**Queue**: items waiting for free GPUs, or started by hand on the GPUs you pick.

![Queue](docs/screenshots/queue.png)

**Servers** and **Databases** (the central server keeps the synchronised database).

![Servers](docs/screenshots/servers.png)

![Databases](docs/screenshots/databases.png)

## Run

Checked out as `<project>/tools/experiments-handler`, set up once, then start it:

```bash
tools/experiments-handler/init.sh --data /data/me/launcher/myproject --image me/myproject:1.0
tools/experiments-handler/start.sh          # in the background: http://<server>:5925 (logs | status | stop)
```

- `init.sh` creates the data folder (settings from `defaults.yaml`, `template.sh` from `template.example.sh`,
  `.gitignore`) and links `<project>/tools/experiments` to it; `--move` moves an existing `tools/experiments` there.
  Run it again at any time: it only creates what is missing. `--help` for the options.
- `start.sh` runs the app in a Docker container of the project's image (`--image`, or `project.mlflow_image` in the
  settings), so its MLflow is the one that wrote the databases. The project may be on a mount the host's Docker
  does not see (sshfs in a dev container): it copies the project into `<data>/.launcher/snapshot/` at each start
  (`project.snapshot`: what to copy; empty: everything but what `.gitignore` files exclude), lends your ssh agent to
  the container, and publishes its port like the database containers (`LAUNCHER_PORT`, `LAUNCHER_BIND`). The data
  folder must be on a disk the host shares at the same path, where unix sockets can be created. The header of
  `start.sh` describes it all; `--attach` keeps it in the terminal, `--no-container` runs it with this machine's
  python.

Without the scripts:

```bash
pip install -r requirements.txt
python app.py --config path/to/your/project/tools/experiments/experiments.yaml   # http://127.0.0.1:8000
```

- `--config` (or `XP_CONFIG`): the settings of your project. Created from `defaults.yaml` when missing. Keep it in your
  project (and in its git): the page edits it, the previous version is kept as `experiments.yaml.bak`.
- `--template` (or `XP_TEMPLATE`): the script template; default `template.sh` next to the settings, else
  `template.example.sh`. Edit it in the page (Template) or by hand.
- `project.root` in the settings: the project the scripts run in, absolute or relative to the settings file.
- Files the app writes for itself (`.gpu-servers.json`, `.mlflow-server.log`, `.queue.json`, backups, and the action
  log: `actions.log` and `actions/<time>_<action>_<server>/commands.log`) go next to the settings.
  An optional `userlist.csv` there (passwd format) names the owners of GPU processes on servers whose users this
  machine does not know.
- Generated scripts go to `<project>/<scripts_dir>/<XP>/`, base configs written by the app to
  `<project>/<scripts_dir>/<XP>/configs/` (copied into the container by `@@COPY_FILES@@`).

**Security**: the app writes and runs shell scripts, and runs the report scripts it generates; nothing asks who you
are. `app.py` listens on 127.0.0.1 by default; use `--host 0.0.0.0` only on a network you trust. `start.sh`
publishes it on every interface of the server (`LAUNCHER_BIND=127.0.0.1` to restrict it), and its container has your
ssh agent and the host's Docker socket.

Optional: `latex` and `dvisvgm` to show the LaTeX tables compiled; a LaTeX install with `LM Roman` for the PDF figures.

## Conventions it assumes

- Configs are jsonargparse YAML (`class_path` / `init_args`); environment variables `<env_prefix><PATH>` with
  `<env_sep>` override any field (`APP_TASK__INIT_ARGS__BATCH_SIZE`), as in a parser that reads them.
- A `subconfig:` key merges another YAML file (path relative to the file); a variable ending in `SUBCONFIG` replaces it.
- The root config has `task` (a task class, whose `links()` may fill arguments) and `settings`.
- Default metrics of the Results tab are looked for as `test/<metric>` (AUROC, AUPR, F1, ...); add any metric in the page.

## Files

| | |
|---|---|
| `app.py` | Flask app and JSON API |
| `core.py` | settings (load, normalise, save), variables of a selection, script rendering |
| `preview.py`, `checker.py`, `picker.py`, `indexer.py` | config preview, variable checks, argument browser (static scan of the source folder) |
| `xpconfigs.py` | base configs written by the app, XPs imported from MLflow runs |
| `results.py`, `report_template.py` | Results tab; `report_template.py` is also the code of the generated report scripts |
| `diffview.py`, `gpus.py`, `mlflow_ui.py` | Diff of two selections, GPU view, MLflow server (in a container) and its proxy |
| `remote.py`, `actions.py` | servers and their databases over ssh; every action as a list of commands, shown first and logged |
| `sync.py` | sync of a server's runs, artifacts and checkpoints into the central server's synchronised database |
| `remote_launch.py` | launches on another server: the copy of the project there, the script, its output |
| `sources.py` | the database read by the Results and MLflow tabs: local, or the synchronised one (its tunnel) |
| `db/launch.sh`, `db/serve.sh` | a Postgres database and its MLflow UI in a container: copied to `<launcher>/db` on the server and run by Databases > Start (image chosen at each start) |
| `xpqueue.py` | queue of experiments (frozen scripts, started when their GPUs are free) |
| `*.js`, `app.css`, `index.html` | the page (no build step) |
| `defaults.yaml`, `template.example.sh` | example settings and script template |
| `init.sh`, `start.sh`, `agent_relay.py` | set up the data folder of a project; start the app in a container (with the relay that lends it your ssh agent) |
