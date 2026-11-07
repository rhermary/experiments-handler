# experiments-handler

A small web app to build, launch and analyse the experiments of a Python research project configured with
[jsonargparse](https://jsonargparse.readthedocs.io) and tracked with [MLflow](https://mlflow.org).

- **Launcher**: an *XP* is a list of *blocks*; each block has *options* that set environment variables
  (`APP_TASK__INIT_ARGS__LR=0.001`). Picking one option per block gives a shell script, generated from a template
  (typically: a docker container that checks out a commit and runs a command loop). The page shows the config the
  program will get, checks every variable against the classes of the source folder (ignored, overwritten, missing
  arguments), compares two selections (Diff), and can write base configs from scratch with an argument browser.
- **Results**: the MLflow runs of an XP, labelled by *dimensions* (from the blocks, or MLflow filters), shown as a table,
  dot plots and a list of runs, with a report script (LaTeX table, PDF figures) that queries MLflow itself. Runs that
  use another run (e.g. a checkpoint) are linked to it, and what differs between those runs can be used as dimensions.
  Training curves and other views, several selections per XP.
- **MLflow** UI in a tab, and the **GPU** usage of your servers (ssh).

## Run

```bash
pip install -r requirements.txt
python app.py --config path/to/your/project/tools/experiments/experiments.yaml   # http://127.0.0.1:8000
```

- `--config` (or `XP_CONFIG`): the settings of your project. Created from `defaults.yaml` when missing. Keep it in your
  project (and in its git): the page edits it, the previous version is kept as `experiments.yaml.bak`.
- `--template` (or `XP_TEMPLATE`): the script template; default `template.sh` next to the settings, else
  `template.example.sh`. Edit it in the page (Template) or by hand.
- `project.root` in the settings: the project the scripts run in, absolute or relative to the settings file.
- Files the app writes for itself (`.gpu-servers.json`, `.mlflow-server.log`, backups) go next to the settings.
  An optional `userlist.csv` there (passwd format) names the owners of GPU processes on servers whose users this
  machine does not know.
- Generated scripts go to `<project>/<scripts_dir>/<XP>/`, base configs written by the app to
  `<project>/<scripts_dir>/<XP>/configs/` (copied into the container by `@@COPY_FILES@@`).

**Security**: the app writes and runs shell scripts, and runs the report scripts it generates. It listens on
127.0.0.1 by default; use `--host 0.0.0.0` only on a network you trust.

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
| `diffview.py`, `gpus.py`, `mlflow_ui.py` | Diff of two selections, GPU view, MLflow UI proxy |
| `*.js`, `app.css`, `index.html` | the page (no build step) |
| `defaults.yaml`, `template.example.sh` | example settings and script template |
