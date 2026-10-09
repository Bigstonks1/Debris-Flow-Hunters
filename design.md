# Debris Flow Hunters: design

R-CON 2026, Challenge 01 (Wildfire Resilience), Problem Statement 2. Showcase 2026-10-13.

This document is the reference for how the system works and what it has found. For
installing and running it, see `README.md`; for working in the code as an AI agent, see
`CLAUDE.md`.

Contents: 1 Goal · 2 Architecture · 3 End-to-end workflow · 4 Inputs, sources, fallbacks ·
5 The models · 6 Code map · 7 Watershed split · 8 Results · 9 Things that will bite you ·
10 Open items

---

## 1. Goal

Compress a post-fire debris-flow hazard assessment from weeks to minutes, for any US fire,
and answer four research questions:

1. Does 1 m LiDAR terrain give a better answer than the standard 10 m DEM?
2. Does HPC (the Lemhi cluster) make it faster, or make otherwise impossible runs possible?
3. Can the whole pipeline run automatically and still agree with USGS and observed flows?
4. Can land and emergency managers use the result directly (map, storm slider, GIS export)?

Advisor framing (Keith): LiDAR *accuracy* matters more than resolution; the aim is a faster,
more accurate model, using HPC even with LiDAR; the users are land-management agencies and
potentially emergency management.

Short answers so far (section 8): 1 no; 2 yes for memory and throughput, not per-run speed;
3 yes, within ~5% of USGS when the burn-severity inputs match theirs; 4 working prototype.

---

## 2. Architecture

```
 Browser (map/template.html)                      Public data services (no accounts)
   Find / Run / Map / Export / Keep, Delete         WFIGS perimeters (NIFC ArcGIS)
        |  HTTP JSON on 127.0.0.1:8000               RECOVER archive (ISU GIS Center)
        v                                            USGS assessment service + ScienceBase
 app/server.py  --- subprocess or sbatch --->        USGS 3DEP DEM tiles (TNM, AWS S3)
   lookup, runs, payloads, exports,                  STATSGO soils (via pfdf)
   keep/delete/trash                                 LANDFIRE EVT ImageServer
        |                                            Sentinel-2 L2A (Earth Search, AWS)
        v                                                    ^
 pipeline/run_fire.py  (one fire, end to end) ---------------+
   recover -> usgs_inputs -> [sentinel2] -> prepare -> wildcat -> usgs check
        |                                   (wildcat = USGS tool built on pfdf)
        v
 data/<fire>_<inputs>/   wildcat project: inputs/, preprocessed/, exports/*.shp,
                         usgs_check.json, map_payload.json
        |
        v
 map/build_map.py  -> map layers for the app, or one offline HTML (map/dist/)

 Lemhi HPC (Slurm): slurm/run_fire.slurm (one fire), slurm/split_*.slurm (1 m, job array)
```

- **Everything is local and file-based.** The app is Python standard library on
  127.0.0.1; fire state is `fires/<key>.json` (settings) plus `data/` (downloads and
  results). There is no database.
- **The model is not ours.** USGS's `wildcat` (a pipeline) on USGS's `pfdf` (a library)
  runs every model. This project finds, fetches and prepares the inputs, drives wildcat,
  validates the output, and presents it. It does not reimplement or modify wildcat.
- **Two seams remain outside:** getting runs onto Lemhi from the app (blocked on SSH
  access; runs go through Open OnDemand by hand), and publishing to ArcGIS Online (later).

---

## 3. End-to-end workflow

What happens, click by click, in the app. The same pipeline runs from the command line
(`pipeline/run_fire.py`) and on Slurm (`slurm/run_fire.slurm`); only the first two steps
are app-specific.

### 3.1 Find fire

- **What:** the user types a name and year (optional two-letter state), or a Unique Fire
  ID, and clicks **Find fire**.
- **How:** `GET /api/lookup?name=&year=&state=` or `?id=` → `app/server.py find()` →
  `pipeline/fires.py lookup()`, which queries the WFIGS Interagency Perimeters feature
  service: exact name within that year (and state) first, else names containing the text
  ("Big Bear" finds "BIG BEAR COMPLEX"); largest first; up to 25. Each result becomes a
  candidate card (`describe()`): name, county, state, acres, Unique Fire ID, discovery date,
  and "on map" if that ID is already assessed here.
- **Source:** WFIGS Interagency Perimeters, NIFC,
  `services3.arcgis.com/T4QMspbfLg3qTGWY/.../WFIGS_Interagency_Perimeters/FeatureServer/0`.
- **Why:** fire names are not unique (four 2025 fires are called "Rush"). The **Unique Fire
  Identifier** (`YEAR-UNIT-NUMBER`, e.g. `2025-IDPAF-005844`) is, so everything after this
  step is keyed by it. Inputs are validated before they reach the query (name characters,
  year, state code).

### 3.2 Pick and Run

- **What:** the user picks a card (a single match is picked automatically; picking fills
  the form) and clicks **Run assessment** (or **Show on map** if already assessed).
- **How:** `POST /api/runs {unique_id}`. The server re-reads the WFIGS record, refuses
  fires before 2016 (no Sentinel-2), shows an already-assessed fire instead of re-running
  it, and starts the run through a runner: `LocalRunner` (a child process, one at a time) or
  `SlurmRunner` (`sbatch slurm/run_fire.slurm`, up to 8 at once; state from `sacct`). The
  page polls `GET /api/runs/<id>` every 2 s; the server reads the run's log and maps its
  stage banners (`===== recover =====` ...) to the labels the user sees.
- **Why:** one local run at a time because a 10 m assessment of a large fire needs several
  GB; Slurm lifts that limit.

### 3.3 The pipeline (`pipeline/run_fire.py --id <UID>`)

Each stage is a separate process, so a stage's memory and GDAL state stay contained.

| # | Stage | Script | Source | What it produces | Why |
|---|---|---|---|---|---|
| 0 | config | `new_fire.py` | WFIGS record | `fires/<name>_<year>.json`: name, ID, state, county, ignition and containment dates, UTM zone from the perimeter centroid, WFIGS unit + local ID, whether a RECOVER bundle exists (HTTP HEAD) | Everything later reads this one file. An existing config for the same ID is reused untouched. |
| 1 | recover | `recover.py` | WFIGS (Esri JSON), RECOVER archive `giscenter-sl.isu.edu/aoc/aoc_research/recover2/archive/{year}/Fire_{year}_{unit}_{localid}.zip` | `data/recover/<key>/perimeter_wfigs.geojson`; if a bundle exists, its EVT and DEM members only, read with HTTP range requests (~50 MB of an 850 MB zip) | The final fire perimeter bounds everything. RECOVER (2022 on) supplies vegetation; its dNBR is token-protected, so not used. |
| 2 | usgs_inputs | `usgs_inputs.py` | USGS assessment service (`services.arcgis.com/v01gqwM5QqNysAAi/.../USGS_Post_Fire_Hazard_Assessment_Data`), ScienceBase item's `inputs.zip` | If USGS assessed this fire (≥ 50% perimeter overlap) and published inputs: `data/usgs/<code>/inputs/dnbr.tif`, `severity.tif`, recorded in the config | The best burn-severity inputs available: BAER-team (field-checked) or EROS BARC. Published with USGS's 2025+ assessments. |
| 3 | sentinel2 (only if no USGS inputs) | `sentinel2_dnbr.py` | Sentinel-2 L2A Collection 1, Earth Search STAC (`earth-search.aws.element84.com/v1`), COGs on AWS | `data/s2_dnbr/<key>/auto/dnbr.tif` (20 m) + `selection.json` (dates, tiles) | dNBR by rule when nobody has published one: pre and post scenes ≥ 98% clear, post date where burned area stops growing; see section 8.6. |
| 4 | prepare | `prepare_fire.py` | USGS 3DEP 1/3 arc-second DEM tiles from TNM S3 (`prd-tnm.s3.amazonaws.com`); STATSGO KF-factor via `pfdf.data.usgs.statsgo`; EVT from RECOVER, else LANDFIRE ImageServer (`lfps.usgs.gov`) | `data/<key>_usgs_usgs/` or `data/<key>_s2_auto_estimate/`: `inputs/` (perimeter, dem, dnbr, severity or none, kf, evt) on one UTM 10 m grid, and `configuration.py` from `pipeline/wildcat_config_template.py` | wildcat needs every raster on one projected grid. Without USGS severity, wildcat estimates it from dNBR (125/250/500). |
| 5 | wildcat | `measure.py` → `wildcat.preprocess/assess/export` | (the project) | `preprocessed/`, `exports/segments.shp`, `basins.shp`, `outlets.shp` (WGS 84); each stage's seconds and peak MB | The USGS models (section 5). Each stage in a fresh process so its memory is its own. With `--split`: `split_fire.py` instead (section 7). |
| 6 | usgs | `usgs_check.py` → `compare_usgs.py` | USGS assessment service; ScienceBase archive (2013–2024) as fallback | `<project>/usgs_check.json`; `usgs_reference` in the config | Validation: finds USGS's own assessment by location, downloads its segments, and matches streams within 20 m. Never fails a run. |

Timings: `data/<key>_run_timings.json`. Typical: 1–2 min for a small fire, ~4 min for
Wapiti (522 km²) with downloads.

### 3.4 Show on the map

- **How:** when the run finishes, the page fetches `GET /api/fires` (summaries of every
  `fires/*.json` with a finished project) and `GET /api/fires/<key>` (the fire's layers).
  `map/build_map.py fire_payload()` builds them from the project: stream segments
  (geometry + 11 fields: threshold, P/V/H at 24 mm/h, area, T, F, S, relief, burned
  area), perimeter, a 30 m hillshade from the preprocessed DEM and a burn-severity image.
  The payload is cached as `map_payload.json` until the exports change.
- **In the page:** the storm slider (5–80 mm/h) recomputes likelihood, volume and hazard
  for every stream in the browser from the stored T/F/S, area and relief (formulas in
  section 5); the views are storm likelihood, hazard, volume and M1 threshold; the popup
  shows a stream's numbers at the slider's storm; tiles show the median threshold, the USGS
  comparison, the burn-severity source (BAER / BARC / estimated) and the segment count.
  The M1 threshold legend is built per fire: its breaks are the sixths of that fire's
  thresholds, rounded to whole mm/h, so each class holds about a sixth of the streams. A
  fire with fewer than 12 thresholds uses fixed breaks (20/25/30/40/55). Method notes
  (formulas, caveats, view explanations) are left to the white paper, not the page.
- **Bottom of the side panel**, three sections:
  - **Configuration:** Settings: HPC cluster (3.8), Run a list of fires.
  - **Logs:**
    - Batches as a dropdown (3.8);
    - **Input data for this fire**: each layer from the project's `sources.json`, with the
      service it came from (`build_map.py input_services()`: WFIGS, TNM 3DEP, STATSGO on
      ScienceBase, LANDFIRE or RECOVER, Earth Search STAC or the USGS-published inputs on
      ScienceBase), plus the USGS assessment used for the comparison;
    - Deleted fires.
  - **Exports:** Download this fire (3.5).

  In the offline map only Logs shows, with only Input data in it.

### 3.5 Export, keep, delete

- **Export:** `GET /api/fires/<key>/export?format=geojson|csv|shp|gdb`: GeoJSON segments,
  CSV attributes, zipped shapefiles (segments, basins, outlets, perimeter), or a zipped Esri
  file geodatabase written by GDAL's OpenFileGDB driver (no ArcGIS). Built under a `.part`
  name and renamed, cached in `data/exports/<key>/`.
- **Keep / delete:** fires added in the app arrive **not kept** (`"kept": false`). Keep
  protects a fire. Delete (two clicks, only when not kept and not running) moves its config
  and data into `data/trash/<time>_<key>/` with a manifest; **Deleted fires** offers Restore
  or Delete permanently (the only step that removes files). Data folders are matched to
  the longest fire key they start with, so deleting `wapiti` never takes `wapiti_2026_*`.

### 3.6 On Lemhi (Slurm)

- One fire at 10 m: `sbatch --export=ALL,FIRE_ID=<UID> slurm/run_fire.slurm` (checks
  outbound HTTPS to every data host first; exits 3 naming the host if one is unreachable).
- One fire at 1 m LiDAR: `sbatch --export=ALL,CONFIG=fires/<fire>.json,RES=1
  slurm/split_prepare.slurm` → prepares inputs on the LiDAR grid, preprocesses, plans the
  pieces, then submits `split_pieces.slurm` as an array (each task builds and assesses one
  piece) and `split_merge.slurm` after it (merge + USGS check). Needs `recover_lidar` (the
  1 m file) in the config, and `baer_dir` for BAER inputs or `DNBR=usgs`.
- Access: SSH (since 2026-10-07) and Open OnDemand.
- **Lemhi's compute nodes have no internet** (checked 2026-10-08 from lemhi0042: no DNS, every
  data host unreachable; the login node reaches all of them). So anything that downloads
  runs on the login node: `run_fire.py --phase inputs` (perimeter, USGS inputs or
  Sentinel-2, DEM, soils, vegetation) and `--phase usgs` (the USGS comparison), and compute
  jobs run only `--phase wildcat`. Batches do this automatically (3.7). `run_fire.slurm` as
  written runs a whole fire and so needs compute-node internet (it exits 3 naming the host
  otherwise); on Lemhi run the inputs phase on the login node first. The 1 m split works
  when its inputs (LiDAR, BAER files, cached soils) are already on disk.

### 3.7 Batches: many fires at once

`pipeline/batch.py`. A batch starts from a list (one Unique Fire ID, or `name,year[,state]`,
per line), typed into a file or built in the app (3.8).

1. **resolve** (serial): each line becomes its `fires/<key>.json` through `new_fire.py`. Done
   one at a time so parallel runs never write configs (two same-named fires could otherwise
   race for the same file). Lines matching no fire or several, malformed lines and duplicates
   are left out with the reason. Writes `data/batches/<batch>/fires.tsv`.
2. **run**: each fire is `run_fire.py <config>`, the same end-to-end run as the app's, with its
   own log and status file; a failure does not stop the others; fires with a finished project
   are skipped unless `--force`.
   - `--runner local --workers N`: N fires at once on this computer, one process each. Each
     run is mostly single-threaded, so 3-4 suit a 6-core, 32 GB computer.
   - `--runner slurm` (staged, because Lemhi's compute nodes have no internet; 3.6):
     1. **inputs** on the login node: `run_fire.py --phase inputs` for every fire,
        `--fetch-workers` (2) at a time, at low priority (`nice`) with at most 2 threads each.
        Mostly downloads, plus ~1 min of raster work per fire. Whether this is acceptable on
        Lemhi's login node (or there is a data-transfer node for it) is a question for the
        cluster staff.
     2. **wildcat** on compute nodes: `slurm/batch_fires.slurm` as one job array over the fires
        whose inputs succeeded, `--phase wildcat` (no network), one fire per task (4 cores,
        16 GB; a 10 m fire peaks at ~2.4 GB), at most `--max-running` at once, each task with
        a private numba cache. Not a node per fire: a 10 m fire would leave most of a 40-core,
        192 GB node idle, and Slurm packs several tasks per node.
     3. **finish** on the login node once the array is done (`batch.py finish <batch>`, or
        `--wait` to poll the queue and do it automatically): `--phase usgs` for every fire
        whose wildcat phase finished, then the summary.

     `--compute-internet` instead runs each whole fire inside the array, with a summary job
     after it, for clusters whose compute nodes can download. 1 m fires still go through the
     split (3.6).
3. **summary**: `summary.csv`, one row per fire: status and error, stage times (all phases
   combined), assess peak memory, where each phase ran, burn inputs used, segments,
   fire-wide threshold, and the USGS comparison.

Each fire's status file records every phase (status, seconds, host, Slurm job, error), so a
batch can be resumed: `run` skips fires already assessed, `finish` only checks fires whose
model run finished.

Tested locally (2026-10-07), 3 at a time from an empty numba cache: five fires covering both
input paths all done in 262 s: South Rim, Big Bear and Rush 2025 on USGS-published BAER
inputs, Woodhead 2020 and Boise 2024 on Sentinel-2 with estimated severity (the first fire
alone, 84 s, building the cache; Woodhead the longest, 177 s). Results identical to their
single runs (e.g. Woodhead 7,781 segments, 31.6 mm/h; Boise 37.0 mm/h). An ambiguous name
("Rush, 2025": 4 fires), a malformed line and a duplicate were left out with the reason.

Staged flow tested locally (2026-10-08) with a stand-in for `sbatch` that runs each array task
here with all network access blocked (every request sent to a dead proxy; Python and GDAL
downloads both confirmed to fail): South Rim, Big Bear and Rush 2025, inputs 8-10 s each (cached
downloads), wildcat 61-69 s each with no network (including ~40 s of compiling into a private
numba cache), USGS comparison ~1 s each; results identical to their single runs.

**On Lemhi (2026-10-08, batch `lemhi1`, array job 1550543):** South Rim, Big Bear, Rush and
Palisades 2025 and Woodhead 2020, all done in ~7 minutes. Inputs on the login node 17-44 s per
fire, 2 at a time; the 5 array tasks ran at once on 4 compute nodes in 1:22-3:26 each (the model
phase 76-200 s, including ~40 s of compiling into a private numba cache and Lemhi's slower
cores), peak memory 0.57-2.1 GB (MaxRSS; Woodhead the largest); USGS comparison on the login
node 3-5 s per fire, Woodhead 56 s (a 114 MB ScienceBase archive). Results identical to the
local runs for all five (segments, fire-wide threshold, USGS ratio and pairs; e.g. Woodhead
7,781 segments, 31.58 mm/h, 0.847 on 7,850 pairs). 16 GB per task is generous:
~6 GB would let Slurm pack more fires per node in large batches.

### 3.8 Batches from the app, on this computer or an HPC cluster

The app runs on the user's computer. A batch is started there and runs either here or on a
cluster; finished fires from the cluster are copied back so they show on the map as if they
had run here.

- **Picking fires:** each search result has a checkbox; ticked fires stay selected across
  searches and show as chips above **Run N selected**. **Run a list of fires** takes pasted
  lines instead (the batch list format, at most 500). **Where** offers *On this computer*,
  plus the cluster once one is set up.
- **On this computer** (`app/batches.py`): `batch.py all <list> --runner local --workers 3`
  as a background process. One local batch at a time, and not while a single run is going.
- **On an HPC cluster** (`app/hpc.py`): over SSH (paramiko), the app uploads the list to
  `data/batches/<batch>.txt` in the cluster's copy of the repository and starts
  `batch.py all ... --runner slurm --wait` there, detached (`nohup`), so it carries on if
  this computer sleeps or the app closes. The batch's exit code lands in `app.exit`. Every
  `poll_s` seconds (default 30, minimum 10) the app reads the batch's `fires.tsv`, status
  files and log tail over SFTP, and copies back each fire that is done or skipped: its
  config, `usgs_check.json`, `sources.json`, `configuration.py`, `preprocessed/dem.tif` and
  `severity.tif`, `exports/*` and `inputs/perimeter.*` (a few MB to tens of MB for a 10 m
  fire). Each file is copied under a `.part` name and renamed. Staged (3.7) by default;
  *Compute nodes have internet* switches to `--compute-internet`.
- **Progress:** **Batches** lists the last 8 batches with per-fire status, phases, errors,
  "copied here", and the log tail, refreshed every 5 s while one is active. The map's fire
  list refreshes as fires finish. Fires a batch adds arrive **not kept** (3.5); fires that
  were already here keep their state.
- **Restarts:** each batch's record is `data/batches/<batch>/app.json`. On start the app
  resumes polling HPC batches and marks local batches whose process is gone as *stopped*
  (finished fires stay). A lost connection shows *disconnected* with **Reconnect**.
- **Any cluster, each user their own:** nothing is Lemhi-specific. **Settings: HPC
  cluster** holds one profile: login host and port, user, optional jump host, key or
  password, the repository folder on the cluster, the environment setup command (default
  `source .venv/bin/activate`), the Slurm partition, max running tasks and staged or not.
  Saved in `data/hpc_settings.json` (gitignored, no secrets). Each user needs their own
  account on a cluster with Slurm, and must copy the repository there and run `uv sync` once.
  **Test connection** checks, in order: login (with the host-key fingerprints), the
  repository folder, the Python environment (imports wildcat and pfdf), the batch scripts,
  and Slurm (`sbatch`, `sinfo`).
- **Credentials:** each login (the jump host, then the cluster) tries SSH keys and the agent
  first, then its saved password, so a gateway that only takes passwords in front of a
  cluster that takes a key works (Lemhi: `ssh -J <user>@bsu-hpclogin.c3plus3.org
  <user>.bsu@lemhi.c3plus3.org`, two accounts). A jump host with no password of its own is
  tried with the cluster's. A password, if one is needed, goes to the system password store through `keyring` (Windows Credential Manager, macOS Keychain,
  or the Secret Service keyring on a Linux desktop; a Linux machine without one refuses to save
  passwords and SSH keys still work) (service
  `debris-flow-hunters`, entry `hpc:<user>@<host>`, plus `hpc-jump:...` for a jump host). It
  is never written to a file or returned to the browser: the page only learns *saved* or
  *not saved*. It can be set or cleared in the app, or by hand with
  `keyring set debris-flow-hunters hpc:<user>@<host>`. Not a `.env` file, so the password is
  not left in plaintext next to the code. Clusters that require a one-time code (Duo/MFA)
  at every login are not supported; use a key there. Host keys are trusted on first use and
  saved to `data/hpc_known_hosts`. A changed key afterwards refuses the connection.
- **Input checks:** the host and user must be plain names, and the repository folder may
  not contain shell metacharacters. The partition is shell-quoted, and batch names are
  generated (`app_YYYYMMDD-HHMMSS`), so nothing typed in the page reaches the remote shell
  unquoted. The server listens on 127.0.0.1 only.

Tested 2026-10-08 (staging app):
- **Local batch:** Border 6 and Upper Smith 2026 were ticked in search and run here.
  - Upper Smith was done in 74 s, appeared on the map as not kept, and its row and log
    showed in Batches.
  - Border 6 failed, correctly, at `recover`: the WFIGS polygon is 10.6 km², but its
    `poly_GISAcres` (1,618 ac = 6.5 km²) is stale. The polygon matches the incident size,
    2,525 ac (open item).
- **Settings:**
  - bad host and folder values are refused;
  - the password went to the password store, was absent from the settings file and from
    every response, and was cleared.
- **Unreachable host:** Test connection reports `login: could not connect to <host>`. A
  batch to it fails with "no password saved..." or the connection error.
- **HPC loop against a fake cluster** (a local folder served through a stand-in
  `Connection`):
  - submit, then rows while running (phases shown, nothing copied);
  - the finished fire copied back (16 export files, DEM, no `.part` left), the left-out
    line shown with its reason;
  - *done* on `app.exit`, and the fire marked not kept.

**On Lemhi from the app (2026-10-08).** Login: password through the BSU jump host
(`<user>@bsu-hpclogin.c3plus3.org`) to `<user>.bsu@lemhi.c3plus3.org`. Test connection:
all five checks pass (wildcat 1.1.1, pfdf 3.0.2).
- **Batch 1, Rush and South Rim 2025:** both had already been assessed on Lemhi, so both
  were skipped in seconds and copied back.
- **Batch 2, `app_20261008-114917`, array job 1562961:** Bush, Big Springs and Archie
  Creek 2020, all on Sentinel-2 with estimated severity.
  - The first launch hung: `cd ... && nohup ... &` backgrounds the whole list as a subshell
    that holds the SSH channel open until the batch ends. The app timed out after 120 s
    and marked the batch failed while it kept running on Lemhi.
  - Fixed: only `nohup setsid ...` goes to the background, and Reconnect is offered on
    failed HPC batches. Reconnect then picked the batch up and copied all three back, not
    kept.

| Fire | Size | Inputs (login node) | Model (array task) | Task time, peak memory | USGS check | Threshold vs USGS |
|---|---|---|---|---|---|---|
| Bush 2020 (AZ) | 193,604 ac | 55.8 s | 441.6 s | 7:27, 2.48 GB | 35.5 s | 0.978 on 18,293 pairs; fire-wide 37.4 vs USGS 37.3 mm/h |
| Archie Creek 2020 (OR) | 131,596 ac | 55.2 s | 262.4 s | 4:28, 1.77 GB | 35.1 s | 0.940 on 19,499 pairs; fire-wide 17.2 vs USGS 19.1 mm/h |
| Big Springs 2020 (UT) | 2,918 ac | 54.6 s | 77.0 s | 1:22, 0.60 GB | 0.9 s | no USGS assessment |

Bush (~780 km²) is the largest fire run so far. It took 7.5 minutes and 2.5 GB on one
4-core task, well inside the 16 GB request.

### 3.9 Publishing to ArcGIS Online (plan, not built)

Goal after the showcase: a scheduled pipeline publishes assessments of current fires as
public hosted layers behind an ArcGIS Dashboard. Publishing is controlled by an authorized
account, not open to anyone running the app.

**Achievable:** yes, with parts that already exist.
- The pipeline already writes a file geodatabase (3.5).
- The ArcGIS API for Python (`arcgis` package) does the rest:
  - signs in to an org (`GIS(url, ...)`, OAuth or a named user);
  - uploads the zipped geodatabase as an item and publishes it as a hosted feature layer
    (`item.publish()`);
  - replaces a layer's data in place on later runs, keeping its URL, so the dashboard
    never needs editing (`FeatureLayerCollection.fromitem(...).manager.overwrite(...)`);
  - shares items with *Everyone* or a group.
- The dashboard is built once in ArcGIS Dashboards on a web map of those layers. It updates
  when the layers do.

What it needs that we do not have:
- An ArcGIS Online organization whose admin allows public sharing. Ask whether the
  university has one.
- One account with a **Creator** user type and the **Publisher** role, used only by the
  pipeline: a service account, not a person's.
- Credits for hosted feature storage, which is small for vector layers.

Proposed flow:
1. **Watch** (every few hours, by Task Scheduler, cron, or `scrontab` on a cluster): query
   WFIGS for fires that have a final or contained perimeter, are above a size threshold,
   lie in the chosen region, and are not yet published or have a newer perimeter. Write
   them as a batch list.
2. **Assess:** `batch.py` as in 3.7, locally or on the cluster.
3. **Stage privately:** publish each finished fire as a private item in a review group.
   Layers: segments, basins, outlets, perimeter. Fields: the design-storm P/V/H and
   threshold, the burn-severity source, the USGS comparison, and a run date.
4. **Approve:** a named reviewer with the authority to publish checks each staged fire and
   shares it to the public group. This step is the only one that makes anything public.
5. **Update:** later runs overwrite the same items. A fire is retired from the public
   dashboard after a chosen period.

Rules:
- Every public layer carries a disclaimer and its sources. USGS publishes official
  assessments for some fires; ours must be labeled as research output, not a USGS product,
  and must link to USGS's assessment where one exists (`usgs_check.py` already finds it).
- The publisher credentials live in the same credential store as the HPC password (3.8),
  on the machine that runs the schedule only.
- The local app does not get a Publish button. Publishing stays in the scheduled pipeline
  behind the review step.
- The browser's storm slider has no Dashboards equivalent. The public layers carry
  precomputed values at fixed design storms instead.

---

## 4. Inputs, sources, fallbacks

| wildcat input | Primary | Fallbacks, in order | Last resort |
|---|---|---|---|
| perimeter | WFIGS final perimeter | accepted on the incident size when WFIGS's GIS acres are stale; clipped to US elevation coverage when it crosses a border | – |
| dem | USGS 3DEP 1/3 arc-second (10 m), TNM S3 tiles | the same tiles from USGS's 3DEP image service, when S3 is unreachable (current version only) | run stops (optional: RECOVER 1 m LiDAR, `--lidar-res`) |
| dnbr | USGS-published inputs (BAER team or EROS BARC) | Sentinel-2 single dates → Sentinel-2 composite → Landsat 8/9 single dates → Landsat 8/9 composite | run stops |
| severity | USGS-published soil burn severity | Forest Service BAER soil burn severity map (image service) | estimated by wildcat from dNBR (125/250/500) |
| kf | STATSGO KFFACT (pfdf) | SSURGO (Soil Data Access), rescaled to STATSGO | the fire's median KF |
| evt (water/developed mask) | RECOVER bundle (LANDFIRE) | LANDFIRE ImageServer (LF2016–2025) | none (nothing masked) |
| validation | USGS assessment service (by location) | ScienceBase archive (by name) | "no assessment" |

Every source is free and needs no account, key or email (4.4). The app shows which one each
fire used, with a link, under **Logs > Input data for this fire** (`build_map.py
input_services()`).

### 4.1 Why each source

**Perimeter**
- **Primary, WFIGS final perimeter**
  - **Why:** the official interagency record, kept up to date through the fire.
  - It carries the Unique Fire ID the whole pipeline keys on, so finding the fire and its
    perimeter is one lookup.
  - It covers every fire the app has run, 2020 to 2026.
  - `recover.py` assembles the rings itself from Esri JSON, because WFIGS's GeoJSON output
    returns invalid perimeters (section 9). It checks the area against WFIGS's own acreage
    (within 1%).
- **Fallback, the incident size** (`recover.py`).
  - **Why:** WFIGS's `poly_GISAcres` is sometimes out of date with its own polygon. Border 6
    2026 records 1,618 ac for a 2,614 ac polygon, and was refused.
  - The polygon is accepted when it matches the incident size within 10% and its rings
    cannot be assembled two ways (no ring inside another, so XOR and union agree). The
    acreage check exists to guard against exactly that ambiguity.
  - Border 6 now passes (+3.6% against its 2,525 ac). Rush, whose acreage is current, is
    unchanged.
- **Clipped to US elevation coverage** (`prepare_fire.py`), when the perimeter crosses into
  Mexico or Canada.
  - **Why:** there is no US elevation (or US soil) on the far side, so nothing can be
    modeled there. USGS's own assessments have the same limit.
  - The perimeter wildcat and the map use is clipped to where the DEM has data, and
    `sources.json` says how much was removed. The run stops only when no part of the
    perimeter has elevation.
  - **Border 6 2026:** 3.7 of 10.6 km2 removed (south of the border); 6.87 km2 modeled.
    131 segments, all with a threshold, and none of their 2,393 vertices on missing
    elevation. The map shows the outline following the border.
  - **Only fires that used to stop are affected.** Re-run with the rule in place, Ranch 2025
    (Mexican border), Eagle Bluff 2023 (Canadian border) and Wood 2024 (inland) give
    identical segments, basins and outlets (every attribute) and byte-identical perimeter
    files.

**Elevation (DEM)**
- **Primary, USGS 3DEP 1/3 arc-second (10 m) from TNM's S3 tiles**
  - **Why:** the DEM USGS's own assessments use. wildcat's resolution check (6.5–11 m) is
    built around it.
  - It covers the lower 48 seamlessly.
  - Reading the tiles directly allows pinning an older version to reproduce an assessment
    (section 9).
- **Option, RECOVER 1 m LiDAR** (`--lidar-res`), where a fire's RECOVER bundle has it.
  - **Why it is not the default:** it does not change the answer. Thresholds and observed
    flows caught are the same at 10, 2 and 1 m (8.2), at up to ~100× the cost.
- **Fallback, the same tiles from USGS's 3DEP image service** (`prepare_fire.dem_from_service`).
  - **Why:** only for an outage of TNM's S3. Coverage and accuracy need no other DEM in the
    lower 48; missing tiles at coasts and borders are handled in 4.3.
  - **Same product, not a different DEM:** the service mosaics every 3DEP resolution
    (1 m LiDAR included), so the export is locked to its 1/3 arc-second catalog items
    (cell ~10.3 m), which carry TNM's tile names.
  - **Version recorded:** each item's `pubdate` is the tile version (Wood n34w112 20260601).
    It goes into the cache's `.source.json` and the fire's `sources.json`, so a run says
    what it used.
  - **Current versions only:** a pinned version (`--dem-version`) exists only on S3, so an
    outage then stops the run rather than silently using another version.
  - **Close, not identical:** the service reprojects to our UTM grid itself, so pixels
    differ from GDAL's bilinear warp of the same tiles.
    - At Wood: median 0.20 m; more than 1 m on 1.9% of pixels.
    - At Wapiti: median 0.25 m; more than 1 m on 2.3% (11.4 M pixels).
    - Both are far smaller than a real version change (Wapiti 2024 vs 2026 tiles: more than
      1 m on 2–45%).
    - Exact pixels could not be pulled from the service: requests on the tiles' own degree
      grid come back mostly empty.
  - **Effect on results (Wood 2024, end to end, 2026-10-08):**
    - segments: 630 on the TNM DEM vs 624 on the service DEM;
    - fire-wide median threshold 73.09 vs 73.15 mm/h (+0.08%);
    - the 310 segments with the same start and end points in both: median ratio 1.0000,
      90% within 0.997–1.006, all within 5%.
  - **Tested failures:**
    - S3 unreachable (unresolvable host): falls back and records it.
    - A pinned version during the outage: stops with "version 20240701 is only there".
    - `DFH_DEM_SOURCE=service` forces the fallback for testing.

**Burn imagery (dNBR)**
- **Primary, the dNBR USGS published with its own assessment** (BAER team or EROS BARC
  products, from `inputs.zip` on ScienceBase; `usgs_inputs.py`).
  - **Why:** it is exactly what USGS ran, scenes chosen by the BAER team.
  - Some of these products appear in no other public service (Big Bear).
  - With them the pipeline reproduces USGS: Big Bear threshold ratio 0.96.
  - Used only when USGS's perimeter covers at least half of ours.
- **Fallback 1, Sentinel-2, one clear pre and one clear post date by rule**
  (`sentinel2_dnbr.py`).
  - **Why:** the only free, no-login imagery that covers any fire since 2016 within days of
    containment.
  - It uses the same 20 m bands (B8A, B12) and reflectance offset as BAER. On the same
    scenes ours is 1.04–1.09× BAER's (8.4).
  - Scenes are chosen by rule, so expect ±20% on F against USGS.
  - A date must be 98% clear over the perimeter.
- **Fallback 2, a Sentinel-2 composite.**
  - **Why:** for cloudy or smoky fires where no single date is 98% clear. Each pixel takes
    the date nearest the target on which that pixel is clear:
    - **post:** nearest containment, from ignition + 14 d to containment + 30 d;
    - **pre:** the same pre-ignition or anniversary window as the single-date rule.
  - Each date used must be at least 20% clear, and at most 6 are used.
  - Grizzly 2024 (Alaska) had no single clear pre-fire date, so the run stopped. It now gets
    a composite of 1 pre and 6 post dates covering 97.8% of the perimeter.
  - Mixing dates adds a little noise where neighbouring pixels come from different days, so
    this is used only when single dates fail.
- **Fallback 3 and 4, Landsat 8/9, single dates then composite** (Collection 2 Level-2,
  from the Microsoft Planetary Computer).
  - **Why:** a second satellite gives more chances of a clear date, and Landsat reaches back
    before 2016.
  - It uses the same NBR bands (OLI B5 and B7) with Landsat's own scale and offset
    (0.0000275, −0.2) and its QA_PIXEL cloud, shadow and snow bits.
  - Checked at Wood 2024: on the same day (2024-09-05) Landsat's NBR correlates with
    Sentinel-2's at r = 0.84, reading +0.04 higher. dNBR cancels a constant offset, and
    where the fire burned between two near-matching date pairs Landsat's dNBR is 0.99×
    Sentinel-2's.
  - Left to its own dates, Landsat picked different scenes at Wood (it passes every 8 days
    at best) and read 0.59× Sentinel-2. Scene choice dominates, as in 8.4. That is why
    Landsat comes after Sentinel-2.
- **Last resort: none.** When nothing reaches 98% of the perimeter, the run stops and lists
  each fallback tried.

**Burn severity**
- **Primary, the soil burn severity USGS published with its assessment.**
  - **Why:** the severity map is the input that moves results most (8.3). BAER's version is
    field-checked.
- **Fallback, the Forest Service BAER team's soil burn severity map** (`baer_sbs.py`), from
  the Forest Service's public image service.
  - **Why:** BAER maps many fires that USGS never assesses, or assessed before it published
    inputs. Those fires used to get estimated severity.
  - It covers Forest Service BAER incidents from 2017 to the last completed fire year.
  - Matching is by location and year: the map must cover half our perimeter.
  - Checked at Wapiti 2024: the service's map is identical, pixel for pixel, to the SBS in
    BAER's own package (1,272,676 classified pixels).
  - Run end to end, 2026-10-08 (threshold ratio against USGS, before → after):
    - **Boise 2024:** 0.856 → **0.932**. The share burned and steep (T) went from 2.00× USGS
      to 1.25×; fire-wide 37.0 → 44.0 mm/h against USGS's 47.3.
    - **Wapiti 2024:** 0.883 → **0.952**. T 2.91× → 1.51×; fire-wide 30.9 → 34.5 mm/h against
      USGS's 34.8.
    - **Woodhead 2020:** 0.847 → 0.845, unchanged. Its gap is the dNBR (F 1.40×, scene
      choice), not the severity map.
  - Not every fire has one. Eagle Bluff 2023 is not a Forest Service incident.
- **Last resort, estimated by wildcat from dNBR** (breaks 125 / 250 / 500).
  - **Why:** wildcat's built-in method, with USGS's standard breaks. It needs nothing beyond
    the dNBR.
  - **Cost:** it over-classifies moderate and high burn (T 1.2–2.0× USGS), which lowers
    thresholds. Fires on this path show as "Estimated" in the app.

**Soil erodibility (KF)**
- **Primary, STATSGO KFFACT** (via `pfdf.data.usgs.statsgo`).
  - **Why:** the soil dataset the USGS M1 model was fit with, and the one USGS's own
    assessments use (their `metadata.json`: "STATSGO KFFACT"). National, no login.
  - Its cloud-optimized files store a missing map unit as 0 (sometimes −0.1). wildcat drops
    those, and a stream draining such ground gets no threshold.
- **Fallback 1, SSURGO from USDA's Soil Data Access** (`soils.py`, `prepare_fire.fill_kf`).
  - **Why:** the detailed county survey often has a value where STATSGO does not.
  - Each map unit gets the component-percent weighted `kffact` of the components' surface
    horizons.
  - **Rescaled:** where both surveys have a value, SSURGO reads differently. At Wapiti it is
    0.74× STATSGO over 8.8 M pixels; at Woodhead, 1.11×. Raw SSURGO would shift S in the
    filled areas, so each fire's SSURGO is multiplied by its own median STATSGO/SSURGO ratio.
    This keeps SSURGO's detail on the scale M1 was fit with.
- **Fallback 2, the fire's median KF.**
  - **Why:** some ground has no KF in either survey (rock outcrop, water). The median keeps
    those streams on the map. wildcat offers the same fill (`kf_fill`), which we do
    ourselves so it can be recorded.
- **Recorded:** `sources.json` says what was filled and how much, for example "STATSGO soils;
  gaps filled: SSURGO x1.35 13.6%, fire median 0.21 4.1% of the area used". The app lists
  SSURGO under Input data.
- **USGS does not fill KF:** its published Big Bear 2025 has 16 segments without a threshold,
  Wapiti 2024 has 517. `--kf-fill none` keeps STATSGO as is for runs meant to match USGS
  exactly; `--kf-fill median` skips SSURGO.
- **Wapiti 2024, run end to end:**
  - STATSGO had no KF for 17.7% of the buffered perimeter. SSURGO filled 13.6% (×1.35) and
    the median 4.1%.
  - Segments without a threshold went from 526 to **0**.
  - The comparison with USGS is unchanged (0.883 on the same 7,643 pairs), because matched
    pairs already had KF. Fire-wide 30.48 → 30.86 mm/h.

**Vegetation type (EVT), used only to mask water and developed land**
- **Primary, LANDFIRE EVT from the fire's RECOVER bundle.**
  - **Why:** already clipped to the fire, with the LANDFIRE vintage RECOVER chose.
- **Fallback, LANDFIRE's public EVT image service** (the newest vintage not after the fire
  year: LF2016, 2022–2025).
  - **Why:** the same data, for fires with no bundle or a bundle missing EVT (Eagle Bluff
    2023).
  - Read from the public ImageServer, because LANDFIRE's job API requires an email address.
- **Last resort, no EVT.**
  - **Why acceptable:** wildcat then masks nothing, so streams through reservoirs and towns
    stay in. A handful of segments, not thresholds elsewhere.

**Validation (the USGS comparison)**
- **Primary, USGS's assessment service, matched by location.**
  - **Why:** it lists every assessment (748, including the current year). Location avoids
    USGS's renames, merges and same-named fires.
- **Fallback, the ScienceBase archive, by name and date** (2013–2024).
  - **Why:** it still works when the service is down.
- **Last resort, "no assessment".**
  - **Why:** many fires are never assessed (Big Springs 2020). Never fails a run.

### 4.2 Fallbacks not built, and why

- **MTBS dNBR and severity.**
  - **Why not:** published one to two years after a fire, too late for emergency use. At
    Grizzly it caught fewer observed flows (12/22) than BAER (16) or estimated (18)
    severity (8.3).
  - Useful only for hindcasts of older fires, and Landsat (above) now covers those.
- **Alaska, Hawaii and Puerto Rico** (LANDFIRE's separate EVT services, Alaska's coarser or
  IfSAR elevation).
  - **Why not:** the USGS M1 model was calibrated on fires in the western lower 48, so a
    result there would not mean much yet. Grizzly 2024 now gets past the dNBR step, but
    stops at EVT.
- **Older perimeters** (NIFC's historical perimeter layer, MTBS boundaries).
  - **Why not:** WFIGS has covered every fire run so far.
- **User-supplied inputs** (own perimeter, BAER files, DEM).
  - **Why not:** deferred until after the showcase. The pipeline already accepts BAER files
    and a LiDAR DEM in a fire's config; the app does not expose them.

### 4.3 DEM tiles at coasts and borders

In `prepare_fire.py`, the 1-degree tiles covering the
padded fire grid are read from TNM's S3 (current or a pinned version, or local copies).
A tile that does not exist (open ocean, outside the US) is skipped with a note. Pixels
with no elevation are left as nodata with a note; where they fall inside the fire perimeter
(a perimeter crossing the border), the perimeter is clipped to the DEM's coverage (4.1), and
the run stops only if no part of the perimeter has elevation. Our tile list is cross-checked against pfdf's TNM
search and any difference printed (advisory). Tested 2026-10-08:
- Palisades 2025 (Pacific coast): 31,759 ocean pixels left as nodata; USGS's WERT inputs;
  threshold ratio 1.00, fire-wide 20.9 vs USGS 20.4 mm/h. (The previous code stopped on
  any empty pixel.)
- Ranch 2025 (Mexican border, AZ): 349,984 pixels south of the border left as nodata.
- Eagle Bluff 2023 (crossed into British Columbia): 1.1 M pixels north of the border left
  as nodata; WFIGS's perimeter covers the US part only. Its 2023 RECOVER bundle has no DEM
  or EVT: the missing members are skipped (they used to fail the run) and EVT comes from
  LANDFIRE LF2023.
- Wapiti, pinned 2024-07-01 version: old code from S3, new code from S3 and new code from
  hand-downloaded tiles are byte-identical to the cached DEM; the current version likewise.
- Not yet exercised on a real fire: a tile missing entirely (the coast and border tiles
  above exist, with nodata over ocean or outside the US); checked only on an ocean tile.

### 4.4 What it costs: nothing

Every source is public, free and needs no account, API key or email. Checked 2026-10-08.

| Source | Used for | API (what the code calls) | Access |
|---|---|---|---|
| WFIGS Interagency Perimeters (NIFC, ArcGIS Online) | perimeter, fire lookup | `https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters/FeatureServer/0/query` (ArcGIS REST query, Esri JSON) | public feature service |
| The National Map S3 | 10 m DEM | `https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{tile}/USGS_13_{tile}.tif`, versions under `historical/` (HTTPS range reads via GDAL `/vsicurl/`) | public bucket |
| USGS 3DEP elevation image service | 10 m DEM when S3 is down | `https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer` `/query`, `/exportImage` | public image service |
| STATSGO KFFACT (ScienceBase COG) | KF | `pfdf.data.usgs.statsgo.read("KFFACT", ...)`, COG collection `https://www.sciencebase.gov/catalog/item/675083c6d34ea60e894354ad` | public |
| USDA Soil Data Access | SSURGO KF | `https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest` (POST, T-SQL, JSON) | public REST |
| RECOVER archive (Idaho State University) | EVT, optional LiDAR | `https://giscenter-sl.isu.edu/aoc/aoc_research/recover2/archive/{year}/Fire_{year}_{unit}_{localid}.zip` (HTTP range reads of zip members) | public HTTPS |
| LANDFIRE EVT image service | EVT | `https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF{year}/LF{year}_EVT_CONUS/ImageServer/exportImage` | public image service |
| USGS post-fire assessment service | published inputs, validation | `https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/USGS_Post_Fire_Hazard_Assessment_Data/FeatureServer` (layers 0 points, 9 perimeters) | public feature service |
| USGS ScienceBase | published inputs, assessment archive | `https://www.sciencebase.gov/catalog/item/{id}?format=json`, `/catalog/file/get/{id}?f=...`, `/catalog/items` search | public |
| Forest Service BAER SBS image service | burn severity | `https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/USFS_EDW_BAER_SoilBurnSeverityClassification/ImageServer` `/query`, `/exportImage` | public image service |
| Earth Search (Element 84) | Sentinel-2 dNBR | STAC search `https://earth-search.aws.element84.com/v1/search` (collection `sentinel-2-c1-l2a`); COGs on AWS read via GDAL | public, no login |
| Microsoft Planetary Computer | Landsat 8/9 dNBR fallback | STAC search `https://planetarycomputer.microsoft.com/api/stac/v1/search` (collection `landsat-c2-l2`); read token `https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2` | public; free anonymous token |

**Avoided because it costs money:** Landsat in Earth Search points at USGS's `usgs-landsat`
bucket, which is requester-pays (the downloader is billed by AWS). The Planetary Computer's
copy of the same data is free.

**Not used because it needs an account or email:** LANDFIRE's job API (LFPS, email
required; the public ImageServer is used instead), USGS EarthExplorer, and NASA Earthdata.

### 4.5 Every path, end to end (2026-10-08)

Each row is a full `run_fire.py` run on the laptop, read back from disk. Forced paths use
the test switches `DFH_DNBR_SENSOR`, `DFH_DNBR_METHOD` and `DFH_DEM_SOURCE`.

| Path | Fire | Result |
|---|---|---|
| USGS-published dNBR and severity | Rush 2025 | ok, 45 s; 659 segments; 0.945 vs USGS on 343 pairs (as in 8.1) |
| Sentinel-2 + Forest Service BAER map | Wood 2024 | ok, 35 s; 630 segments, none without a threshold |
| Sentinel-2 + estimated severity, LANDFIRE EVT (no RECOVER EVT), KF filled | Eagle Bluff 2023 | ok, 106 s; 1,508 segments; KF: SSURGO 1.7%, median 10.6% |
| Perimeter accepted on the incident size, then clipped at the border | Border 6 2026 | first run stopped at the DEM (3.7 of 10.6 km2 in Mexico); with clipping (4.1): ok, 6.87 km2 modeled, 131 segments |
| Sentinel-2 composite (forced) | Wood 2024 | ok; method recorded "composite of 1 pre and 1 post dates" (dates nearest containment were clear) |
| Landsat 8/9, single dates (forced) | Wood 2024 | ok; pre 2023-10-05, post 2024-10-07 |
| Landsat 8/9 composite (forced) | Wood 2024 | ok |
| KF `--kf-fill none` / `median` / default | Woodhead 2020 | all ok; sources.json records each; 7,873 segments, 0.845 vs USGS in all three |
| DEM from the 3DEP image service (forced, and S3 unresolvable) | Wood 2024 | ok; fire-wide +0.08% (4.1) |
| DEM fallback with a pinned version | Wood 2024 | stops: "version 20240701 is only there" |
| USGS comparison with the service down | Woodhead 2020 | found `wdh2020` by name in the ScienceBase archive; same 0.845 on 7,900 pairs |
| Dates refreshed from WFIGS | Wood 2024 (stale copy) | containment 2024-09-15 corrected to 2024-10-16; `dates_locked` left it alone |

The app, same day, against the staging server:
- **Fires and map:** all 11 fires load; tiles, the per-fire threshold legend and the
  input-data list are right for each path (Rush published, Wood BAER map, Eagle Bluff
  estimated with LANDFIRE and SSURGO rows).
- **Exports:** all four formats for Wood are consistent: GeoJSON 630 features, CSV 630
  rows, shapefiles and file geodatabase with 630 segments, 69 basins, 69 outlets and the
  perimeter.
- **Add a fire:** Upper Smith 2026, by search, done in 60 s through every stage label,
  arrived not kept.
- **Keep, delete, restore, delete permanently:** each works, with two-click confirmations.
- **Run a list:** one assessed fire skipped, one bad line left out with its reason, shown
  in the Batches dropdown.
- **HPC:** Test connection to Lemhi passes all five checks.
- **Console:** no errors.

Found and fixed while testing:
- the batch summary lacked a `baer_sbs_s` column and failed to write;
- KF fill counted pixels with no DEM (Eagle Bluff's Canadian buffer) in its shares;
- the Wood DEM comparison had first been counted by segment end point, which merged
  segments that share a confluence (406 of 630); corrected in 4.1 and 8.10.

---

## 5. The models

wildcat runs three pfdf models on every stream segment (catchments ≤ 8 km²):

- **Likelihood, USGS M1** (Staley et al. 2017, 15-minute):
  `logit P = -3.63 + (0.41·T + 0.67·F + 0.70·S) · R15`, with `R15 = I15 · 0.25` (mm in 15
  min). T = share of the catchment burned at moderate/high severity *and* steeper than 23°;
  F = mean dNBR / 1000; S = mean soil KF-factor. The **rainfall threshold** is the I15 at
  which P = 50%: `I15_50 = 3.63 / (0.25 · (0.41T + 0.67F + 0.70S))` mm/h.
- **Volume, Gartner et al. 2014 (emergency):**
  `ln V = 4.22 + 0.39·√I15 + 0.36·ln(Bmh) + 0.13·√R`, V in m³, Bmh = moderate/high burned
  area (km²), R = relief (m). 95% range: V·e^(±1.96·1.04).
- **Combined hazard, Cannon et al. 2010:** likelihood class (edges 0.2/0.4/0.6/0.8) plus
  volume class (edges 10³/10⁴/10⁵ m³), summed and binned into hazard 1–3 (edges 3, 6).

Rainfall is not an input: the model solves for it. USGS reports design storms of 16, 20, 24
and 40 mm/h; the map lets the user pick any storm.

---

## 6. Code map

| Path | Role |
|---|---|
| `app/server.py` | Local web app: lookup, runs (local or Slurm), batches, HPC settings, map data, exports, keep/delete/trash |
| `app/batches.py` | Batches started from the app: run here, or on a cluster and copy each finished fire back (3.8) |
| `app/hpc.py` | HPC profile, password in the system password store, SSH/SFTP, connection test, remote batch start and status (3.8) |
| `pipeline/batch.py` | Many fires: resolve, run (local or staged Slurm), finish, summary (3.7) |
| `map/template.html` | The whole front end (Leaflet): map, storm slider, popups, legend, app panels |
| `map/build_map.py` | Map layers from a finished project; `main()` writes the offline map `map/dist/debris_flow_map.html` |
| `pipeline/run_fire.py` | End to end for one fire (the stages in 3.3) |
| `pipeline/fires.py` | WFIGS lookups, Unique Fire ID, `fire_key` (folder naming) |
| `pipeline/new_fire.py` | Writes `fires/<name>_<year>.json` from WFIGS |
| `pipeline/recover.py` | WFIGS perimeter; RECOVER bundle members over HTTP range requests |
| `pipeline/usgs_inputs.py` | USGS-published dNBR/severity for a fire, if any |
| `pipeline/sentinel2_dnbr.py` | Sentinel-2 dNBR with rule-based scene dates |
| `pipeline/prepare_fire.py` | Builds the wildcat project (DEM, KF, EVT, dNBR, severity, config); `--lidar-res` for LiDAR |
| `pipeline/wildcat_config_template.py` | wildcat's default `configuration.py`, copied into each project |
| `pipeline/measure.py` | Runs one wildcat stage in a fresh process: seconds and peak MB |
| `pipeline/run_wildcat.py` | Run wildcat on a project by hand (the CLI's `assess` is broken; section 9) |
| `pipeline/soils.py` | SSURGO KF from USDA Soil Data Access, to fill STATSGO gaps (4.1) |
| `pipeline/baer_sbs.py` | Forest Service BAER soil burn severity map for a fire, from its public image service (4.1) |
| `pipeline/split_fire.py` | Watershed split: preprocess, plan, build, run, merge, compare (section 7) |
| `pipeline/usgs_check.py`, `compare_usgs.py` | Find, fetch and compare USGS's own assessment |
| `slurm/*.slurm` | `run_fire` (one fire), `split_prepare` / `split_pieces` / `split_merge` (1 m array) |
| `fires/*.json` | One settings file per fire (written automatically) |
| `data/` | Everything downloaded or computed (gitignored) |

The research scripts behind section 8 (resolution sweep, scoring against observed flows,
Graber comparison, Trail Creek case study) are not on this branch; they live on
`wapiti-e2e` (uncommitted as of 2026-09-28).

---

## 7. Watershed split

- **Why:** a 1 m run of a large fire does not fit a regular node (Wapiti at 1 m: ~2 billion
  cells; the whole-fire run died at 168 GB on a 192 GB node), and more cores do not speed a
  single run up (section 8.5). Independent pieces fit regular nodes and run at once.
- **Why it is exact:** water does not cross drainage divides, and the model only rates
  catchments ≤ 8 km². Pieces are cut along divides and only across channels draining
  ≥ 20 km², so every segment the model can keep lies wholly inside one piece.
- **How** (`pipeline/split_fire.py`):
  1. **preprocess** the whole fire once (wildcat).
  2. **plan** on a 10 m version of the preprocessed DEM: D8 flow and drainage area (pfdf);
     label every cell by the first ≥ 20 km² channel cell it drains to; walk that channel
     network upstream to downstream, closing a piece at ~60 km² (`--target-km2`), at a
     river's exit, or where the channel ends; catchments draining off the grid go whole to
     the nearest piece; pieces under 15 km² merge into a neighbour. Writes `plan.tif`,
     `plan.json` (N pieces, bounds) and `inflow.tif` (rivers entering a piece from another).
     N is therefore set by the drainage network: Wapiti gives 27 pieces on the USGS 10 m DEM
     and 36 on the LiDAR grid.
  3. **build** each piece: every preprocessed raster clipped to the piece + 1 km margin,
     plus wildcat's `excluded` mask over rivers flowing in from another piece (a piece sees
     only part of their drainage). On Slurm each array task builds its own piece.
  4. **run** wildcat assess + export per piece (locally N at a time; on Slurm one per task).
  5. **merge**: a segment belongs to the piece containing its midpoint; basins follow their
     segment (`Segment_ID`); an outlet is kept when it lies exactly at the downstream end of a
     kept basin's segment; IDs renumbered into `<project>_split`. Outlets are matched by
     location because wildcat's outlets file is not in basin order: it saves the basins,
     removes nested drainages, then saves the outlets, so outlet *i* is not basin *i*.
  6. **compare** (optional) with a whole-fire run of the same project.

---

## 8. Results

Each result: **Source** (data, scripts, where it ran), **What** (the question), **Why**
(why it matters), **How** (method), **Result**, and **What it means** (the plain reading
and its limits). "Ratio" means ours ÷ USGS, as the median of per-segment ratios over
matched streams (within 20 m, similar drainage area), unless noted.

### Reading the numbers

**T, F, S: what the model knows about each stream.** For every stream segment the USGS M1
model summarises its catchment (the land draining into it) as three numbers; higher means a
debris flow is more likely, at less rain.

| Term | Plain meaning | Computed as | Wapiti, USGS median |
|---|---|---|---|
| T, terrain | How much of the catchment is both steep and badly burned | share of the catchment burned at moderate/high severity *and* steeper than 23° (~42% grade); 0–1 | 0.022 |
| F, fire | How badly the catchment burned on average | mean dNBR ÷ 1000 (dNBR: satellite burn damage, the pre- minus post-fire drop in the Normalized Burn Ratio × 1000; roughly < 100 unburned, > 660 high severity) | 0.333 |
| S, soil | How erodible the soil is | mean soil erodibility (KF-factor), STATSGO | 0.222 |

**The rainfall threshold** (`I15_50`, mm/h) is the 15-minute rainfall intensity at which a
stream's chance of a debris flow reaches 50%: `3.63 ÷ (0.25 · (0.41T + 0.67F + 0.70S))`.
**Lower is more dangerous.** 35 mm/h sustained for 15 minutes is ~9 mm (a third of an inch)
of rain in a quarter hour. **Fire-wide** is the median over all of a fire's streams: the
rain at which a typical stream becomes 50% likely to flow; USGS publishes the same figure
as its "guidance". The map also shows **P** (the chance of a flow at a chosen storm), **V**
(volume, m³) and **H** (hazard class 1–3); see §5.

**Ratios (ours ÷ USGS).** 1.00 is identical. **Threshold ratio 0.97** means our thresholds
are typically 3% lower: our map says flows start at slightly less rain (slightly more
cautious); above 1 would be less cautious. **Pairs** is how many streams could be compared.
**S ≈ 1.00** means terrain, stream network and soils line up, so T or F differences come
from the burn inputs. The terms do not weigh equally: at Wapiti's medians, T contributes
~2% of the sum `0.41T + 0.67F + 0.70S`, F ~58% and S ~40%. So **a T ratio of 1.5 moves the
threshold only ~1%, while an F ratio of 1.47 moves it ~20%**: disagreements in F (the dNBR)
matter most. (Grizzly's steeper, more severely burned catchments give T ~8% of the sum.)

**Scores against observed flows (8.0 B).** Hit rate = share of real debris flows the
threshold caught; false-alarm rate = share of storms without a flow that it warned for;
threat score = hits ÷ (hits + misses + false alarms), 1 is perfect; MCC, −1 to 1, is a
balanced skill score that also credits correct "no flow" calls.

### 8.0 How the results are assessed

Five methods, used throughout 8.1–8.9. Each result names the one(s) it uses.

**A. Agreement with USGS's own assessment**
- **Source:** USGS's published assessment of the same fire. Found by location through the
  USGS Post-Fire Hazard Assessment service (ArcGIS, `USGS_Post_Fire_Hazard_Assessment_Data`:
  748 assessment points, ~300 perimeters), with the ScienceBase archive (2013–2024) as the
  fallback. What we use from it: USGS's stream segments with T, F, S, threshold and drainage
  area, and USGS's fire-wide "Segment" guidance. Code: `pipeline/usgs_check.py` (find and
  fetch) and `pipeline/compare_usgs.py` (compare).
- **What:** do our inputs (T, F, S) and outputs (rainfall thresholds) match USGS's, stream
  by stream?
- **Why:** USGS's assessment is the operational product land managers use today. Matching
  it shows the automated pipeline is correct. Mismatches point to the input that differs.
- **How:**
  - USGS and wildcat cut streams into segments differently, so raw fire-wide medians are
    not like for like.
  - Each USGS segment is paired with our nearest segment. The pair is kept only if it is
    within 20 m (measured in metres, in UTM) and drains a similar area (ours ÷ theirs
    between 0.5 and 2).
  - For each pair: ours ÷ USGS for T, F, S and the threshold. Reported: the **median** of
    those ratios ("threshold ratio 0.97" = ours 3% lower), the number of pairs, and both
    sides' median threshold over the pairs.
  - Also reported: our fire-wide median threshold against USGS's published guidance.
  - Same fire check: a candidate is accepted only if at least one stream pairs. A USGS
    dNBR ratio outside 0.8–1.25 is flagged as "different imagery".
  - Reading it: 1.00 is identical. S ≈ 1.00 means the terrain, stream network and soils
    line up, so differences in T or F isolate the burn-severity inputs.

**B. Skill against observed debris flows**
- **Source:**
  - Grizzly Creek 2020 (the only fire here with a scored inventory): Rengers et al. storm
    and debris-flow observations (doi:10.5066/P9Z7RROL);
  - the USGS runoff-generated post-fire debris-flow inventory (doi:10.5066/P1R9VXC4);
  - rain-gauge peak 15-minute intensities.
  - Scripts: `m3/score_resolutions.py` and `graber/evaluation.py` on `wapiti-e2e`.
- **What:** do our thresholds predict where and when debris flows happened?
- **Why:** agreeing with USGS is not the same as being right. Observed flows are the real
  test, and the only way to say whether an input (DEM, severity) improves accuracy.
- **How:**
  - Each observation (one watershed, one storm, flow or no flow) is paired with our
    nearest segment within 150 m whose drainage area is within 2× the observed watershed's.
  - Predicted "flow" means the storm's peak 15-minute intensity ≥ that segment's threshold.
  - Scores:
    - hit rate: share of flows caught;
    - false-alarm rate: share of no-flow cases warned;
    - threat score: hits ÷ (hits + misses + false alarms);
    - Matthews correlation (MCC).
  - Year-1 observations only. Also scored for the gauge ≤ 4 km subset, and for the
    fire-wide threshold against the 49 storms of 2021.
  - Projects are compared on the observations matched in *every* project (162, 22 flows),
    so all rows are scored on the same cases.

**C. Cost: time and memory**
- **Source:**
  - `pipeline/measure.py` (`run_stage`; `m3/scaling.py` on `wapiti-e2e`);
  - Slurm `sacct` (Elapsed, MaxRSS, Start/End) on Lemhi;
  - hardware as recorded in each log.
- **What:** how long each wildcat stage takes and how much memory it needs.
- **Why:** research question 2, and sizing Slurm requests (memory is what decides which
  node a run fits on).
- **How:**
  - Each stage (preprocess, assess, export) runs in a **fresh process**, so its peak memory
    is its own. The child reports its own peak before exiting: Windows peak working set, or
    Linux max resident set.
  - Wall time is measured around the stage call, excluding interpreter start-up.
  - On Slurm, job Elapsed and MaxRSS come from `sacct`, and end-to-end time from the first
    job's Start to the last job's End.
  - Downloads are excluded unless stated ("cold start").
  - Memory is reproducible within ~1% between runs. Wall time on a busy desktop varies up
    to ~2×, so hardware comparisons use identical input files, run back to back.

**D. Split exactness**
- **Source:** `pipeline/split_fire.py compare`, a whole-fire run and a split run of the
  same project.
- **What:** does the watershed split change any result?
- **Why:** the split is only usable if it gives the whole-fire answer.
- **How:**
  - Segments are matched by exact geometry: coordinates rounded to 10⁻⁶ degrees, about
    0.1 m.
  - Reported:
    - identical-geometry count, and segments only in one run;
    - the largest absolute difference of every field (threshold, P, V, area, T, F, S) over
      matched segments;
    - hazard-class mismatches at 24 mm/h;
    - fire-wide median thresholds;
    - basin counts;
    - outlets by position: how many sit at the same place as the whole run's, and, in the
      merged result, outlets not at any basin's mouth and basins without an outlet (both
      should be 0).

**E. Controlled experiments**
- **Source:** projects built from byte-identical input files except the one under test.
- **What / Why:** isolate one factor (DEM resolution, DEM source, burn severity, dNBR,
  hardware) so a difference can be attributed to it.
- **How:**
  - Copy the other inputs file for file.
  - Change only the factor under test: for example the DEM averaged from the same 1 m
    LiDAR to 10, 5, 4, 3, 2, 1 m, or USGS's dNBR and severity swapped in one at a time.
  - Apply A, B or C to every variant.

**Also used:** a volume check against measured erosion (8.8: Gartner volumes summed over
each drainage's observed flow events at their peak 15-minute intensity, against net
channel erosion from repeat LiDAR, Rengers et al. 2023), and pixel comparisons of dNBR
rasters (8.4: ours resampled onto the reference grid; burned pixels are reference dNBR
> 100; median ratio, fitted slope, perimeter-mean ratio, correlation).

### 8.1 The pipeline reproduces USGS's assessments

- **Source:** USGS assessments `grz2020` (Grizzly Creek 2020, doi:10.5066/P142R8E2),
  `wap2024` (Wapiti 2024, doi:10.5066/P13GTMAX), and 2025 assessments of Big Bear, Rush and
  South Rim from the USGS assessment service; burn-severity inputs from the fires' BAER
  packages (USFS/USGS burn severity portal) or USGS's `inputs.zip`. Laptop runs, 2026-09-20
  to 09-28, `compare_usgs.py`.
- **Methods:** A.
- **What:** with the same kind of inputs USGS used, does our automated run give USGS's
  thresholds?
- **Why:** agreement with the operational product is the first test of correctness.
- **How:** run the pipeline, match every USGS segment to ours, compare T, F, S and the
  threshold; compare fire-wide medians with USGS's published guidance.
- **Result:**

  | Fire | Inputs | Pairs | Threshold ratio | Fire-wide, ours vs USGS (mm/h) |
  |---|---|---|---|---|
  | Grizzly Creek 2020, CO | BAER dNBR + BAER SBS | 3,118 | 0.99 | 26.2 vs 25.0 |
  | Wapiti 2024, ID | BAER dNBR + BAER SBS | 7,648 | 0.97 | 35.0 vs 34.8 |
  | Big Bear 2025, ID | USGS-published (BAER team) | 631 | 0.96 | 43.7 vs 45.0 |
  | Rush 2025, ID | USGS-published (BAER team) | 343 | 0.94 | 57.9 vs 62.5 |
  | South Rim 2025, CO | USGS-published (EROS BARC) | 353 | 1.00 | 25.7 vs 26.2 |

  Within 1–6% everywhere. A residual T ratio of 1.3–1.5 remains even with USGS's inputs
  (open item); USGS ran different DEM tile versions and its own wildcat build ("ocelote").
- **What it means:** given the burn-severity inputs USGS used, our automated run gives
  USGS's answer: stream-by-stream thresholds 0–6% lower than USGS's (ratios 0.94–1.00,
  i.e. slightly on the cautious side) and fire-wide figures within 0.2–4.6 mm/h.
  So for a fire USGS has not assessed yet, our map is a reasonable stand-in, provided the
  burn inputs are good (8.3, 8.4). The T disagreement matters little because T is a small
  part of the threshold ("Reading the numbers"). Limits: five fires; agreement with USGS
  is not proof of accuracy against real flows (that is 8.2–8.3).

### 8.2 1 m LiDAR does not change the answer

- **Source:** Grizzly Creek, LiDAR 1 m averaged to 10/5/4/3/2 m, BAER inputs, on Lemhi
  (2026-09-23); Grizzly 1 m sub-basin on the laptop (2026-09-20); Wapiti RECOVER 1 m LiDAR at
  10 m and 1 m on Lemhi (2026-09-23, 09-28). Observed flows: Rengers et al. Grizzly Creek
  inventory and storm table (doi:10.5066/P9Z7RROL) and USGS's runoff-generated debris-flow
  inventory (doi:10.5066/P1R9VXC4).
- **Methods:** E (only the DEM changes), with A, B and C.
- **What:** is a 1 m DEM more accurate than 10 m, and what does it cost?
- **Why:** research question 1; LiDAR is expensive to acquire and process.
- **How:** controlled experiments: identical dNBR, severity, soils and perimeter, only the
  DEM changes. Compare with USGS, and score thresholds against observed debris flows
  (hit rate, false-alarm rate, threat score) at Grizzly, the one fire with an inventory.
- **Result:**

  | Fire, DEM | Threshold ratio | Fire-wide (mm/h) | Observed flows caught (Grizzly) |
  |---|---|---|---|
  | Grizzly, LiDAR 10 m | 0.99 | 26.5 | 17 of 22 |
  | Grizzly, LiDAR 2 m | 0.99 | 26.3 | 17 of 22 |
  | Grizzly, LiDAR 1 m | 0.98 | 26.1 | 17 of 22 |
  | Wapiti, LiDAR 10 m | 0.97 | 35.0 | – |
  | Wapiti, LiDAR 1 m (split) | 0.97 | 35.1 | – |

  10 m → 1 m moves the threshold 0.1–1.5% for ~100× the cells, ~60× the memory and ~90×
  the assess time, and catches the same flows. Finer terrain raises T by ~10% (steeper
  slopes found) but the threshold barely moves: T is intersected with a 20–30 m burn-
  severity map, which a finer DEM cannot sharpen. **Burn severity, not terrain, is the
  binding constraint.**

  LiDAR finds somewhat more small channels (Wapiti: 12,034 segments on the USGS 10 m DEM,
  12,381 on LiDAR at 10 m, 13,229 at 1 m) but they line up a little less with USGS's
  streams, which USGS drew from the standard 10 m DEM (7,648 / 7,553 / 7,261 matched).
- **What it means:** for this USGS model, buying, storing and processing 1 m LiDAR does
  not produce a better hazard map; the standard 10 m DEM gives the same thresholds and
  catches the same debris flows. Effort is better spent on the burn-severity input.
  Limits: one fire (Grizzly, 22 flows) scored against observed flows; Wapiti confirms the
  thresholds but has no flow inventory yet. This is about this model: a model built to use
  fine terrain, or LiDAR's value elsewhere (e.g. mapping deposits or runout), is not tested.

### 8.3 The burn-severity input matters more than the DEM

- **Source:** Grizzly Creek, seven projects with byte-identical DEM, soils and perimeter;
  MTBS (extended, 2021 imagery), BAER, Sentinel-2; scored against the observed-flow
  inventories above (2026-09-24).
- **Methods:** E (only dNBR and severity change), with B; A for Big Bear.
- **What:** does input *accuracy* matter where resolution did not?
- **Why:** it decides where effort should go (Keith's point).
- **How:** vary only dNBR and severity; score each against 162 observed watershed-storms.
- **Result:** flows caught: MTBS 12/22 → BAER severity 16/22 → estimated severity 18/22
  (NED vs LiDAR at 10 m: 16 → 17; 10 m → 1 m: 17 → 17). The dNBR source hardly matters when
  the severity map is the same; the severity classification does. Big Bear 2025 shows the
  same thing against USGS: swapping in USGS's dNBR and severity moves the threshold ratio
  0.56 → 0.73 / 0.75 (one input) → 0.96 (both).
- **What it means:** the burn-severity map is the input worth getting right: changing it
  moved results far more than any DEM change. Use field-checked severity (BAER) when it
  exists, which the pipeline now does automatically: USGS's published inputs first, then the
  Forest Service BAER map (8.10).
  **Caveat, easy to misread:** estimated severity caught the most flows (18/22), but that
  does not make it the most accurate severity map. It over-classifies moderate/high burn
  (T 1.2–1.3× USGS, vs 1.05 with BAER's map), which lowers thresholds so more flows get
  caught, with a similar false-alarm rate (0.21–0.23 vs 0.20–0.21). Two extra flows at one fire is not a
  significant difference. At Big Bear the same over-classification made thresholds half of
  USGS's (0.56). Estimated severity is a workable fallback, not a better product.

### 8.4 Sentinel-2 dNBR is correct; scene choice drives the differences

- **Source:** Wapiti BAER package (includes the Sentinel-2 reflectance BAER used), Grizzly
  BAER dNBR, USGS-published dNBR for Big Bear and Rush; our Sentinel-2 dNBR (2026-09-28).
- **Methods:** pixel comparison of dNBR rasters (8.0, "also used"); A for the F ratios.
- **What:** why does our dNBR sometimes read ~1.4× USGS's (Big Bear, Woodhead)?
- **Why:** fires without published inputs depend on our dNBR.
- **How:** same sensor and dates as BAER (Wapiti), compare reflectance and dNBR pixel by
  pixel; then compare our rasters with every reference raster available.
- **Result:** BAER applies the same Collection-1 reflectance offset (−0.1) we do; on the
  same scenes our dNBR is 1.04–1.09× theirs (correlation 0.98). Where our scenes differ, the
  ratio ranges 0.88 (Rush) to 1.18–1.33 (Big Bear, depending on the post-fire date). The
  "1.4×" is scene choice, not processing. Expect ±20% on F for Sentinel-2-only fires; the
  pipeline uses USGS-published inputs whenever they exist.
- **What it means:** our satellite processing is right; the difference is which days'
  images are compared. Because F carries ~60% of the threshold, ±20% on F is roughly
  ±10–15% on thresholds for fires where only Sentinel-2 is available: treat those maps as
  approximate, and prefer published BAER inputs when they appear. Fixing the date rule for
  fires that burn for months (Big Bear) is an open item.

### 8.5 Compute: what HPC helps with

- **Source:** Grizzly and Wapiti runs on the team's standard computer (AMD Ryzen 5 5600X,
  6 cores, 32 GB; "laptop" in older notes and elsewhere in this section) and on
  Lemhi regular nodes (40 cores, 192 GB), 2026-09-20 to 09-28, timed per stage with
  `measure.py` or Slurm `sacct`.
- **Methods:** C, with E for the hardware comparison (identical files).
- **What:** does a cluster make a fire faster, or possible?
- **Why:** research question 2.
- **How:** resolution sweep (cost vs cells), core-count sweep (1–40 cores, `taskset`),
  wildcat's own basin parallelism, and the watershed split.
- **Result:**
  - Cost is regular in cell count: time ~linear, memory slightly sublinear up to 2 m;
    1 m is ~1.5× worse than the curve predicts.
  - **More cores do not speed up one run** (≤ 10% from 1 to 40 cores; most of wildcat's
    work is single-threaded), and wildcat's `parallelize_basins` is 4.7× *slower*.
  - A Lemhi core is slower than the standard computer's for this work (Grizzly 10 m assess
    1.3×, 2 m 1.9×; Wapiti 10 m with identical files, whole run 1.37×, assess 1.42×).
  - So HPC helps through **memory** (1 m runs) and **throughput** (many runs or pieces at
    once), not per-run speed. The split makes that concrete (8.6, 8.7).
- **What it means:** a 10 m assessment of any single fire runs fine on an ordinary
  computer in minutes; the cluster will not make it faster. Use the cluster when a run
  needs more memory than a computer has (1 m on a large fire), or to run many fires or
  pieces at the same time (e.g. every fire of a season, or all pieces of a 1 m run). When
  asking Slurm for resources, ask for memory, not cores (1–4 cores per run is enough).

### 8.6 The watershed split is exact at 10 m and near-exact at 1 m

- **Source:** Wapiti, Woodhead, Big Bear at 10 m; Grizzly 1 m sub-basin (laptop,
  2026-09-28); Wapiti at 1 m, whole fire on Lemhi's 3 TB node (job 1456278) against the
  36-piece split (Lemhi, 2026-10-08); `split_fire.py compare`.
- **Methods:** D, and C for memory.
- **What:** does splitting a fire into drainage pieces change any result?
- **Why:** the split is only useful if it gives the whole-fire answer.
- **How:** run each fire whole and split; compare every segment's geometry and fields.
- **Result:** 10 m: Wapiti 12,029 / 12,034, Woodhead 7,779 / 7,781, Big Bear 1,328 / 1,328
  segments identical, every field exact, 0 hazard mismatches, identical fire-wide medians
  (the few others are re-cuts at junctions with masked rivers). 1 m sub-basin: 473 / 483
  identical, 0 hazard mismatches, fire-wide +0.5%; the differences are tributary mouths
  stopped 10–14 m short of a masked inflow river, because the plan runs at 10 m. Outlets:
  every merged outlet is at a merged basin's mouth and every basin has its outlet; Wapiti
  10 m 1,361 of the whole run's 1,365 outlets at the same place, the 1 m test area 43 of 51
  (the rest at those shortened tributary mouths). Memory per job falls 1.3–3.3× at 10 m; on
  one laptop the split is slower (padded pieces, one process each), so it is for grids too
  big for one node.

  **Wapiti at 1 m, full scale** (whole fire vs 36 pieces, ~2 billion cells): 12,813 of the
  whole run's 13,435 segments identical (95.4%), every field exactly equal on all of them
  and 0 hazard mismatches; fire-wide 35.106 vs 35.098 mm/h (0.02%). The 622 whole-run
  segments without an exact twin (and 416 split-only ones) are the network cut slightly
  differently where tributaries meet a river flowing in from another piece; Wapiti has
  many more such junctions than the test area. The same junctions give the split 61 more
  terminal basins (1,458 vs 1,397). Every split outlet is at its basin's mouth; 1,103 sit
  where the whole run's are. (The split writes one outlet per basin; wildcat omits nested
  drainages' outlets, 1,342 for 1,397 basins.) Cost:

  | Wapiti, 1 m LiDAR | Whole fire, 3 TB node | Split, 36 regular nodes |
  |---|---|---|
  | Wall time | 12 h 34 min (assess 12.3 h) | ~59 min (49.5 min re-run) |
  | Largest job | 280 GB | 50 GB preprocess; pieces ≤ 40 GB |
  | Total assess CPU time | 44,435 s | 30,512 s |
- **What it means:** a fire can be cut into pieces and run on many nodes without changing
  the hazard map: at 10 m, stream for stream, it is the same answer; at full-fire 1 m every
  stream present in both runs carries exactly the same values, no hazard class changes and
  the fire-wide threshold moves 0.02%. What differs is a few percent of tributary ends near
  piece boundaries, drawn ~10 m shorter. At 1 m the split is also ~13× faster, fits regular
  nodes instead of the one 3 TB node, and uses ~30% less total model CPU time (the model's
  cost grows faster than the grid at 1 m, so small pieces are cheaper in total). Use it
  only when a fire does not fit one node; at 10 m it just adds overhead.

### 8.7 Wapiti: laptop vs HPC, 10 m vs 1 m

- **Source:** Wapiti 2024 (2024-IDBOF-000683), BAER dNBR + soil burn severity (Sentinel-2,
  2023-09-15 / 2024-09-24), STATSGO, RECOVER LANDFIRE EVT, WFIGS perimeter (522 km²); DEMs:
  USGS 3DEP 10 m, RECOVER 1 m LiDAR (averaged to 10 m, or native 1 m). Compared with USGS
  `wap2024` (guidance 34.8 mm/h). Full write-up, logs and scripts:
  `wapiti_results_2026-09-28.zip` (shared with the team).
- **Methods:** A, C and E (rows 1–2: identical input files; 1 m vs 10 m LiDAR: only resolution).
- **What:** same fire, same burn inputs: what do a standard computer and the cluster take,
  at 10 m and at 1 m, and do the results differ?
- **Why:** research questions 1 and 2 on one fire, as a table for the team.
- **How:** wildcat preprocess/assess/export, each stage timed with its peak memory; 1 m via
  the watershed split as a 36-task Slurm array.
- **Result:**

  | Run | DEM | Wall time (wildcat) | Peak memory | Threshold ratio | Fire-wide (mm/h) |
  |---|---|---|---|---|---|
  | Standard computer, whole fire | USGS 10 m | 184 s (assess 160 s) | 2.4 GB | 0.97 | 35.0 |
  | Lemhi, whole fire (1 node, Xeon Gold 6148) | USGS 10 m, identical files | 253 s (assess 227 s) | 2.4 GB | 0.97 | 35.0 |
  | Lemhi, whole fire | LiDAR at 10 m | 637 s (assess 271 s) | – | 0.97 | 35.0 |
  | Lemhi, whole fire, regular node | LiDAR 1 m | failed at 2 h 30 m (168 GB) | > 168 GB | – | – |
  | Lemhi, whole fire, 3 TB node | LiDAR 1 m | 12 h 34 min (assess 12.3 h) | 280 GB | 0.97 | 35.1 |
  | Lemhi, 36-piece array | LiDAR 1 m | ~59 min end to end | 47.8 GB prepare, 40 GB largest piece | 0.97 | 35.1 |

  At 1 m the whole fire needs the single 3 TB node and 12.5 hours; split, it runs on regular
  nodes in under an hour with the same answer (8.6), and both give the 10 m answer. With identical input
  files, one Lemhi node reproduces the standard computer's result exactly (same 7,648
  pairs and ratios) but takes 1.37× as long (assess 1.42×): per run, the cluster is slower.

  **Building the pieces inside the array tasks (re-run 2026-09-28, jobs 1497003–1497005)**
  gave an identical result but saved only 8%: plan through merge 49 min 28 s vs 53 min 54 s
  (the earlier run's input preparation and preprocess, reused here, excluded). Cutting the
  pieces took 19,740 s summed across the 36 tasks against 854 s when one node cut them
  all in turn, so the slowest piece grew from 35:49 to 45:46 while its assess time held
  (1,958 vs 1,962 s). Likely cause, not yet verified: one node reading the same rasters 36
  times had them in its file cache; 36 nodes each read them cold from the shared file
  system at once, and strip-organized rasters make every window read full-width rows.
- **What it means:** on the same fire and burn inputs, every configuration lands on the
  same answer, 35.0–35.1 mm/h against USGS's 34.8, a threshold 3% lower than USGS's
  stream for stream. The computer does not change the answer (rows 1–2 identical), and
  neither does 1 m terrain (35.1 vs 35.0). What changes is cost: 3 minutes on a desktop,
  4 on one cluster node, and for 1 m either 12.5 hours on the cluster's single 3 TB node or
  under an hour across 36 regular nodes. For Wapiti, the practical product is the 10 m run
  on an ordinary computer.

### 8.8 The volume model overpredicts ~4–6×

- **Source:** Grizzly Creek measured channel erosion from repeat LiDAR (Rengers et al.
  2023); our Gartner 2014 volumes at every resolution (2026-09-24).
- **Methods:** volume check (8.0, "also used"); A for ours vs USGS.
- **What / Why:** is the volume shown on the map realistic?
- **How:** per drainage, sum the model volume over each observed flow at its peak I15;
  compare with measured net channel erosion.
- **Result:** our volumes match USGS's (0.95–1.09), but the model overpredicts measured
  erosion by a median ~4–6× (the published figure is 4.4×) while ranking drainages well
  (log correlation 0.83). Resolution does not change it. The map says so next to volume.
- **What it means:** use the volumes to compare drainages (which could produce the larger
  flow), not as an estimate of how much material will come down: the numbers run several
  times too high. The hazard class, which combines likelihood and volume, inherits some of
  that. Limits: one fire, ~30 drainages, channel erosion only (deposits on fans excluded).

### 8.9 From a name to a map in minutes

- **Source:** end-to-end runs through the app and `run_fire.py` (laptop, 2026-09-24 to 09-28).
- **Methods:** C (end-to-end stage timings, `data/<key>_run_timings.json`); A.
- **Result:** South Rim 2025 through the app, never run before: 54–100 s (USGS 1.00×). Big
  Bear 2025: 36 s to 2 min. Wapiti 2024 from a cold start (every cache emptied): ~4 min.
  USGS published its Wapiti assessment two months after ignition; the real lower bound for
  us is the first clear post-fire satellite scene.
- **What it means:** computing is no longer the bottleneck: once burn-severity data exist,
  a hazard map takes minutes, not weeks. The waiting is for a cloud- and smoke-free
  satellite image after the fire, or for BAER's field-checked severity map (which USGS's
  published inputs then provide). A manager could have a first map as soon as a clear
  image exists, and a better one when BAER's arrives.

### 8.10 The free fallbacks: the BAER severity map helps most

- **Source:** laptop runs on 2026-10-08, compared with USGS assessments:
  - `wap2024`, `boi2024` and `wdh2020` from the USGS assessment service;
  - Forest Service BAER soil burn severity maps from its public image service;
  - Lemhi batch `app_20261008-114917` for Bush and Archie Creek (3.8).
- **Methods:**
  - E: one input changes, everything else byte-identical;
  - A: stream-matched comparison with USGS.
- **What:** do the fallbacks added in 4.1 make results closer to USGS's, and do any of them
  change results they should not?
- **Why:**
  - Most fires have no USGS-published inputs, so they run on the fallbacks.
  - A fallback that silently shifts thresholds would be worse than none.
- **How:** re-run each fire with only the fallback's input changed and compare with the
  previous run and with USGS.
- **Result, burn severity** (dNBR from Sentinel-2 in every row; only the severity map
  changes):

  | Fire | Severity | Pairs | Threshold ratio | T ratio | Fire-wide, ours vs USGS (mm/h) |
  |---|---|---|---|---|---|
  | Wapiti 2024, ID | estimated from dNBR | 7,643 | 0.883 | 2.91 | 30.9 vs 34.8 |
  | Wapiti 2024, ID | Forest Service BAER map | 7,648 | **0.952** | 1.51 | **34.5 vs 34.8** |
  | Boise 2024, CA | estimated from dNBR | 555 | 0.856 | 2.00 | 37.0 vs 47.3 |
  | Boise 2024, CA | Forest Service BAER map | 564 | **0.932** | 1.25 | **44.0 vs 47.3** |
  | Woodhead 2020, ID | estimated from dNBR | 7,850 | 0.847 | 1.36 | 31.6 vs 37.4 |
  | Woodhead 2020, ID | Forest Service BAER map | 7,900 | 0.845 | 1.53 | 31.7 vs 37.4 |

  For reference, Wapiti with BAER's own dNBR and severity map gives 0.97 (8.1). With
  Sentinel-2 dNBR and the same BAER map, 0.952: our dNBR costs about two points.
- **Result, Sentinel-2 with estimated severity at scale** (Lemhi, before the BAER map step
  existed):
  - Bush 2020, AZ: 0.978 on 18,293 pairs; fire-wide 37.4 vs 37.3 mm/h.
  - Archie Creek 2020, OR: 0.940 on 19,499 pairs; fire-wide 17.2 vs 19.1 mm/h.
- **Result, the other fallbacks:**
  - **Missing KF filled (Wapiti 2024, SSURGO ×1.35 then the median):**
    - segments without a threshold 526 → **0**;
    - threshold ratio 0.8831 → 0.8830 on the same 7,643 pairs;
    - fire-wide 30.48 → 30.86 mm/h.
  - **DEM from the 3DEP image service instead of TNM S3 (Wood 2024):**
    - pixels: median difference 0.20 m;
    - segments: 630 → 624;
    - fire-wide +0.08% (73.09 → 73.15 mm/h);
    - the 310 segments with the same start and end points in both: median ratio 1.0000,
      90% within 0.997–1.006.
  - **Landsat 8/9 against Sentinel-2 (Wood 2024):**
    - same-day NBR: r = 0.84, Landsat 0.04 higher (an offset dNBR cancels);
    - dNBR where the fire burned between near-matching date pairs: 0.99×;
    - left to choose its own dates: 0.59×.
  - **Sentinel-2 composite (Grizzly 2024, AK):** no single clear pre-fire date; a composite
    of 1 pre and 6 post dates covers 97.8% of the perimeter.
- **What it means:**
  - **The severity map is again the input that moves results** (as in 8.3). Where a Forest
    Service BAER map exists, using it instead of estimating severity brings Wapiti and
    Boise within 5–7% of USGS. Wapiti's fire-wide figure lands within 0.3 mm/h of USGS's.
  - **Woodhead shows the limit:** its gap is the dNBR (our scenes read 1.4× USGS's, 8.4),
    which no severity map fixes.
  - **The other fallbacks do what they should and little else:**
    - Filling KF puts every stream on the map without moving the comparison.
    - The DEM fallback changes the fire-wide threshold by 0.08%.
    - The Landsat processing agrees with Sentinel-2 on matching dates; its risk is date
      choice, which is why it comes after Sentinel-2.
  - **Limits:**
    - The severity result rests on three fires.
    - Woodhead's T ratio rose with the BAER map (1.36 → 1.53) without changing its
      threshold ratio, so "closer T" and "closer threshold" do not always go together.
    - No lower-48 fire has needed the composite or Landsat end to end yet.

---

## 9. Things that will bite you

- **`wildcat assess` from the CLI dies** (`pysheds 0.4` calls `np.in1d`, removed in NumPy
  2). `measure.py` and `run_wildcat.py` install `np.in1d = np.isin` before importing
  wildcat. Never run `wildcat assess` from the command line.
- **pfdf and wildcat are not on PyPI**: they install from the USGS package index in
  `pyproject.toml` (`uv sync`).
- **The DEM must be projected before preprocess.** TNM tiles arrive in degrees; unprojected
  pixels are ~8 × 10 m and pass wildcat's resolution check while being wrong.
  `prepare_fire.py` reprojects to square 10 m UTM.
- **pfdf's own 10 m DEM fetcher depends on TNM's search index.** Until about 2026-09-24 it
  returned no Idaho tiles (they were not indexed as "Current"); it finds them now. It also
  cannot fetch older tile versions. `prepare_fire.py` reads the TNM S3 tiles directly (by
  name, current or a pinned version) and uses pfdf's search only as a cross-check.
- **DEM versions change**: TNM revises tiles (Wapiti: >1 m differences on 2–45% of pixels).
  `--dem-version YYYYMMDD` reproduces an older assessment.
- **Finer than 6.5 m needs config, not code**: `resolution_check = "none"`, and
  `confinement_neighborhood` is in pixels (scale it: 40 at 1 m). `prepare_fire.py
  --lidar-res` does both.
- **BAER and MTBS rasters mislabel nodata** (0 declared, −32768 or 255 real);
  `prepare_fire.py` takes the true mask.
- **WFIGS `f=geojson` returns invalid perimeters** (islands as extra shells);
  `recover.py` requests Esri JSON and assembles rings itself.
- **GDAL network reads can hang a process at exit on Windows**; `prepare_fire.py` runs
  those downloads in a child process it kills after a `.done` marker.
- **Geodatabase export**: shapefiles mix Polygon/MultiPolygon, UTM perimeters fail, and big
  multipart perimeters trip OGR's polygon organizing; the exporter writes Multi* types in
  WGS 84 with `OGR_ORGANIZE_POLYGONS=SKIP`.
- **Fire names are not unique**; always key by Unique Fire ID.
- **Folder prefixes collide** (`wapiti_*` vs `wapiti_2026_*`): match data folders to the
  longest fire key, as the app's delete does.
- **`parallelize_basins`**: leave it off (4.7× slower on Lemhi, unusable on Windows).
- **Parallel runs and numba's cache.** pfdf/pysheds compile with numba and cache the code next
  to the library. Several processes building that cache at once corrupt it: from an empty
  cache, 2 of 3 parallel fires failed in assess ("KeyError: int64 generator(...)"); once it
  existed, all ran in parallel. The local batch and split run one fire or piece alone first;
  the Slurm scripts give each job a private `NUMBA_CACHE_DIR` (~40 s of compiling per job).
- **Starting a detached job over SSH:** background only the `nohup` command, with every
  stream redirected (`cd dir || exit 1; nohup setsid cmd > log 2>&1 < /dev/null &`). A
  backgrounded `cd dir && nohup cmd ... &` is a subshell that keeps the SSH channel open, so
  the caller hangs until the job ends.
- **STATSGO KF gaps are 0, not nodata.** pfdf's STATSGO COGs store a missing map unit as 0
  (sometimes -0.1) with nodata NaN; wildcat drops KF <= 0 (`constrain_kf`). Test `kf > 0`,
  not nodata, to find gaps (`prepare_fire.fill_kf`).
- **Soil Data Access answers `{}` to a query it could not run**, rejects `DECLARE`, and stores
  `kffact` as text (cast it to float). An empty result is not proof of no data.
- **SSURGO and STATSGO KF are on different scales** (Wapiti 0.74x, Woodhead 1.11x); rescale
  before mixing them (4.1).
- **A fire's dates go stale.** `new_fire.py` writes a config once and leaves it, so a fire
  first run while burning kept its containment date (or the ignition + 90 d stand-in), and
  containment sets the post-fire image window. `recover.py` now refreshes ignition and
  containment from WFIGS on every run and prints any change; `"dates_locked": true` in a
  config keeps hand-set dates. Other saved inputs (DEM, soils, EVT, RECOVER files) stay
  cached until `--cold`.
- **USGS's 3DEP image service mixes resolutions.** Its default mosaic serves 1 m LiDAR where
  it exists; lock the export to the 1/3 arc-second items (`LowPS` ~10.3) to get the 10 m
  product. It answers 500/502 now and then, and blocks of 4000 x 4000 drew 500s: retry and
  keep blocks at 2000. Requests on the tiles' native degree grid return mostly no-data.
- **Landsat for free:** Earth Search's Landsat assets are in a requester-pays bucket (billed);
  use the Planetary Computer and its anonymous token for collection `landsat-c2-l2` (the
  endpoint wants the collection id, not `landsat-c2`). Its token and search endpoints return
  the odd 504; retry.
- **Small GDAL network-read scripts hang at exit on Windows** (a 2x2 window or a whole band,
  Sentinel-2 or Landsat, launched from bash or Python), while `sentinel2_dnbr.py`, which
  reads through `WarpedVRT`, exits normally. Root cause not found (see `prepare_fire.py`
  fetch_cache for the same symptom). Test network reads through the pipeline scripts, or
  with a timeout.
- **wildcat's outlets file is not in basin order** (nested drainages are removed between
  saving basins and outlets): pair outlets with basins by location (section 7).

---

## 10. Open items

**Validation and science**
- [x] Wapiti on Lemhi with the USGS 10 m DEM: identical result, 1.37× slower (8.7).
- [ ] Residual T ratio 1.3–1.5 vs USGS even with USGS's inputs (DEM version, perimeter,
      or USGS's wildcat build).
- [ ] Sentinel-2 date rule for long fires (Big Bear: post date 20 days into a four-month
      fire); BAER scene IDs for Big Bear and Woodhead would close 8.4.
- [ ] A scored second fire: the Wapiti debris-flow inventory (Robby Jost, Boise State).
- [ ] Is the RECOVER 1 m LiDAR layer bare earth? (Keith)

**HPC**
- [x] Lemhi SSH access (2026-10-07).
- [x] Batch many fires: `pipeline/batch.py`, locally or as one Slurm array (3.7).
- [x] Check compute nodes reach the data hosts: they do not (no internet); batches are staged.
- [x] Run a first staged batch on Lemhi: 5 fires, ~7 min (3.7).
- [ ] Lower the batch task memory request (16 GB -> ~6 GB) after a larger batch confirms peaks.
- [ ] Ask the cluster staff whether input downloads and light raster work on the login node
      are acceptable, or whether there is a data-transfer node for them.
- [ ] Single-fire Slurm runs (`run_fire.slurm`, the app's Slurm runner) on Lemhi: run the
      inputs phase on the login node first, as batches do.
- [x] App: select several fires and run them as a batch, here or on a cluster over SSH, with
      finished fires copied back (3.8).
- [x] Run an app batch against Lemhi end to end: 3 fires including Bush 2020 (~780 km²),
      copied back with USGS checks (3.8).
- [ ] Confirm the fixed launch on Lemhi: a new batch shows *running* within seconds.
- [ ] Choose whole-fire vs split automatically by grid size.
- [x] Re-time the 1 m array with the build inside the tasks: 49.5 min, only 8% faster (8.7).
- [ ] Faster piece builds: write the preprocessed rasters as tiled GeoTIFFs once in the
      prepare job so each task reads only its tiles (or build in the prepare job, in parallel).

**Product**
- [ ] Publish to ArcGIS Online: a scheduled, reviewed pipeline (plan in 3.9). Needs an org
      with public sharing and a Creator/Publisher service account.
- [x] Perimeter check vs stale WFIGS acreage: accepted on the incident size (4.1).
- [x] Fill missing KF: SSURGO rescaled to STATSGO, then the fire's median; Wapiti 526 -> 0
      segments without a threshold (4.1).
- [x] BAER soil burn severity for fires USGS did not assess: the Forest Service image
      service, free; Wapiti 2024 ratio 0.883 -> 0.952, Boise 0.856 -> 0.932 (4.1).
- [x] Cloud fallbacks for dNBR: Sentinel-2 composite, then Landsat 8/9 (Planetary Computer,
      free) single dates and composite (4.1).
- [x] DEM fallback for a TNM S3 outage: USGS's 3DEP image service, 1/3 arc-second items
      only, version recorded; Wood fire-wide threshold +0.08% (4.1).
- [x] Fires whose WFIGS perimeter crosses a national border: clipped to US elevation
      coverage and recorded (Border 6 2026: 3.7 of 10.6 km2 removed); other fires unchanged (4.1).
- [ ] Run a lower-48 fire that actually needs the composite or Landsat end to end (so far:
      Grizzly 2024, Alaska, dNBR step only; Wood 2024 Landsat forced).
- [ ] LANDFIRE EVT for Alaska, Hawaii, Puerto Rico.
- [ ] Authentication before the app is served beyond localhost.
- [ ] Tests (no test tooling yet).

**Project**
- [ ] Commit `staging` (and the research on `wapiti-e2e`); both are uncommitted.
- [ ] Showcase 2026-10-13: findings one-pager and a rehearsed demo.
