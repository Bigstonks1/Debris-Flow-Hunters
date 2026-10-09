"""Run many fires as one batch: on this computer N at a time, or on Slurm as a job array.

    python pipeline/batch.py resolve fires.txt [--name NAME]      # list -> batch folder
    python pipeline/batch.py run NAME --runner local [--workers 4] [--force]
    python pipeline/batch.py run NAME --runner slurm [--max-running 20] [--fetch-workers 2] [--wait]
                                     [--partition P] [--compute-internet]
    python pipeline/batch.py finish NAME                           # Slurm: USGS checks + summary
    python pipeline/batch.py summary NAME                          # -> summary.csv
    python pipeline/batch.py all fires.txt --runner local --workers 4   # resolve, run, summary

The list has one fire per line: a Unique Fire ID (2025-IDPAF-005844), or name,year[,state]
("Rush,2025,ID"). Blank lines and lines starting with # are ignored.

Steps
  resolve  turns every line into its fires/<key>.json (pipeline/new_fire.py), one at a time:
           parallel runs never write configs, so two same-named fires cannot collide. A line
           that matches no fire, or several, is recorded with the reason and left out.
           Writes data/batches/<name>/fires.tsv.
  run      each fire is pipeline/run_fire.py <config>, the same end-to-end run as the app's,
           with its own logs and status (status/<key>.json). One failure does not stop the
           others. Fires with a finished project are skipped unless --force.
           Local: the whole run per fire, --workers fires at once (each run is mostly
           single-threaded, so 3-4 on a 6-core computer), after one fire alone has built
           numba's compiled-code cache (see run_local).
           Slurm (staged, because Lemhi's compute nodes have no internet):
             inputs   on this (login) node, --fetch-workers fires at a time, at low priority:
                      downloads and input preparation (run_fire.py --phase inputs)
             wildcat  on compute nodes: slurm/batch_fires.slurm as one array over the fires
                      whose inputs succeeded, --max-running at once (--phase wildcat; no
                      network; each task has its own numba cache)
             finish   back on this node once the array is done (`batch.py finish NAME`, or
                      --wait): the USGS comparison (--phase usgs, downloads) and the summary.
           --compute-internet runs each whole fire inside the array instead (clusters whose
           compute nodes can download), with a summary job after it.
  summary  one row per fire: status, stage times, peak memory, the burn inputs used,
           fire-wide threshold and the USGS comparison when USGS assessed the fire.
"""

import csv
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import UID_RE, finished_project, fire_key  # noqa: E402

BATCHES = ROOT / "data" / "batches"
FIELDS = ["index", "line", "unique_id", "key", "config", "error"]
# Slurm commands; DFH_SBATCH / DFH_SQUEUE replace them (e.g. a stand-in for testing)
SBATCH = shlex.split(os.environ.get("DFH_SBATCH", "sbatch"), posix=os.name != "nt")
SQUEUE = shlex.split(os.environ.get("DFH_SQUEUE", "squeue"), posix=os.name != "nt")


def batch_dir(name):
    return BATCHES / name


def parse_line(line):
    """('uid', ID) or ('name', name, year, state) from one list line, or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if UID_RE.match(line.upper()):
        return ("uid", line.upper())
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError("expected a Unique Fire ID or name,year[,state]")
    return ("name", parts[0], int(parts[1]), parts[2].upper() if len(parts) > 2 and parts[2] else None)


def resolve(list_path, name=None):
    from new_fire import ensure_config
    name = name or f"{Path(list_path).stem}_{datetime.now():%Y%m%d-%H%M%S}"
    out = batch_dir(name)
    out.mkdir(parents=True, exist_ok=True)
    rows, seen = [], set()
    for line in Path(list_path).read_text(encoding="utf-8").splitlines():
        row = {"index": "", "line": line.strip(), "unique_id": "", "key": "", "config": "", "error": ""}
        try:
            parsed = parse_line(line)
            if parsed is None:
                continue
            config = (ensure_config(uid=parsed[1]) if parsed[0] == "uid"
                      else ensure_config(parsed[1], parsed[2], parsed[3]))
            fire = json.loads(config.read_text())
            row.update(unique_id=fire.get("unique_id", ""), key=fire_key(fire),
                       config=str(config.relative_to(ROOT)).replace("\\", "/"))
            if row["unique_id"] in seen:
                row["error"] = "duplicate of an earlier line"
            seen.add(row["unique_id"])
        except SystemExit as e:          # new_fire: no match, or several (lists them)
            row["error"] = str(e).splitlines()[0]
        except Exception as e:           # WFIGS unreachable, bad line, ...
            row["error"] = f"{type(e).__name__}: {e}"
        rows.append(row)
    n = 0
    for row in rows:                     # array task numbers: 1..N over the runnable fires
        if not row["error"]:
            n += 1
            row["index"] = n
    with open(out / "fires.tsv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS, delimiter="\t")
        w.writeheader()
        w.writerows(rows)
    bad = [r for r in rows if r["error"]]
    print(f"batch {name}: {n} fires to run, {len(bad)} left out -> {out / 'fires.tsv'}")
    for r in bad:
        print(f"  left out: {r['line']!r}: {r['error']}")
    return name


def fires_of(name):
    with open(batch_dir(name) / "fires.tsv", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f, delimiter="\t") if r["index"]]


def project_of(key):
    """The finished project run_fire.py made for a fire, or None (as map/build_map.py finds it)."""
    return finished_project(key)


def load_status(name, key):
    p = batch_dir(name) / "status" / f"{key}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def task(name, index, force=False, phase="all"):
    """One fire, one phase (or all): run_fire.py with its own log; the result goes into the
    fire's status file. Used by both runners and by the Slurm array tasks."""
    fire = next(r for r in fires_of(name) if int(r["index"]) == int(index))
    out = batch_dir(name)
    (out / "logs").mkdir(exist_ok=True)
    (out / "status").mkdir(exist_ok=True)
    status = load_status(name, fire["key"]) or {"index": int(index), "unique_id": fire["unique_id"],
                                                "key": fire["key"]}
    status.setdefault("phases", {})      # status files from before phases had none
    host = os.environ.get("SLURMD_NODENAME") or platform.node()
    if phase in ("all", "inputs") and project_of(fire["key"]) and not force:
        status.update(status="skipped (already assessed)")
    else:
        log = out / "logs" / (f"{fire['key']}.log" if phase == "all" else f"{fire['key']}.{phase}.log")
        command = [sys.executable, str(ROOT / "pipeline" / "run_fire.py"), fire["config"]]
        if phase != "all":
            command += ["--phase", phase]
        start = time.perf_counter()
        with open(log, "w", encoding="utf-8") as f:
            code = subprocess.run(command, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"}).returncode
        result = {"status": "done" if code == 0 else "failed", "returncode": code,
                  "seconds": round(time.perf_counter() - start, 1), "host": host,
                  "job": os.environ.get("SLURM_JOB_ID", "")}
        if code:
            lines = [l for l in log.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            result["error"] = (lines[-1] if lines else "no output")[:300]
        status["phases"][phase] = result
        status.update(overall(status["phases"]))
    (out / "status" / f"{fire['key']}.json").write_text(json.dumps(status, indent=1))
    last = status["phases"].get(phase, {})
    print(f"[{index}] {fire['key']} {phase}: {last.get('status', status['status'])} "
          f"({last.get('seconds', 0)} s)" + (f" - {last['error']}" if last.get("error") else ""), flush=True)
    return status


def overall(phases):
    """A fire's status across its phases, and the summed time."""
    failed = [p for p, r in phases.items() if r["status"] == "failed"]
    seconds = round(sum(r.get("seconds", 0) for r in phases.values()), 1)
    if failed:
        return {"status": f"failed ({failed[0]})", "error": phases[failed[0]].get("error", ""), "seconds": seconds}
    done = [p for p in ("all", "usgs", "wildcat", "inputs") if phases.get(p, {}).get("status") == "done"]
    state = "done" if done and done[0] in ("all", "usgs") else f"{done[0]} done" if done else "not run yet"
    return {"status": state, "error": "", "seconds": seconds}


def run_local(name, workers=4, force=False):
    """Fires one at a time until one completes, then the rest `workers` at a time.

    pfdf/pysheds compile with numba and cache the compiled code next to the library. Several
    processes building that cache at once corrupt it ("KeyError: int64 generator(...)" in
    assess; reproduced: 2 of 3 parallel fires failed from an empty cache, all succeeded once
    it existed). The first completed fire builds the cache; after that, parallel runs only
    read it."""
    pending = fires_of(name)
    print(f"batch {name}: {len(pending)} fires, {workers} at a time on this computer", flush=True)
    start = time.perf_counter()
    while pending and workers > 1:
        if task(name, pending.pop(0)["index"], force)["status"] == "done":
            break
    with ThreadPoolExecutor(max_workers=workers) as pool:    # each fire is its own process
        list(pool.map(lambda r: task(name, r["index"], force), pending))
    print(f"batch {name}: finished in {time.perf_counter() - start:.0f} s")
    summary(name)


def sbatch(*args):
    r = subprocess.run([*SBATCH, "--parsable", *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"sbatch failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip().split(";")[0]


def run_slurm(name, max_running=20, force=False, fetch_workers=2, wait=False, compute_internet=False,
              partition=None):
    global SBATCH
    if partition:                 # overrides the script's #SBATCH --partition
        SBATCH = [*SBATCH, "-p", partition]
    (ROOT / "logs").mkdir(exist_ok=True)
    fires = fires_of(name)
    if not fires:
        raise SystemExit(f"batch {name} has no fires to run")
    if compute_internet:      # whole fires inside the array; a summary job after it
        array = sbatch(f"--array=1-{len(fires)}%{max_running}",
                       f"--export=ALL,BATCH={name},PHASE=all" + (",FORCE=1" if force else ""),
                       "slurm/batch_fires.slurm")
        summ = sbatch(f"--dependency=afterany:{array}", f"--export=ALL,BATCH={name}",
                      "--job-name=dfh-batch-sum", "-p", "tiny", "-t", "00:15:00", "--mem=4G",
                      "-o", "logs/batch-summary_%j.log",
                      "--wrap", f"source .venv/bin/activate && python pipeline/batch.py summary {name}")
        (batch_dir(name) / "slurm.json").write_text(json.dumps({"array": array, "summary": summ}))
        print(f"batch {name}: {len(fires)} fires as array job {array}; summary job {summ}")
        return
    # Staged. 1: inputs here, gently: low priority, few threads, few fires at a time
    if hasattr(os, "nice"):
        os.nice(10)
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "GDAL_NUM_THREADS"):
        os.environ.setdefault(var, "2")
    print(f"batch {name}: inputs for {len(fires)} fires on {platform.node()}, "
          f"{fetch_workers} at a time", flush=True)
    with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
        statuses = list(pool.map(lambda r: task(name, r["index"], force, "inputs"), fires))
    ready = [int(s["index"]) for s in statuses if s["phases"].get("inputs", {}).get("status") == "done"]
    if not ready:
        print(f"batch {name}: no fire has inputs ready; nothing to submit")
        summary(name)
        return
    # 2: wildcat on compute nodes, one array task per ready fire
    array = sbatch(f"--array={','.join(map(str, ready))}%{max_running}",
                   f"--export=ALL,BATCH={name},PHASE=wildcat", "slurm/batch_fires.slurm")
    (batch_dir(name) / "slurm.json").write_text(json.dumps({"array": array, "fires": ready,
                                                            "max_running": max_running}))
    print(f"batch {name}: {len(ready)} fires submitted as array job {array} ({max_running} at once)."
          f"\n  When it finishes: python pipeline/batch.py finish {name}")
    if wait:     # 3: poll the queue, then finish here
        while queued(array):
            time.sleep(60)
        finish(name)


def queued(job):
    r = subprocess.run([*SQUEUE, "-h", "-j", job, "-o", "%i"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def finish(name):
    """After the array: the USGS comparison for every fire whose wildcat phase is done (it
    downloads USGS's assessment, so it runs on this node), then the summary."""
    fires = fires_of(name)
    todo = [r for r in fires
            if load_status(name, r["key"]).get("phases", {}).get("wildcat", {}).get("status") == "done"
            and load_status(name, r["key"]).get("phases", {}).get("usgs", {}).get("status") != "done"]
    print(f"batch {name}: USGS comparison for {len(todo)} fires", flush=True)
    for r in todo:
        task(name, r["index"], phase="usgs")
    pending = [r["key"] for r in fires if load_status(name, r["key"]).get("status") in ("inputs done", None)]
    if pending:
        print(f"batch {name}: not finished on the cluster yet (or not submitted): {', '.join(pending)}")
    summary(name)


def summary(name):
    out = batch_dir(name)
    with open(out / "fires.tsv", encoding="utf-8") as f:
        listed = list(csv.DictReader(f, delimiter="\t"))
    cols = ["line", "unique_id", "key", "status", "error", "seconds", "recover_s", "usgs_inputs_s",
            "baer_sbs_s", "sentinel2_s", "prepare_s", "wildcat_s", "usgs_s", "assess_peak_mb", "where", "burn_inputs",
            "segments", "fire_wide_I15_50", "usgs_code", "usgs_pairs", "usgs_threshold_ratio",
            "usgs_guidance", "project"]
    rows = []
    for r in listed:
        row = {c: "" for c in cols}
        row.update(line=r["line"], unique_id=r["unique_id"], key=r["key"])
        if r["error"]:
            row.update(status="not run", error=r["error"])
            rows.append(row)
            continue
        s = load_status(name, r["key"])
        row.update(status=s.get("status", "not run yet"), error=s.get("error", ""), seconds=s.get("seconds", ""),
                   where="; ".join(f"{p}: {v.get('host', '')}" for p, v in s.get("phases", {}).items()))
        timings = ROOT / "data" / f"{r['key']}_run_timings.json"
        if timings.exists():
            merged = {}      # the latest value of each stage across this fire's phase runs
            for t in json.loads(timings.read_text()):
                merged.update({k: v for k, v in t.items() if k not in ("peak_mb", "total")})
                if t.get("peak_mb"):
                    merged["assess_peak_mb"] = t["peak_mb"].get("assess", "")
            row.update({f"{k}_s": merged.get(k, "") for k in
                        ("recover", "usgs_inputs", "baer_sbs", "sentinel2", "prepare", "wildcat", "usgs")},
                       assess_peak_mb=merged.get("assess_peak_mb", ""))
        project = project_of(r["key"])
        if project:
            row.update(project=str(project.relative_to(ROOT)).replace("\\", "/"),
                       burn_inputs="USGS-published" if project.name.endswith("_usgs_usgs")
                       else "Sentinel-2 + BAER severity map" if project.name.endswith("_baersbs")
                       else "Sentinel-2 + estimated severity")
            check = project / "usgs_check.json"
            if check.exists():
                c = json.loads(check.read_text())
                row.update(usgs_code=c.get("code", "") if c.get("found") else "none found",
                           usgs_pairs=c.get("pairs", ""), usgs_threshold_ratio=_round(c.get("threshold_ratio")),
                           usgs_guidance=c.get("usgs_guidance", ""), fire_wide_I15_50=_round(c.get("our_fire_wide")))
            row["segments"] = _count(project / "exports" / "segments.shp")
            if not row["fire_wide_I15_50"]:
                row["fire_wide_I15_50"] = _median_threshold(project / "exports" / "segments.shp")
        rows.append(row)
    with open(out / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(rows)
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(f"batch {name}: {counts} -> {out / 'summary.csv'}")
    return rows


def _round(v, n=3):
    return round(v, n) if isinstance(v, (int, float)) else ""


def _count(shp):
    import fiona
    with fiona.open(shp) as c:
        return len(c)


def _median_threshold(shp):
    import fiona
    import numpy as np
    with fiona.open(shp) as c:
        v = [f["properties"]["I15_50"] for f in c if f["properties"]["I15_50"] is not None]
    return round(float(np.median(v)), 3) if v else ""


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("resolve", "run", "task", "finish", "summary", "all"):
        raise SystemExit(__doc__)
    opt = lambda k, d=None: args[args.index(k) + 1] if k in args else d
    step, target = args[0], args[1]
    force = "--force" in args or os.environ.get("FORCE") == "1"
    if step == "resolve":
        resolve(target, opt("--name"))
    elif step == "task":
        task(target, int(args[2]), force, opt("--phase", "all"))
    elif step == "finish":
        finish(target)
    elif step == "summary":
        summary(target)
    else:
        name = resolve(target, opt("--name")) if step == "all" else target
        runner = opt("--runner", "local")
        if runner == "local":
            run_local(name, int(opt("--workers", 4)), force)
        elif runner == "slurm":
            run_slurm(name, int(opt("--max-running", 20)), force, int(opt("--fetch-workers", 2)),
                      "--wait" in args, "--compute-internet" in args, opt("--partition"))
        else:
            raise SystemExit("--runner local or slurm")


if __name__ == "__main__":
    main()
