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
                                                         # batches to any cluster: Settings > HPC in the app
uv run python pipeline/run_fire.py --id 2025-IDPAF-005844   # one fire end to end, by Unique Fire ID
uv run python pipeline/run_fire.py "Rush" 2025 ID        # by name, year, state
uv run python pipeline/run_fire.py --id <UID> --cold     # delete the fire's cached downloads first
uv run python pipeline/run_fire.py --id <UID> --split --workers 4   # wildcat by watershed pieces
uv run python pipeline/run_fire.py --id <UID> --kf-fill none        # KF gaps left empty, as USGS does (default: SSURGO, then median)
uv run python pipeline/run_fire.py --id <UID> --no-baer-sbs         # skip the Forest Service BAER severity map
DFH_DEM_SOURCE=service uv run python pipeline/run_fire.py --id <UID>   # force the DEM fallback (3DEP image service), for testing
uv run python pipeline/sentinel2_dnbr.py fires/<key>.json --sensor landsat   # dNBR from Landsat 8/9 only (comparison)
DFH_DNBR_SENSOR=landsat DFH_DNBR_METHOD=composite uv run python pipeline/run_fire.py fires/<key>.json   # force a dNBR fallback end to end (testing)
uv run python pipeline/usgs_check.py fires/<key>.json [data/<project>]   # compare with USGS
uv run python pipeline/batch.py all fires.txt --runner local --workers 3   # many fires on this computer
python pipeline/batch.py all fires.txt --runner slurm [--wait]   # on Lemhi's login node: inputs here, model on compute nodes
python pipeline/batch.py finish <batch>                  # after the array: USGS comparison + summary.csv
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
- **Lemhi's compute nodes have no internet** (no DNS, no route). Anything that downloads
  (`run_fire.py --phase inputs` and `--phase usgs`) runs on the login node; compute jobs run
  only `--phase wildcat`. Keep new network calls out of the wildcat phase.
- **Batches resolve fire configs serially, then run fires in parallel** (`pipeline/batch.py`).
  Never create `fires/*.json` from parallel runs: two same-named fires could collide.
- **numba's compiled-code cache must not be built by several processes at once**: it is
  corrupted ("KeyError: int64 generator(...)" in assess). Parallel local runs start with one
  run alone; Slurm scripts give each job its own `NUMBA_CACHE_DIR`. Deleting `__pycache__`
  folders inside `.venv` empties that cache.
- **wildcat's outlets file is not in basin order** (it removes nested drainages between saving
  basins and outlets). Pair outlets with basins by location: an outlet is the downstream end
  of its basin's terminal segment (`Segment_ID`).
- **Every stage is a subprocess** writing to `data/`; `run_fire.py` stage banners
  (`===== name =====`) are parsed by `app/server.py` (`STAGES`). Keep them when editing.
- **Slurm scripts end in `.slurm`** and run in the folder they were submitted from
  (`SLURM_SUBMIT_DIR`, default `~/scratch/Debris-Flow-Hunters`).
- **HPC credentials never go in files.** The app's cluster password lives in the OS
  password store via `keyring` (`app/hpc.py`; Credential Manager, Keychain or Secret
  Service); `data/hpc_settings.json` holds no secrets and API responses only say whether
  one is saved. Keep it that way, and keep anything typed
  in the page out of remote shell commands unless validated or `shlex.quote`d.
- **App batches read `pipeline/batch.py`'s files** (`fires.tsv`, `status/*.json`), locally
  and over SFTP. Changing their format breaks the app's Batches panel and HPC copy-back.
- **Every input source must be free and need no account, key or email** (`design.md` §4.4).
  Earth Search's Landsat files are requester-pays: Landsat comes from the Planetary Computer.
- **Inputs fall back in a fixed order** (`design.md` §4 table): USGS-published burn inputs,
  then the Forest Service BAER map (`pipeline/baer_sbs.py`), then estimated severity;
  Sentinel-2 single dates, composite, then Landsat (`pipeline/sentinel2_dnbr.py`); STATSGO KF,
  then SSURGO (`pipeline/soils.py`), then the fire median; TNM S3 DEM, then the 3DEP image
  service. Every choice is written to the project's `sources.json`, which the app shows.
  Never fall back silently.
- **Project kinds live in one list**, `fires.PROJECT_SUFFIXES` (`_usgs_usgs`,
  `_s2_auto_baersbs`, `_s2_auto_estimate`, in order of preference) with
  `fires.finished_project()`. Add a new kind there, not in each caller.
- **STATSGO KF gaps are 0 (sometimes -0.1), not nodata**, and SSURGO is on a different
  scale (Wapiti 0.74x STATSGO): test `kf > 0` and rescale before mixing (`prepare_fire.fill_kf`).
- **A perimeter crossing the border is clipped** to where the DEM has data
  (`prepare_fire.main`); fires inside US coverage are untouched. The run stops only when no
  part of the perimeter has elevation.
- **Dates are refreshed from WFIGS on every run** (`recover.py`); `"dates_locked": true` in a
  config keeps hand-set dates. Other per-fire downloads stay cached until `--cold`.
- **Test switches** force fallbacks end to end: `DFH_DEM_SOURCE=service`,
  `DFH_DNBR_SENSOR=sentinel2|landsat`, `DFH_DNBR_METHOD=single|composite`.
- More traps (DEM projection, nodata labels, WFIGS geometry, Windows GDAL hangs, Soil Data
  Access quirks): `design.md` §9.

## Project conventions

- Branch per piece of work; do not commit or push unless the user asks.
- Benchmarks are deliverables: when you run something, keep the numbers and put them in
  `design.md` §8 with their source, what, why and how.
- Report negative results straight (e.g. 1 m LiDAR costs ~100× for no accuracy gain).
- The users are land and emergency managers: plain language in anything user-facing.
