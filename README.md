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
   click a stream for its numbers, and download it under **Download this fire**.
4. Fires added in the app are **not kept** until you click **Keep**. **Delete** moves a fire
   to **Deleted fires**, where you can restore it or delete it permanently.

The app starts empty; every fire you run is stored under `data/` (not in git). Fires from
2016 onward work (Sentinel-2 imagery starts in 2016).

### One fire from the command line

```bash
uv run python pipeline/run_fire.py --id 2025-IDPAF-005844     # by Unique Fire ID
uv run python pipeline/run_fire.py "Rush" 2025 ID             # by name, year, state
```

Results go to `data/<fire>_<inputs>/exports/` (shapefiles). `uv run python map/build_map.py`
writes every finished fire into one offline map, `map/dist/debris_flow_map.html`.

### On an HPC cluster (Slurm)

The same pipeline runs as Slurm jobs; see `slurm/`. Copy the repository to the cluster,
run `uv sync` there, then from the repository root:

```bash
mkdir -p logs
sbatch --export=ALL,FIRE_ID=2025-IDPAF-005844 slurm/run_fire.slurm          # one fire, 10 m
sbatch --export=ALL,CONFIG=fires/<fire>.json,RES=1 slurm/split_prepare.slurm   # one fire at 1 m LiDAR
```

The 1 m job splits the fire into drainage pieces and runs them as a job array, one piece
per node; see `design.md`, "Watershed split". The scripts assume the repository is at
`~/scratch/Debris-Flow-Hunters`. The app can also submit runs to Slurm when it runs on the
cluster: `uv run python app/server.py --runner slurm`.

## Where things are

| Folder | What is in it |
| --- | --- |
| `app/` | The local web app (`server.py`): find, run, map, keep/delete and export fires |
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
