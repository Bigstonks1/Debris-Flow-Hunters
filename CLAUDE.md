# CLAUDE.md

Orientation for AI coding agents (Claude Code and others) working in this repository.

## Read these first

| File | What it gives you |
|---|---|
| `design.md` | The system: architecture (§2), the click-by-click end-to-end workflow with every data source (§3), inputs and fallbacks (§4), model formulas (§5), a file-by-file code map (§6), the watershed split (§7), how results are assessed (§8.0) and every result with its source (§8), verified traps (§9) and the open-items checklist (§10). |
| `README.md` | What the project is, install, run (for humans). Keep it in sync when commands change. |

Answer "how does X work" from `design.md` §3 and §6 before searching the code.

## What this is

A local web app and pipeline that runs the USGS post-fire debris-flow models for any US
wildfire: name or Unique Fire ID in, mapped and exported stream-segment hazards out,
validated against USGS's own assessment. Branch `staging` is the pre-production branch:
only what the app needs. Research scripts (resolution sweeps, scoring against observed
flows, Graber models, Trail Creek case study) live on `wapiti-e2e`, not here.

## Commands

```bash
uv sync                                                  # install (Python 3.12, pfdf, wildcat)
uv run python app/server.py                              # app at http://127.0.0.1:8000
uv run python app/server.py --runner slurm               # app submitting runs to Slurm (on the cluster)
uv run python pipeline/run_fire.py --id 2025-IDPAF-005844   # one fire end to end, by Unique Fire ID
uv run python pipeline/run_fire.py "Rush" 2025 ID        # by name, year, state
uv run python pipeline/run_fire.py --id <UID> --split --workers 4   # wildcat by watershed pieces
uv run python pipeline/usgs_check.py fires/<key>.json [data/<project>]   # compare with USGS
uv run python pipeline/split_fire.py all data/<project>  # split: plan, build, run, merge, compare
uv run python pipeline/run_wildcat.py data/<project> [preprocess|assess|export]   # wildcat by hand
uv run python map/build_map.py                           # offline map: map/dist/debris_flow_map.html
sbatch --export=ALL,FIRE_ID=<UID> slurm/run_fire.slurm   # one fire on Slurm
sbatch --export=ALL,CONFIG=fires/<key>.json,RES=1 slurm/split_prepare.slurm   # 1 m LiDAR as an array
```

On Windows without `uv run`: `.venv/Scripts/python.exe`. No test, lint or build tooling
exists yet; if you add tests, add the runner to `pyproject.toml` and list it here.

## Rules that are easy to break

- **Never run `wildcat assess` from the CLI.** pysheds 0.4 calls `np.in1d`, removed in
  NumPy 2. Run wildcat through `pipeline/measure.py` (`run_stage`) or
  `pipeline/run_wildcat.py`, which install the shim first.
- **Do not reimplement or patch wildcat/pfdf.** They are the USGS model; this repo drives
  them. Behaviour is set by each project's `configuration.py` (template:
  `pipeline/wildcat_config_template.py`; defaults in `.venv/Lib/site-packages/wildcat/_utils/_defaults/`).
  Most "code changes" are config values. Read the installed source instead of guessing API
  shapes: `.venv/Lib/site-packages/pfdf/`, `.venv/Lib/site-packages/wildcat/`.
- **pfdf and wildcat come from the USGS package index** declared in `pyproject.toml`; plain
  `pip install` will not find them.
- **Key fires by Unique Fire ID**, never by name (names repeat). Folder names come from
  `fires.fire_key()`; match data folders to the *longest* key they start with.
- **`data/` is never committed** and can be large (0.1–4 GB per fire). `fires/*.json` is
  written by the pipeline; do not hand-edit keys the pipeline writes.
- **Every stage is a subprocess** writing to `data/`; `run_fire.py` stage banners
  (`===== name =====`) are parsed by `app/server.py` (`STAGES`). Keep them when editing.
- **Slurm scripts end in `.slurm`** and assume the repo at `~/scratch/Debris-Flow-Hunters`.
- More traps (DEM projection, nodata labels, WFIGS geometry, Windows GDAL hangs): `design.md` §9.

## Project conventions

- Branch per piece of work; do not commit or push unless the user asks.
- Benchmarks are deliverables: when you run something, keep the numbers and put them in
  `design.md` §8 with their source, what, why and how.
- Report negative results straight (e.g. 1 m LiDAR costs ~100× for no accuracy gain).
- The users are land and emergency managers: plain language in anything user-facing.
