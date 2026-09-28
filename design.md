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
- Access today: Open OnDemand (file upload + shell); direct SSH is blocked (section 10).

---

## 4. Inputs, sources, fallbacks

| wildcat input | Primary | Fallback | Last resort |
|---|---|---|---|
| perimeter | WFIGS final perimeter | – | – |
| dem | USGS 3DEP 1/3 arc-second (10 m), TNM S3 tiles | – (optional: RECOVER 1 m LiDAR, `--lidar-res`) | – |
| dnbr | USGS-published inputs (BAER team or EROS BARC) | Sentinel-2 dNBR computed by rule | – |
| severity | USGS-published soil burn severity | estimated by wildcat from dNBR (125/250/500) | – |
| kf | STATSGO KFFACT (pfdf) | – (gap: SSURGO) | – |
| evt (water/developed mask) | RECOVER bundle (LANDFIRE) | LANDFIRE ImageServer (LF2016–2025) | none (nothing masked) |
| validation | USGS assessment service (by location) | ScienceBase archive (by name) | "no assessment" |

All sources are public and need no account, key or email.

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
| `app/server.py` | Local web app: lookup, runs (local or Slurm), map data, exports, keep/delete/trash |
| `map/template.html` | The whole front end (Leaflet): map, storm slider, popups, legend, formulas, app panels |
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
     segment, outlets pair with their basin; IDs renumbered into `<project>_split`.
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
    - basin and outlet counts.

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
  exists, which the pipeline now does automatically via USGS's published inputs.
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
  2026-09-28); `split_fire.py compare`.
- **Methods:** D, and C for memory.
- **What:** does splitting a fire into drainage pieces change any result?
- **Why:** the split is only useful if it gives the whole-fire answer.
- **How:** run each fire whole and split; compare every segment's geometry and fields.
- **Result:** 10 m: Wapiti 12,029 / 12,034, Woodhead 7,779 / 7,781, Big Bear 1,328 / 1,328
  segments identical, every field exact, 0 hazard mismatches, identical fire-wide medians
  (the few others are re-cuts at junctions with masked rivers). 1 m sub-basin: 473 / 483
  identical, 0 hazard mismatches, fire-wide +0.5%; the differences are tributary mouths
  stopped 10–14 m short of a masked inflow river, because the plan runs at 10 m. Memory per
  job falls 1.3–3.3× at 10 m; on one laptop the split is slower (padded pieces, one process
  each), so it is for grids too big for one node.
- **What it means:** a fire can be cut into pieces and run on many nodes without changing
  the hazard map: at 10 m, stream for stream, it is the same answer. At 1 m a handful of
  small tributaries end ~10 m sooner where they join a big river from another piece; no
  hazard class changes and the fire-wide threshold moves 0.5%, which a map user would not
  see. Use it only when a fire does not fit one node; otherwise it just adds overhead.

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
  | Lemhi, whole fire | LiDAR 1 m | failed at 2 h 30 m (168 GB) | > 168 GB | – | – |
  | Lemhi, 36-piece array | LiDAR 1 m | ~59 min end to end | 47.8 GB prepare, 40 GB largest piece | 0.97 | 35.1 |

  The 1 m run is only possible split, and it gives the 10 m answer. With identical input
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
  4 on one cluster node, and an hour across 36 nodes for 1 m, which is the only way 1 m
  runs at all. For Wapiti, the practical product is the 10 m run on an ordinary computer.

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
- **pfdf's own 10 m DEM fetcher returns nothing for Idaho** (a TNM dataset-name quirk);
  `prepare_fire.py` reads the TNM S3 tiles directly.
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
- [ ] Lemhi access from the laptop/app: SSH jump host closes after the password; question
      with c3plus3 support. Then the app's Slurm runner can submit directly.
- [ ] Batch many fires as one Slurm array (on hold until access is sorted).
- [ ] Choose whole-fire vs split automatically by grid size.
- [x] Re-time the 1 m array with the build inside the tasks: 49.5 min, only 8% faster (8.7).
- [ ] Faster piece builds: write the preprocessed rasters as tiled GeoTIFFs once in the
      prepare job so each task reads only its tiles (or build in the prepare job, in parallel).

**Product**
- [ ] Publish to ArcGIS Online (needs an org publisher account and the `arcgis` package).
- [ ] Soils fallback where STATSGO has no KF (SSURGO); Wapiti has 528 segments without M1.
- [ ] LANDFIRE EVT for Alaska, Hawaii, Puerto Rico.
- [ ] Authentication before the app is served beyond localhost.
- [ ] Tests (no test tooling yet).

**Project**
- [ ] Commit `staging` (and the research on `wapiti-e2e`); both are uncommitted.
- [ ] Showcase 2026-10-13: findings one-pager and a rehearsed demo.
