"""End to end, from a fire to exported M1 thresholds, likelihood, volume and hazard.

Stages (each a subprocess, so a stage's memory and GDAL state stay contained):
  recover      WFIGS perimeter + RECOVER bundle members          pipeline/recover.py
  usgs_inputs  BAER dNBR + severity USGS published, if any       pipeline/usgs_inputs.py
  sentinel2    otherwise dNBR from Sentinel-2, dates by rule     pipeline/sentinel2_dnbr.py
  prepare      wildcat project (DEM, KF, EVT, dNBR, severity)    pipeline/prepare_fire.py
  wildcat      preprocess, assess, export                        pipeline/measure.py
  usgs         USGS's own assessment, if any: fetch, compare     pipeline/usgs_check.py

Burn inputs: USGS's published BAER dNBR and soil burn severity when USGS has assessed the
fire and published its inputs (project data/<key>_usgs_usgs), else Sentinel-2 dNBR with
wildcat-estimated severity (data/<key>_s2_auto_estimate). `--no-usgs-inputs` forces the
Sentinel-2 path. `--cold` deletes the per-fire caches first (RECOVER members, DEM, KF,
Sentinel-2 output) so the timing includes every download.

    python pipeline/run_fire.py fires/<key>.json [--cold]
    python pipeline/run_fire.py "Rush" 2025 [ID] [--cold]   # writes the fire's config first;
                                                            # state only if the name is shared
    python pipeline/run_fire.py --id 2025-IDPAF-005844       # by Unique Fire ID
    python pipeline/run_fire.py --id ... --split [--workers 4]   # wildcat by watershed pieces

map/build_map.py and app/server.py pick up whichever project the run produced.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import fire_key  # noqa: E402
from new_fire import ensure_config  # noqa: E402


def main():
    argv = sys.argv[1:]
    if "--id" in argv:
        config = ensure_config(uid=argv[argv.index("--id") + 1])
    else:
        args = [a for a in argv if not a.startswith("--")]
        config = Path(args[0]) if args[0].endswith(".json") else \
            ensure_config(args[0], int(args[1]), args[2] if len(args) > 2 else None)
    fire = json.loads(config.read_text())
    key = fire_key(fire)
    if "--cold" in sys.argv:
        for path in (ROOT / "data" / "recover" / key, ROOT / "data" / "s2_dnbr" / key / "auto"):
            if path.exists():
                shutil.rmtree(path)

    timings = {}
    total = time.perf_counter()

    def run(name, command):
        start = time.perf_counter()
        print(f"\n===== {name} =====", flush=True)
        result = subprocess.run(command, cwd=ROOT)
        timings[name] = round(time.perf_counter() - start, 1)
        if result.returncode:
            raise SystemExit(f"{name} failed (exit {result.returncode})")

    run("recover", [PY, "pipeline/recover.py", str(config)])
    use_usgs = False
    if "--no-usgs-inputs" not in sys.argv:
        run("usgs_inputs", [PY, "pipeline/usgs_inputs.py", str(config)])    # never fails
        use_usgs = bool(json.loads(config.read_text()).get("usgs_inputs"))
    if use_usgs:
        project = ROOT / "data" / f"{key}_usgs_usgs"
        burn = ["--dnbr", "usgs", "--severity", "usgs"]
    else:
        project = ROOT / "data" / f"{key}_s2_auto_estimate"
        burn = ["--dnbr", "s2:auto", "--severity", "estimate"]
        run("sentinel2", [PY, "pipeline/sentinel2_dnbr.py", str(config)])
    run("prepare", [PY, "pipeline/prepare_fire.py", str(config), *burn])
    peaks = {}
    if "--split" in sys.argv:
        # Watershed split (pipeline/split_fire.py): preprocess the whole fire, then wildcat
        # assess + export per drainage piece, --workers at a time, merged into <project>_split
        workers = argv[argv.index("--workers") + 1] if "--workers" in argv else "4"
        run("wildcat", [PY, "pipeline/split_fire.py", "pipeline", str(project), "--workers", workers])
        project = project.parent / f"{project.name}_split"
        steps = json.loads((project / "split_meta" / "steps.json").read_text())
        pieces = json.loads((project / "split_meta" / "run.json").read_text())
        peaks = {"preprocess": steps["preprocess"]["peak_mb"],
                 "assess_max_piece": max(p["assess"]["peak_mb"] for p in pieces)}
    else:
        # one fresh process per stage (pipeline/measure.py), so each stage's time and
        # peak memory are recorded
        from measure import run_stage
        start = time.perf_counter()
        print("\n===== wildcat =====", flush=True)
        for stage in ("preprocess", "assess", "export"):
            print(f"\n===== {stage} =====", flush=True)
            r = run_stage(stage, project)
            if "error" in r or r.get("timeout"):
                raise SystemExit(f"wildcat {stage} failed: {r.get('error', 'timeout')}")
            print(f"-- {stage}: {r['seconds']:.1f}s, peak {r['peak_mb']:.0f} MB", flush=True)
            peaks[stage] = r["peak_mb"]
        timings["wildcat"] = round(time.perf_counter() - start, 1)
    # informative only: always exits 0, "no assessment" is a result too
    run("usgs", [PY, "pipeline/usgs_check.py", str(config), str(project)])
    timings["total"] = round(time.perf_counter() - total, 1)

    print("\nstage timings (s):", json.dumps(timings), "| wildcat peak MB:", json.dumps(peaks))
    out = ROOT / "data" / f"{key}_run_timings.json"
    runs = json.loads(out.read_text()) if out.exists() else []
    runs.append({"cold": "--cold" in sys.argv, "split": "--split" in sys.argv, **timings,
                 "peak_mb": peaks})
    out.write_text(json.dumps(runs, indent=1))


if __name__ == "__main__":
    main()
