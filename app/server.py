"""Local debris-flow hazard app: request a fire by name, watch it run, map it, export it.

    uv run python app/server.py            # then open http://127.0.0.1:8000

    uv run python app/server.py --runner slurm   # submit runs to Slurm (sbatch on this host)

The same map as map/build_map.py, but each fire's layers are fetched when chosen instead
of baked into one file, and a panel runs a new fire (name, year, optional state) through
pipeline/run_fire.py: here, one at a time (a 10 m assessment needs several GB), or as
slurm/run_fire.slurm jobs, several at once. Exports come straight from the wildcat
project: GeoJSON, CSV, zipped shapefiles, or a zipped Esri file geodatabase (GDAL's
OpenFileGDB writer; no ArcGIS needed).

Standard library only, bound to 127.0.0.1.
"""

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fiona
from fiona.transform import transform_geom
from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon, mapping, shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "map"))
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "app"))
import batches  # noqa: E402
import build_map  # noqa: E402
import hpc  # noqa: E402
from fires import UID_RE, describe, find_config, fire_key, lookup  # noqa: E402

BATCH_RE = re.compile(r"^app_\d{8}-\d{6}$")

RUNS = ROOT / "data" / "runs"
EXPORTS = ROOT / "data" / "exports"
PORT = 8000
STAGES = {   # run_fire.py stage banners -> what the user sees
    "recover": "Fire perimeter (WFIGS) and RECOVER data",
    "sentinel2": "Burn severity from Sentinel-2 (dNBR)",
    "prepare": "Elevation (USGS 10 m) and soils (STATSGO, SSURGO)",
    "wildcat": "wildcat: stream network", "preprocess": "wildcat: preparing inputs",
    "assess": "wildcat: likelihood, volume and hazard", "export": "wildcat: writing results",
    "usgs_inputs": "Looking for USGS's published BAER inputs",
    "baer_sbs": "Looking for a Forest Service BAER burn severity map",
    "usgs": "Checking against USGS's own assessment",
}
NAME_RE = re.compile(r"^[A-Za-z0-9 .'\-]{2,60}$")

runs, run_lock = {}, threading.Lock()
export_lock = threading.Lock()     # one export at a time; GDB writes are heavy
payloads = {}     # key -> (segments mtime, payload)
payload_locks = {}      # key -> Lock: one build per fire, however many requests wait on it


# ---------------------------------------------------------------- fires and map data

def fires():
    return {f["key"]: f for f in build_map.discover()}


META = ("key", "name", "year", "state", "unique_id", "usgs_median", "usgs_check", "inputs", "services",
        "severity_source", "kept")


def payload(f):
    """Map data for a fire. The layers are cached in memory and on disk until the fire's
    exports change; the descriptive fields come from its config on every call, so editing
    fires/*.json (e.g. adding a USGS reference) shows up without rebuilding anything."""
    source = ROOT / f["project"] / "exports" / "segments.shp"
    with payload_locks.setdefault(f["key"], threading.Lock()):
        stamp = source.stat().st_mtime
        if f["key"] in payloads and payloads[f["key"]][0] == stamp:
            data = payloads[f["key"]][1]
        else:
            cache = ROOT / f["project"] / "map_payload.json"
            if cache.exists() and cache.stat().st_mtime >= stamp:
                data = json.loads(cache.read_text())
            else:
                data = build_map.fire_payload(f)
                cache.write_text(json.dumps(data, separators=(",", ":")))
            payloads[f["key"]] = (stamp, data)
    return {**data, **{k: f[k] for k in META}}


def build_payload(key):
    """Build a fire's map layers as soon as its results arrive (HPC copy-back), so the
    next page load does not wait minutes for a large fire."""
    f = fires().get(key)
    if f:
        payload(f)


def summaries():
    return [{k: payload(f)[k] for k in build_map.SUMMARY} for f in fires().values()]


# ---------------------------------------------------------------- keep and delete
#
# Keep protects a fire from deletion. Fires already configured are kept; fires added in
# the app arrive not kept. Delete moves the fire's config and data into data/trash/<id>/
# (same relative paths, plus manifest.json), so it can be restored; only "delete
# permanently" removes files.

TRASH = ROOT / "data" / "trash"
FIRES_DIR = ROOT / "fires"
manage_lock = threading.Lock()


def set_kept(f, kept):
    cfg = FIRES_DIR / f["config"]
    fire = json.loads(cfg.read_text())
    fire["kept"] = bool(kept)
    cfg.write_text(json.dumps(fire, indent=1) + "\n")


def owned_paths(f):
    """Everything on disk that belongs to one fire: its config; the data/<key>_* projects and
    files (pipeline/prepare_fire.py, run_fire.py), each given to the LONGEST fire key it
    starts with, so deleting "rush" never takes "rush_2025"'s projects; its RECOVER and
    Sentinel-2 downloads; its cached exports. Shared downloads (data/usgs/<code>) stay."""
    key = f["data_key"]
    keys = {fire_key(json.loads(p.read_text())) for p in FIRES_DIR.glob("*.json")}
    data = ROOT / "data"

    def owner(name):
        matches = [k for k in keys if name == k or name.startswith(k + "_")]
        return max(matches, key=len) if matches else None

    paths = [FIRES_DIR / f["config"]]
    paths += [p for p in data.iterdir() if p.name not in ("trash", "runs", "exports", "recover", "s2_dnbr", "usgs")
              and owner(p.name) == key]
    paths += [p for p in (data / "recover" / key, data / "s2_dnbr" / key, EXPORTS / f["key"]) if p.exists()]
    return paths


def size_of(paths):
    total = 0
    for p in paths:
        files = [p] if p.is_file() else (q for q in p.rglob("*") if q.is_file())
        total += sum(q.stat().st_size for q in files)
    return total


def active_uids():
    with run_lock:
        return {r["unique_id"] for r in runs.values() if r["status"] in ("queued", "running")}


def delete_fire(f):
    """Move a fire into the trash; (status, body)."""
    if f["kept"]:
        return 409, {"error": f"{f['name']} {f['year']} is kept; turn Keep off to delete it"}
    if f["unique_id"] in active_uids():
        return 409, {"error": f"{f['name']} {f['year']} is running; wait for it to finish"}
    with manage_lock:
        paths = owned_paths(f)
        tid = f"{time.strftime('%Y%m%d-%H%M%S')}_{f['key']}"
        dest = TRASH / tid
        moved = []
        try:
            for p in paths:
                target = dest / p.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(target))
                moved.append((p, target))
        except OSError as e:      # e.g. a file held open on Windows: put everything back
            for p, target in reversed(moved):
                shutil.move(str(target), str(p))
            shutil.rmtree(dest, ignore_errors=True)
            return 409, {"error": f"could not move {Path(e.filename or '').name or 'a file'} "
                                  f"(open in another program?); nothing was deleted"}
        manifest = {"id": tid, "key": f["key"], "name": f["name"], "year": f["year"],
                    "state": f["state"], "unique_id": f["unique_id"], "deleted": time.time(),
                    "bytes": size_of([t for _, t in moved]),
                    "paths": [str(p.relative_to(ROOT)).replace("\\", "/") for p, _ in moved]}
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
        payloads.pop(f["key"], None)
    return 200, manifest


def trash_list():
    out = []
    for m in sorted(TRASH.glob("*/manifest.json"), reverse=True):
        out.append(json.loads(m.read_text()))
    return out


def restore_fire(tid):
    dest = TRASH / tid
    manifest = json.loads((dest / "manifest.json").read_text())
    clash = [p for p in manifest["paths"] if (ROOT / p).exists()]
    if clash:
        return 409, {"error": f"cannot restore: {clash[0]} exists again (fire re-run since?)"}
    with manage_lock:
        for p in manifest["paths"]:
            (ROOT / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest / p), str(ROOT / p))
        shutil.rmtree(dest)
    return 200, {"restored": manifest["key"]}


def purge_fire(tid):
    with manage_lock:
        shutil.rmtree(TRASH / tid)
    return 200, {"purged": tid}


# ---------------------------------------------------------------- runs

def stage_of(log):
    stage = None
    for line in log.splitlines():
        m = re.match(r"^===== (\w+) =====", line)
        if m and m.group(1) in STAGES:
            stage = m.group(1)
    return stage


def error_of(log):
    """The informative part of a failed run's log, not the traceback around it."""
    lines = [l for l in log.splitlines() if l.strip() and "Warning" not in l]
    for i, line in enumerate(lines):
        if re.search(r"no network|query failed|expected one|is already|not found|failed|empty pixels|does not match|no clear", line, re.I) \
                and not line.lstrip().startswith(("File ", "raise ")):
            # keep only new_fire.py's list of same-named fires, not the traceback after it
            extra = [l for l in lines[i + 1:i + 12] if re.match(r"^\s+US-[A-Z]{2}\s", l)]
            return "\n".join([line.strip()] + [l.rstrip() for l in extra])
    return lines[-1].strip() if lines else "failed without output"


class LocalRunner:
    """pipeline/run_fire.py as a child process of this server. One at a time: a 10 m
    assessment of a large fire needs several GB."""
    max_runs, poll_s = 1, 2

    def submit(self, uid, rid):
        RUNS.mkdir(parents=True, exist_ok=True)
        log_path = RUNS / f"{rid}.log"
        log = open(log_path, "w", encoding="utf-8")
        cmd = [sys.executable, "pipeline/run_fire.py", "--id", uid]
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return {"proc": proc, "logfile": log, "log": log_path, "where": "this computer"}

    def poll(self, h):
        """("queued" | "running" | "done" | "failed", detail)"""
        code = h["proc"].poll()
        if code is None:
            return "running", None
        h["logfile"].close()
        return ("done", None) if code == 0 else ("failed", f"exit {code}")


class SlurmRunner:
    """slurm/run_fire.slurm through sbatch; state from sacct (squeue if accounting is off),
    progress from the job's log, which is on the same filesystem as the results.

    `prefix` goes in front of every Slurm command. Empty means sbatch and sacct run on
    this host, i.e. the server runs on the cluster. How the app reaches Lemhi otherwise
    (Open OnDemand) is still open, and would plug in here."""
    max_runs, poll_s = 8, 5
    ACTIVE = {"PENDING": "queued", "REQUEUED": "queued", "CONFIGURING": "queued",
              "RUNNING": "running", "COMPLETING": "running", "SUSPENDED": "running"}

    def __init__(self, prefix=()):
        self.prefix = list(prefix)

    def slurm(self, *args):
        try:
            return subprocess.run([*self.prefix, *args], cwd=ROOT, capture_output=True,
                                  text=True, timeout=120)
        except FileNotFoundError:
            raise RuntimeError(f"cannot run {(self.prefix or [args[0]])[0]}: not found "
                               f"(is this a Slurm host, or is --slurm-prefix right?)") from None

    def submit(self, uid, rid):
        (ROOT / "logs").mkdir(exist_ok=True)     # the script's --output directory
        export = f"ALL,FIRE_ID={uid}"
        r = self.slurm("sbatch", "--parsable", f"--export={export}", "slurm/run_fire.slurm")
        if r.returncode:
            raise RuntimeError(f"sbatch failed: {(r.stderr or r.stdout).strip()[:300]}")
        job = r.stdout.strip().split(";")[0]      # --parsable: "jobid[;cluster]"
        return {"job": job, "log": ROOT / "logs" / f"fire_{job}.log",
                "where": f"Lemhi, Slurm job {job}"}

    def poll(self, h):
        r = self.slurm("sacct", "-j", h["job"], "-X", "--noheader", "--parsable2",
                       "--format=State")
        if r.returncode == 0:
            # sacct can lag a few seconds behind sbatch: no row yet means still pending
            state = (r.stdout.split() or ["PENDING"])[0]
        else:
            q = self.slurm("squeue", "-h", "-j", h["job"], "-o", "%T")
            state = q.stdout.strip() or ("COMPLETED" if self.finished_ok(h) else "FAILED")
        if state in self.ACTIVE:
            return self.ACTIVE[state], state
        return ("done", None) if state == "COMPLETED" else ("failed", state)

    @staticmethod
    def finished_ok(h):
        """Without sacct: the job script's last line is "done <date>" only on success."""
        log = Path(h["log"])
        lines = log.read_text(errors="replace").strip().splitlines() if log.exists() else []
        return bool(lines) and lines[-1].startswith("done ")


RUNNER = LocalRunner()      # replaced from the command line (--runner slurm)


def start_run(card):
    """Run the fire a lookup card describes (see fires.describe), by its Unique Fire ID."""
    uid, name, year, state = card["unique_id"], card["name"], card["year"], card["state"]
    with run_lock:
        active = [r for r in runs.values() if r["status"] in ("queued", "running")]
        if isinstance(RUNNER, LocalRunner) and batches.active("local"):
            raise RuntimeError("a batch is running on this computer; wait for it to finish")
        if len(active) >= RUNNER.max_runs:
            raise RuntimeError("a run is already in progress; one at a time"
                               if RUNNER.max_runs == 1 else
                               f"{len(active)} runs in progress; wait for one to finish")
        rid = uuid.uuid4().hex[:8]
        new = find_config(uid) is None      # added here, so it arrives not kept
        handle = RUNNER.submit(uid, rid)
        run = {"id": rid, "unique_id": uid, "name": name, "year": year, "state": state,
               "status": "queued",
               "started": time.time(), "log": str(handle["log"]), "where": handle["where"],
               "fire_key": None, "error": None}
        runs[rid] = run

    def watch():
        while True:
            status, detail = RUNNER.poll(handle)
            if status in ("queued", "running"):
                run["status"] = status
                time.sleep(RUNNER.poll_s)
                continue
            break
        log = Path(run["log"])
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        run["finished"] = time.time()
        if status == "failed":
            reason = error_of(text) if text.strip() else "no log written"
            # Slurm's own verdict matters when the job was killed rather than failing
            if detail in ("OUT_OF_MEMORY", "TIMEOUT", "CANCELLED", "NODE_FAIL", "PREEMPTED"):
                reason = f"Slurm: {detail}\n{reason}"
            run["status"], run["error"] = "failed", reason
            return
        match = [f for f in fires().values() if f["unique_id"] == uid]
        run["fire_key"] = match[0]["key"] if match else None
        if match and new:
            set_kept(match[0], False)
            match[0]["kept"] = False
        run["status"] = "done"
        if match:
            payload(match[0])            # build the map data now, not on first view

    threading.Thread(target=watch, daemon=True).start()
    return run


def run_status(rid):
    run = runs[rid]
    log = Path(run["log"])
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    stage = stage_of(text)
    label = f"Queued ({run['where']})" if run["status"] == "queued" \
        else STAGES.get(stage, f"Starting ({run['where']})")
    end = run.get("finished", time.time())
    return {**{k: run[k] for k in ("id", "unique_id", "name", "year", "state", "status",
                                   "fire_key", "error", "where")},
            "stage": stage, "stage_label": label, "elapsed_s": round(end - run["started"])}


# ---------------------------------------------------------------- finding a fire

def on_map(uid):
    """Key of the finished fire with this Unique Fire ID, or None."""
    return next((f["key"] for f in fires().values() if f["unique_id"] == uid), None)


def find(q):
    """(status, body) for /api/lookup?name=&year=[&state=] or ?id=: WFIGS candidates, each
    marked with its map key when it is already assessed."""
    get = lambda k: (q.get(k, [""])[0] or "").strip()
    uid, name, state = get("id").upper(), get("name"), get("state").upper()
    if uid:
        if not UID_RE.match(uid):
            return 400, {"error": "Unique Fire ID looks like 2025-IDPAF-005844"}
        found = lookup(uid=uid)
    else:
        try:
            year = int(get("year"))
        except ValueError:
            return 400, {"error": "enter a year"}
        if not NAME_RE.match(name):
            return 400, {"error": "fire name: letters, numbers, spaces, . ' - only"}
        if state and not re.fullmatch(r"[A-Z]{2}", state):
            return 400, {"error": "state: two-letter code, e.g. ID"}
        found = lookup(name, year, state or None)
    cards = [describe(f) for f in found[:25]]
    for c in cards:
        c["on_map"] = on_map(c["unique_id"])
    return 200, {"candidates": cards, "more": max(0, len(found) - 25)}


# ---------------------------------------------------------------- exports

def layers(f):
    p = ROOT / f["project"]
    return {"segments": p / "exports" / "segments.shp", "basins": p / "exports" / "basins.shp",
            "outlets": p / "exports" / "outlets.shp", "perimeter": p / "inputs" / "perimeter.shp"}


def gdb_records(c):
    """A shapefile layer's records and schema for a geodatabase, in WGS 84.

    Shapefiles do not tell Polygon from MultiPolygon, but a feature class holds one type,
    so a layer with any multipart feature is written as Multi* with single parts wrapped.
    Everything goes to WGS 84 (wildcat's exports already are): GDAL's OpenFileGDB writer
    rejected the UTM perimeter with "Point outside of projection domain"."""
    kinds = {r["geometry"]["type"] for r in c if r["geometry"]}
    kind = c.schema["geometry"]
    multi = len(kinds) > 1 or (kinds and kinds != {kind})
    wrap = {"Polygon": MultiPolygon, "LineString": MultiLineString, "Point": MultiPoint}
    # The writer has datetime fields but not date ones (Grizzly's MTBS perimeter has dates)
    fields = {k: ("datetime" if v.startswith("date") and v != "datetime" else v)
              for k, v in c.schema["properties"].items()}
    dates = [k for k, v in c.schema["properties"].items() if v.startswith("date") and v != "datetime"]
    records = []
    for r in c:
        g = shape(transform_geom(c.crs, "EPSG:4326", r["geometry"]))
        if multi and not g.geom_type.startswith("Multi"):
            g = wrap[g.geom_type]([g])
        props = dict(r["properties"])
        for k in dates:
            if props[k]:
                props[k] = f"{props[k]}T00:00:00"
        records.append(fiona.Feature(geometry=fiona.Geometry.from_dict(mapping(g)),
                                     properties=props))
    geometry = "Multi" + kind.replace("Multi", "") if multi else kind
    return records, {"geometry": geometry, "properties": fields}


def export(f, fmt):
    """(bytes, filename, content type) for one fire in one format, cached until it changes."""
    src = layers(f)
    stamp = max(s.stat().st_mtime for s in src.values() if s.exists())
    ext = {"geojson": "geojson", "csv": "csv", "shp": "shp.zip", "gdb": "gdb.zip"}[fmt]
    out = EXPORTS / f["key"] / f"{f['key']}_debris_flow.{ext}"
    kind = {"geojson": "application/geo+json", "csv": "text/csv"}.get(fmt, "application/zip")
    with export_lock:
        if out.exists() and out.stat().st_mtime >= stamp:
            return out.read_bytes(), out.name, kind
        out.parent.mkdir(parents=True, exist_ok=True)
        # Build under a temporary name and rename when complete, so a failed export is
        # never mistaken for a cached one
        part = out.with_name(out.name + ".part")
        write_export(f, fmt, src, part)
        os.replace(part, out)
    return out.read_bytes(), out.name, kind


def write_export(f, fmt, src, target):
    if fmt == "geojson":        # stream segments, WGS 84 as GeoJSON requires
        with fiona.open(src["segments"]) as c:
            feats = [{"type": "Feature", "properties": dict(x["properties"]),
                      "geometry": mapping(shape(transform_geom(c.crs, "EPSG:4326", x["geometry"])))}
                     for x in c]
        target.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    elif fmt == "csv":          # segment attributes only
        with fiona.open(src["segments"]) as c, open(target, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(c.schema["properties"]))
            w.writeheader()
            for x in c:
                w.writerow(dict(x["properties"]))
    elif fmt == "shp":
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for name, shp in src.items():
                for piece in shp.parent.glob(shp.stem + ".*"):
                    z.write(piece, f"{f['key']}_{name}{piece.suffix}")
    else:                       # file geodatabase: one feature class per layer, then zipped
        # A fresh folder each time: GDAL can keep a failed geodatabase's files open,
        # and Windows will not delete an open file
        work = Path(tempfile.mkdtemp(dir=target.parent))
        gdb = work / f"{f['key']}_debris_flow.gdb"
        try:
            # Our polygons are already valid; GDAL's re-organizing of polygons with many
            # parts (Wapiti's perimeter has 65 unburned islands) errors out otherwise
            with fiona.Env(OGR_ORGANIZE_POLYGONS="SKIP"):
                for name, shp in src.items():
                    with fiona.open(shp) as c:
                        records, schema = gdb_records(c)
                    with fiona.open(gdb, "w", driver="OpenFileGDB", layer=name,
                                    crs="EPSG:4326", schema=schema) as dst:
                        dst.writerecords(records)
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                for piece in gdb.rglob("*"):
                    z.write(piece, piece.relative_to(work))
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, kind="application/json", filename=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, separators=(",", ":"))
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not parts:
                data = "const APP = true;\nconst FIRES = " + json.dumps(summaries()) + ";"
                return self.send(200, build_map.render(data), "text/html; charset=utf-8")
            if parts == ["api", "fires"]:
                return self.send(200, summaries())
            if parts == ["api", "lookup"]:
                try:
                    return self.send(*find(parse_qs(url.query)))
                except RuntimeError as e:        # WFIGS unreachable or refusing the query
                    return self.send(502, {"error": str(e)})
            if parts[:2] == ["api", "fires"] and len(parts) == 3:
                f = fires().get(parts[2])
                return self.send(200, payload(f)) if f else self.send(404, {"error": "no such fire"})
            if parts[:2] == ["api", "fires"] and len(parts) == 4 and parts[3] == "export":
                f = fires().get(parts[2])
                fmt = parse_qs(url.query).get("format", [""])[0]
                if not f or fmt not in ("geojson", "csv", "shp", "gdb"):
                    return self.send(404, {"error": "unknown fire or format"})
                body, name, kind = export(f, fmt)
                return self.send(200, body, kind, filename=name)
            if parts == ["api", "trash"]:
                return self.send(200, trash_list())
            if parts == ["api", "batches"]:
                return self.send(200, batches.listing())
            if parts[:2] == ["api", "batches"] and len(parts) == 3 and BATCH_RE.match(parts[2]):
                v = batches.view(parts[2])
                return self.send(200, v) if v else self.send(404, {"error": "no such batch"})
            if parts == ["api", "settings", "hpc"]:
                return self.send(200, hpc.public())
            if parts[:2] == ["api", "runs"] and len(parts) == 3 and parts[2] in runs:
                return self.send(200, run_status(parts[2]))
            if parts == ["favicon.ico"]:
                return self.send(204, b"", "image/x-icon")
            return self.send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {"error": str(e)})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts[:2] in (["api", "fires"], ["api", "trash"]) and len(parts) == 4:
            return self.manage(parts)
        if parts[:2] in (["api", "batches"], ["api", "settings"]):
            return self.batch_or_settings(parts)
        if parts != ["api", "runs"]:
            return self.send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            uid = str(body.get("unique_id", "")).strip().upper()
            if not UID_RE.match(uid):
                return self.send(400, {"error": "choose a fire first (Unique Fire ID, e.g. 2025-IDPAF-005844)"})
            found = lookup(uid=uid)
            if not found:
                return self.send(404, {"error": f"no WFIGS perimeter for {uid}"})
            card = describe(found[0])
            if card["year"] < 2016:                       # Sentinel-2 L2A starts in 2016
                return self.send(400, {"error": f"{card['name']} {card['year']}: Sentinel-2 "
                                                "imagery starts in 2016"})
            # Already assessed: show it rather than spend minutes re-running it
            done = on_map(uid)
            if done:
                return self.send(200, {"status": "done", "existing": True, "fire_key": done,
                                       "name": card["name"], "year": card["year"]})
            return self.send(202, run_status(start_run(card)["id"]))
        except (ValueError, json.JSONDecodeError):
            return self.send(400, {"error": "send JSON: {unique_id}"})
        except RuntimeError as e:
            return self.send(502 if "WFIGS" in str(e) else 409, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {"error": str(e)})

    def manage(self, parts):
        """POST /api/fires/<key>/keep {kept}, /api/fires/<key>/delete,
        /api/trash/<id>/restore, /api/trash/<id>/purge"""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            _, kind, name, action = parts
            if kind == "fires":
                f = fires().get(name)
                if not f:
                    return self.send(404, {"error": "no such fire"})
                if action == "keep":
                    set_kept(f, body.get("kept", True))
                    return self.send(200, {"key": f["key"], "kept": bool(body.get("kept", True))})
                if action == "delete":
                    return self.send(*delete_fire(f))
            else:
                # a trash id is a folder name we made; never a path
                if not re.fullmatch(r"\d{8}-\d{6}_[\w.\-]+", name) or not (TRASH / name / "manifest.json").exists():
                    return self.send(404, {"error": "no such deleted fire"})
                if action == "restore":
                    return self.send(*restore_fire(name))
                if action == "purge":
                    return self.send(*purge_fire(name))
            return self.send(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError):
            return self.send(400, {"error": "send JSON"})
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {"error": str(e)})

    def batch_or_settings(self, parts):
        """POST /api/batches {lines, where: local|hpc}, /api/batches/<name>/reconnect,
        /api/settings/hpc {profile fields}, /api/settings/hpc/password {password | clear, which},
        /api/settings/hpc/test"""
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 200_000:
                return self.send(413, {"error": "request too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if parts == ["api", "batches"]:
                lines = body.get("lines") or []
                if not isinstance(lines, list) or not all(isinstance(l, str) and len(l) < 200 for l in lines):
                    return self.send(400, {"error": "lines: a list of fire IDs or name,year[,state]"})
                where = body.get("where", "local")
                if where == "local":
                    with run_lock:
                        if isinstance(RUNNER, LocalRunner) and any(
                                r["status"] in ("queued", "running") for r in runs.values()):
                            return self.send(409, {"error": "a single-fire run is in progress on this "
                                                            "computer; wait for it to finish"})
                rec = batches.start(lines, where, int(body.get("workers", 3)))
                return self.send(202, batches.view(rec["name"]))
            if parts[:2] == ["api", "batches"] and len(parts) == 4 and parts[3] == "reconnect" \
                    and BATCH_RE.match(parts[2]):
                batches.reconnect(parts[2])
                return self.send(200, batches.view(parts[2]))
            if parts == ["api", "settings", "hpc"]:
                return self.send(200, hpc.public(hpc.save(body)))
            if parts == ["api", "settings", "hpc", "password"]:
                s = hpc.load()
                if not s["host"] or not s["user"]:
                    return self.send(400, {"error": "save the host and user first"})
                which = "jump" if body.get("which") == "jump" else "main"
                if body.get("clear"):
                    hpc.clear_password(s, which)
                else:
                    pw = body.get("password")
                    if not isinstance(pw, str) or not pw or len(pw) > 512:
                        return self.send(400, {"error": "enter the password"})
                    hpc.set_password(s, pw, which)
                return self.send(200, hpc.public(s))
            if parts == ["api", "settings", "hpc", "test"]:
                return self.send(200, {"checks": hpc.test()})
            return self.send(404, {"error": "not found"})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except RuntimeError as e:
            return self.send(409, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {"error": str(e)})

    def log_message(self, fmt, *args):      # keep the console to runs and errors
        if "/api/runs/" not in self.path and not (self.command == "GET" and "/api/batches" in self.path):
            sys.stderr.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")


if __name__ == "__main__":
    import argparse
    import shlex
    ap = argparse.ArgumentParser(description="Local debris-flow hazard app")
    ap.add_argument("--runner", choices=["local", "slurm"], default="local",
                    help="run fires on this machine, or submit them to Slurm")
    ap.add_argument("--slurm-prefix", default="",
                    help="command put in front of sbatch/sacct/squeue (default: none, run them here)")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    if args.runner == "slurm":
        # non-POSIX splitting keeps Windows backslashes but also the quotes; drop those
        RUNNER = SlurmRunner([t.strip('"') for t in shlex.split(args.slurm_prefix, posix=False)])
    batches.on_fetched = build_payload
    batches.resume()        # keep following HPC batches started before a restart
    print(f"Debris-flow hazard app: http://127.0.0.1:{args.port}  runner: {args.runner}"
          f"{f' via {args.slurm_prefix}' if args.slurm_prefix else ''}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
