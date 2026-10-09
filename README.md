# Debris Flow Hunters

Post-fire debris-flow hazard maps for any US wildfire, in minutes instead of weeks.

After a wildfire, burned slopes can turn a heavy rainstorm into a debris flow. The USGS
assesses that hazard for major fires with its published models, but an assessment takes
days to weeks of specialist work. This project automates it: pick a fire by name, and the
pipeline downloads the fire's perimeter, terrain, soils and burn severity, runs the USGS
models (through the USGS `wildcat` tool), checks the result against USGS's own assessment
when one exists, and shows it on an interactive map you can export to GIS formats.

Built for R-CON 2026 (Challenge 01: Wildfire Resilience) by the Debris Flow Hunters team.
The intended users are land managers and emergency managers.

**Research prototype.** Not an official USGS product and not for operational decisions.

## What you get for each fire

- **Stream segments** with, for each:
  - the **rainfall threshold**: the 15-minute rainfall intensity (mm/h) at which a debris
    flow becomes 50% likely (USGS M1 model, Staley et al. 2017);
  - the **likelihood** of a debris flow at a chosen storm;
  - the **volume**, with its 95% range (Gartner et al. 2014);
  - the **combined hazard class**, 1 to 3 (Cannon et al. 2010).
- **A map** with a storm slider: drag it and every stream recolours to show where that
  storm would trigger debris flows.
- **Exports**: GeoJSON, CSV, zipped shapefiles, or a zipped Esri file geodatabase (no ArcGIS
  needed).
- **A comparison with USGS's own assessment** of the same fire, when USGS has published one.
- **A record of where every input came from**, shown in the app under **Logs > Input data
  for this fire**.

## Where the data comes from

Every input is downloaded from a public service. All of them are free and need no account,
key or email. When the first choice is missing or unavailable, the pipeline falls back to
the next one and records which it used.

| Input | First choice | Fallbacks, in order |
|---|---|---|
| Fire perimeter | WFIGS interagency perimeter | if WFIGS is down: the perimeter saved by the fire's last run → the fire's final daily perimeter from its RECOVER package (checked against WFIGS's last acreage) |
| Elevation (10 m) | USGS 3DEP tiles (The National Map) | the same tiles from USGS's 3DEP elevation service, if the first is down |
| Burn imagery (dNBR) | the BAER files USGS published with its own assessment | Sentinel-2, one clear date before and after the fire → Sentinel-2 built from several dates → Landsat 8/9 single dates → Landsat 8/9 built from several dates |
| Burn severity | the BAER map USGS published | the Forest Service BAER team's map → estimated from the burn imagery |
| Soil erodibility | STATSGO | SSURGO (USDA), rescaled to STATSGO → the fire's median value |
| Vegetation (masks water and towns) | LANDFIRE, from the fire's RECOVER package | LANDFIRE's public service → none |

A few things worth knowing:
- **Burn severity matters most.** A field-checked BAER map gives results much closer to
  USGS's than an estimate (Wapiti 2024: 0.95 of USGS's thresholds against 0.88).
- **Fires that cross the border into Mexico or Canada** are modeled on the US side only,
  where there is US elevation data. The app says how much area was left out.
- **Dates are refreshed** from WFIGS on every run, so a fire first run while it was still
  burning gets its final containment date. Other downloads (elevation, soils, vegetation)
  are kept after the first run; `--cold` downloads them again.

`design.md` §4 explains why each source and fallback was chosen, and lists the exact APIs.

## Install

You need about 2 GB of disk for the software, plus 0.1 to 4 GB per fire, and an internet
connection (every input is downloaded from public services; no accounts or keys).

1. Install [uv](https://docs.astral.sh/uv/), which installs the right Python and every
   package for you:
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   - macOS / Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Get the code and install:
   ```bash
   git clone <this repository> Debris-Flow-Hunters
   cd Debris-Flow-Hunters
   git checkout staging
   uv sync
   ```
   `uv sync` fetches Python 3.12 and the USGS packages `pfdf` and `wildcat` from the USGS
   package index named in `pyproject.toml` (they are not on PyPI, so plain `pip install`
   will not find them).

## Run

### The app (recommended)

```bash
uv run python app/server.py
```

Open http://127.0.0.1:8000. Then:

1. **Add a fire**: type a fire name and year (and a two-letter state if the name is
   common), or paste a Unique Fire ID such as `2025-IDPAF-005844`. Click **Find fire** and
   pick the right fire from the list.
2. Click **Run assessment**. The stages show as they run. Small fires take about 1 minute,
   the largest (500+ km²) about 10.
3. The fire appears on the map. Drag the **storm** slider, switch what the map **shows**,
   and click a stream for its numbers.
4. Fires added in the app are **not kept** until you click **Keep**. **Delete** moves a fire
   to **Deleted fires**, where you can restore it or delete it permanently.

At the bottom of the side panel are three sections:
- **Configuration**: **Settings: HPC cluster** and **Run a list of fires**.
- **Logs**:
  - **Batches**: pick a batch from the list to see its fires, steps, errors and log;
  - **Input data for this fire**: each input layer and the service it came from;
  - **Deleted fires**.
- **Exports**: **Download this fire** as GeoJSON, CSV, shapefiles or a file geodatabase.

The app starts empty; every fire you run is stored under `data/` (not in git). Fires from
2016 onward work (Sentinel-2 imagery starts in 2016).

**Several fires at once:** tick the checkbox on each search result (selections stay across
searches), or open **Run a list of fires** and paste one fire per line. Choose where to run
and click **Run**. Progress shows under **Batches**, and each fire appears on the map as it
finishes.

**On an HPC cluster from the app:** you need your own account on a Slurm cluster. Copy this
repository there and run `uv sync` once. Then open **Settings: HPC cluster** and enter:
- the login host and your user name (and a jump host, if your cluster uses one);
- the folder you copied the repository to;
- the Slurm partition.

Click **Test connection**. If your SSH key is already on the cluster, nothing else is needed.
If the cluster needs a password, save it in the settings. It is stored in your system's
password store (Windows Credential Manager, macOS Keychain, or the keyring on a Linux
desktop), never in a file. Batches then offer the cluster as a place to run. The batch runs
there on its own, and the app copies each finished fire back to this computer.

### One fire from the command line

```bash
uv run python pipeline/run_fire.py --id 2025-IDPAF-005844     # by Unique Fire ID
uv run python pipeline/run_fire.py "Rush" 2025 ID             # by name, year, state
uv run python pipeline/run_fire.py --id <ID> --cold           # download every input again
uv run python pipeline/run_fire.py --id <ID> --kf-fill none   # leave soil gaps empty, as USGS does
uv run python pipeline/run_fire.py --id <ID> --no-baer-sbs    # skip the Forest Service BAER map
```

Results go to `data/<fire>_<inputs>/exports/` (shapefiles). `uv run python map/build_map.py`
writes every finished fire into one offline map, `map/dist/debris_flow_map.html`.

### Many fires at once (batch)

Put one fire per line in a text file: a Unique Fire ID (`2025-IDPAF-005844`) or
`name,year,state` (`Rush,2025,ID`; the state only matters when the name is shared). Then:

```bash
uv run python pipeline/batch.py all my_fires.txt --runner local --workers 3
```

Each fire runs exactly as a single fire would; 3-4 at a time suits a 6-core computer. Lines
that match no fire or several fires are left out with the reason, a failed fire does not stop
the others, and fires already assessed are skipped (`--force` re-runs them). Results:
`data/batches/<batch>/summary.csv` (one row per fire: status, times, threshold, agreement with
USGS) and a log per fire in `data/batches/<batch>/logs/`. They also appear in the app.

### On an HPC cluster (Slurm)

The same pipeline runs as Slurm jobs; see `slurm/`. Copy the repository to the cluster,
run `uv sync` there, then from the repository root:

```bash
mkdir -p logs
sbatch --export=ALL,FIRE_ID=2025-IDPAF-005844 slurm/run_fire.slurm          # one fire, 10 m
sbatch --export=ALL,CONFIG=fires/<fire>.json,RES=1 slurm/split_prepare.slurm   # one fire at 1 m LiDAR
python pipeline/batch.py all my_fires.txt --runner slurm --max-running 20   # many fires, 10 m
```

Lemhi's compute nodes have no internet, so a cluster batch runs in three stages from the
login node: it downloads and prepares each fire's inputs there (2 fires at a time, at low
priority), submits one Slurm job array that runs only the model on the compute nodes (one fire
per task, 4 cores and 16 GB each, at most `--max-running` at once), and runs the USGS
comparison back on the login node. With `--wait` (as the app does) each fire is compared as
soon as its model finishes, so it is ready without waiting for the slowest fire, and
`summary.csv` is written at the end. Without it, run `python pipeline/batch.py finish
<batch>` once the array has finished. On a
cluster whose compute nodes can download, `--compute-internet` runs each whole fire in the
array instead.

The 1 m job splits the fire into drainage pieces and runs them as a job array, one piece
per node; see `design.md`, "Watershed split". Submit from the repository's folder: the
scripts run in the folder they were submitted from (default `~/scratch/Debris-Flow-Hunters`).
The app can also submit runs to Slurm when it runs on the cluster:
`uv run python app/server.py --runner slurm`.

## Where things are

| Folder | What is in it |
| --- | --- |
| `app/` | The local web app (`server.py`): find, run, map, keep/delete and export fires; batches here or on a cluster (`batches.py`, `hpc.py`) |
| `map/` | The map page (`template.html`) and the offline map builder (`build_map.py`) |
| `pipeline/` | Every step from a fire name to a finished assessment; `run_fire.py` runs them all |
| `slurm/` | Batch scripts for an HPC cluster |
| `fires/` | One small settings file per fire (`<fire>.json`), written automatically |
| `data/` | Downloads and results; created as you run fires, never committed |

## More

- `design.md`: how it works, where every input comes from, the results so far (with their
  sources), and the open items.
- `CLAUDE.md`: orientation for AI coding agents working in this repository.
- The models and tools: the USGS Landslide Hazards Program (https://www.usgs.gov/programs/landslide-hazards)
  runs the post-fire assessments; `wildcat` and `pfdf` are its open-source code
  (https://code.usgs.gov/ghsc/lhp/wildcat, https://code.usgs.gov/ghsc/lhp/pfdf).
