"""Find USGS's own debris-flow assessment of a fire, fetch it, and compare it with ours.

Location first. USGS's assessment service (the layers behind its post-fire dashboard)
lists every assessment, 748 of them including the current year, with a point location,
start and assessment dates and a download link, and has perimeters for ~300 of them.
Candidates are the USGS perimeters that overlap this fire's perimeter (most shared area
first), then the assessment points near it, all starting within MAX_DAYS of the fire.
Names play no part: USGS renames and merges fires ("North Complex"), and same-named fires
exist (two 2024 "Coffee Pot" assessments, in Oregon and California). If the service is
unreachable, the ScienceBase archive is searched by name and date instead (2013-2024).

Each candidate's zip is fetched from its download link, keeping only what the comparison
needs; ScienceBase ignores Range requests, so an old-layout zip (Woodhead: 114 MB) comes
down once. Two layouts:
  2024                   <code>-segments.shp: T, F, S, I15_50, Area_km2
  2025 on                segments.shp: Terrain_M1, Fire_M1, Soil_M1, I15_50, Area
  2013-2023              <code>_Segment_RainfallEstimates_15min_RainAtP_50.shp: L_X1..3,
                         RainIntAtP, UpArea_km2, and <code>_ThresholdGuidance_Segment.txt
The fire-wide guidance is the Segment figure (median-thresholds.csv or the Segment guidance
file, never the Basin one), else the median of the segment thresholds.

A candidate is accepted only if at least one of its segments matches one of ours within
20 m (compare_usgs.compare): the final check that it is the same fire. The
verified reference goes into the fire config (`usgs_reference`) and the comparison into
<project>/usgs_check.json, which the app shows. Never fails a run.

    python pipeline/usgs_check.py fires/wapiti.json [project]
"""

import csv
import io
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import fiona
import numpy as np
from pyproj import Transformer
from shapely.geometry import Point, mapping, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import finished_project, fire_key  # noqa: E402

SERVICE = ("https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
           "USGS_Post_Fire_Hazard_Assessment_Data/FeatureServer")
POINTS, PERIMETERS = 0, 9
SB = "https://www.sciencebase.gov/catalog"
OUT = ROOT / "data" / "usgs"
MAX_DAYS = 60
NEAR_KM = 25        # assessment points are rounded to ~0.1 degree, so allow ~10 km slack
DNBR_OK = (0.8, 1.25)   # F ratio band outside which USGS's dNBR is clearly a different product
FIELDS = {"T": ("T", "Terrain_M1", "L_X1"), "F": ("F", "Fire_M1", "L_X2"),
          "S": ("S", "Soil_M1", "L_X3"), "I15_50": ("I15_50", "RainIntAtP"),
          "area_km2": ("Area_km2", "Area", "UpArea_km2")}
KEEP = re.compile(r"(segments\.|_Segment_RainfallEstimates_15min_RainAtP_50\.|"
                  r"median-thresholds\.csv$|_ThresholdGuidance_Segment\.txt$|metadata\.txt$)", re.I)


def get_json(url, params):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=120) as r:
        data = json.load(r)
    if "error" in data:
        raise RuntimeError(f"USGS service: {data['error'].get('message')}")
    return data


def ms_day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date() if ms else None


# ---------------------------------------------------------------- finding candidates

def perimeter_ll(fire, project):
    """This fire's perimeter in WGS 84 (WFIGS copy from recover.py, else the project's)."""
    wfigs = ROOT / "data" / "recover" / fire_key(fire) / "perimeter_wfigs.geojson"
    if wfigs.exists():
        geom, crs = shape(json.loads(wfigs.read_text())["geometry"]), f"EPSG:{fire['crs']}"
    else:
        with fiona.open(Path(project) / "inputs" / "perimeter.shp") as c:
            geom, crs = unary_union([shape(f["geometry"]) for f in c]), c.crs
    return transform(Transformer.from_crs(crs, 4326, always_xy=True).transform, geom)


def by_location(fire, project):
    """Service candidates, best first: USGS perimeters overlapping ours (by shared area),
    then assessment points within NEAR_KM of it. Each: code, name, dates, download."""
    per = perimeter_ll(fire, project)
    start = date.fromisoformat(fire["ignition"])
    lo, hi = start - timedelta(days=MAX_DAYS), start + timedelta(days=MAX_DAYS)
    when = f"start_date >= DATE '{lo}' AND start_date <= DATE '{hi}'"
    x0, y0, x1, y1 = per.bounds
    pad = NEAR_KM / 80                                     # km -> degrees, generously
    box = {"xmin": x0 - pad, "ymin": y0 - pad, "xmax": x1 + pad, "ymax": y1 + pad,
           "spatialReference": {"wkid": 4326}}
    common = {"geometry": json.dumps(box), "geometryType": "esriGeometryEnvelope",
              "inSR": 4326, "outSR": 4326, "spatialRel": "esriSpatialRelIntersects",
              "where": when, "f": "json"}
    points = get_json(f"{SERVICE}/{POINTS}/query", {**common, "outFields": "*"})["features"]
    info = {p["attributes"]["Fire_ID"]: p for p in points}
    perims = get_json(f"{SERVICE}/{PERIMETERS}/query",
                      {**common, "outFields": "Fire_ID", "geometryPrecision": 5})["features"]
    overlap = {}
    for f in perims:
        rings = f.get("geometry", {}).get("rings") or []
        if rings:
            theirs = unary_union([shape({"type": "Polygon", "coordinates": [r]}).buffer(0) for r in rings])
            overlap[f["attributes"]["Fire_ID"]] = per.intersection(theirs).area / per.area
    # km from each assessment point to our perimeter, in a local metric projection
    to_m = Transformer.from_crs(4326, f"+proj=aeqd +lat_0={per.centroid.y} +lon_0={per.centroid.x}",
                                always_xy=True).transform
    per_m = transform(to_m, per)
    found = []
    for code, p in info.items():
        a = p["attributes"]
        km = per_m.distance(Point(*to_m(a["lon"], a["lat"]))) / 1000
        if overlap.get(code, 0) > 0 or km <= NEAR_KM:
            found.append({"code": code, "name": a["fire"], "start": str(ms_day(a["start_date"])),
                          "assessment_date": str(ms_day(a["assessment_date"])),
                          "url": (a.get("download_url") or "").strip() or None,
                          "item": (a.get("ScienceBaseURL") or "").strip() or None,
                          "overlap": round(overlap.get(code, 0), 3), "km": round(km, 1),
                          "days": abs((ms_day(a["start_date"]) - start).days)})
    return sorted(found, key=lambda c: (-c["overlap"], c["km"], c["days"]))


def by_name(fire):
    """Fallback when the service is down: the ScienceBase archive, by name and date."""
    def norm(name):
        words = re.sub(r"[^a-z ]", " ", name.lower().replace("-", " ").replace("_", " ")).split()
        return "".join(w for w in words if w not in ("fire", "complex"))
    names = {norm(fire["name"]), norm(fire.get("map_name", "")), norm(fire.get("wfigs_name", ""))} - {""}
    title = f"{fire['year']} Post-Wildfire Debris-Flow Hazard Assessments"
    hits = [i for i in get_json(f"{SB}/items", {"q": title, "fields": "title", "max": 20,
                                                "format": "json"})["items"] if i["title"] == title]
    if not hits:
        return []
    start = date.fromisoformat(fire["ignition"])
    found = []
    for f in get_json(f"{SB}/item/{hits[0]['id']}", {"format": "json"}).get("files", []):
        m = re.match(r"^(.*)_(\d{4}-\d{2}-\d{2})_(\w+)\.zip$", f["name"])
        if m and norm(m[1]) in names:
            days = abs((date.fromisoformat(m[2]) - start).days)
            if days <= MAX_DAYS:
                found.append({"code": m[3], "name": m[1], "start": m[2], "assessment_date": None,
                              "url": f["url"], "item": f"{SB}/item/{hits[0]['id']}",
                              "overlap": None, "km": None, "days": days})
    return sorted(found, key=lambda c: c["days"])


# ---------------------------------------------------------------- fetching one

def fetch(c):
    """Download a candidate's zip, keep what the comparison needs; a `usgs_reference`."""
    out = OUT / re.sub(r"[^\w.-]", "_", c["code"])
    tmp = out / "archive.zip.part"
    out.mkdir(parents=True, exist_ok=True)
    try:
        for attempt in range(3):        # ScienceBase answers 502 now and then
            try:
                with urllib.request.urlopen(c["url"], timeout=1800) as r, open(tmp, "wb") as fh:
                    shutil.copyfileobj(r, fh, 1 << 20)
                break
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 2:
                    raise
                time.sleep(10 * (attempt + 1))
        with zipfile.ZipFile(tmp) as z:
            for n in z.namelist():
                if KEEP.search(Path(n).name) and not n.endswith("/"):
                    (out / Path(n).name).write_bytes(z.read(n))
    finally:
        tmp.unlink(missing_ok=True)
    ref = {"code": c["code"], "source": c["item"] or c["url"], "usgs_start_date": c["start"],
           "assessment_date": c["assessment_date"], "auto": True}
    seg = next(iter(sorted(out.glob("*segments.shp"))
                    + sorted(out.glob("*_Segment_RainfallEstimates_15min_RainAtP_50.shp"))), None)
    if not seg:
        raise RuntimeError(f"{c['code']}: no segment thresholds in the download")
    # Field names differ by vintage: 2013-2023 L_X1..3/RainIntAtP/UpArea_km2, 2024
    # T/F/S/I15_50/Area_km2, 2025 Terrain_M1/Fire_M1/Soil_M1/I15_50/Area. Take them from
    # the file rather than assume a layout per year.
    with fiona.open(seg) as s:
        have = set(s.schema["properties"])
    fields = {}
    for key, names in FIELDS.items():
        fields[key] = next((n for n in names if n in have), None)
        if not fields[key]:
            raise RuntimeError(f"{c['code']}: no {key} field in {seg.name} ({sorted(have)[:12]}...)")
    ref.update(segments=str(seg.relative_to(ROOT)).replace("\\", "/"), fields=fields)
    # Fire-wide guidance: the Segment figure, as USGS reports it
    csv_file = next(out.glob("*median-thresholds.csv"), None)
    txt_file = next(out.glob("*_ThresholdGuidance_Segment.txt"), None)
    if csv_file:
        rows = {r[0].strip(): r for r in csv.reader(io.StringIO(csv_file.read_text()))}
        ref["guidance_I15_50"] = round(float(rows["Segments"][[h.strip() for h in rows["Type"]].index("I15_50")]), 1)
    elif txt_file:
        row = next(r for r in csv.DictReader(open(txt_file))
                   if r["Duration_min"] == "15" and r["Threshold_P"] == "50")
        ref["guidance_I15_50"] = float(row["Int_mmh-1"])
    else:
        with fiona.open(seg) as s:
            v = [f["properties"][fields["I15_50"]] for f in s]
        ref["guidance_I15_50"] = round(float(np.nanmedian([x for x in v if x is not None])), 1)
    return ref


# ---------------------------------------------------------------- main

def main():
    from compare_usgs import compare
    config = Path(sys.argv[1])
    fire = json.loads(config.read_text())
    project = Path(sys.argv[2]) if len(sys.argv) > 2 else \
        (finished_project(fire_key(fire)) or ROOT / "data" / f"{fire_key(fire)}_s2_auto_estimate")
    result = {"checked": date.today().isoformat()}
    try:
        ref, stats, tried = fire.get("usgs_reference"), None, []
        if ref:
            stats = compare(ref, project)
        else:
            try:
                candidates, how = by_location(fire, project), "location"
            except Exception as e:              # service down: archive by name
                print(f"USGS service unavailable ({e}); searching ScienceBase by name")
                candidates, how = by_name(fire), "name"
            for c in candidates:
                where = (f"{100 * c['overlap']:.0f}% perimeter overlap" if c.get("overlap")
                         else f"{c['km']} km away" if c.get("km") is not None else "name match")
                print(f"candidate {c['code']} ({c['name']}, started {c['start']}, {where})")
                if not c["url"]:
                    tried.append(f"{c['code']}: listed by USGS but no download yet")
                    continue
                cand = fetch(c)
                s = compare(cand, project)
                if s["pairs"]:          # shares streams with ours: same fire
                    ref, stats = cand, s
                    fire["usgs_reference"] = ref
                    config.write_text(json.dumps(fire, indent=1) + "\n")
                    break
                tried.append(f"{c['code']} ({c['name']}): shares no streams with this fire")
            result["matched_by"] = how
        if ref and stats:
            result.update(found=True, code=ref.get("code"), source=ref.get("source"),
                          assessment_date=ref.get("assessment_date"), **stats)
            f = result["F_ratio"]
            if not DNBR_OK[0] <= f <= DNBR_OK[1]:
                result["note"] = (f"our dNBR reads {f:.2f}x USGS's: they used a different "
                                  "burn-severity product, so thresholds are not like for like")
        else:
            result.update(found=False, note=("; ".join(tried) if tried else
                          f"no USGS assessment near {fire['name']} {fire['year']}"))
    except Exception as e:          # the check is informative only; never fail the run
        result.update(found=None, note=f"USGS check failed: {e}")
    # NaN is not JSON; the app parses this file
    result = {k: (None if isinstance(v, float) and v != v else v) for k, v in result.items()}
    (project / "usgs_check.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
