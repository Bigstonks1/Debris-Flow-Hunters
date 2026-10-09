"""BAER soil burn severity for fires USGS has not assessed, from the USDA Forest Service's
public image service (no login, no key):

    https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/
        USFS_EDW_BAER_SoilBurnSeverityClassification/ImageServer

It holds the soil burn severity map each Forest Service BAER team produced, 2017 to the last
completed fire year, one catalog item per fire (category 1) with its footprint and year.
Classes are BARC4: 1 unburned, 2 low, 3 moderate, 4 high; 0 is no data. Severity is the
input that moves results most (design.md 8.3), and these maps are field-checked, so a fire
without USGS-published inputs uses one when it exists instead of wildcat's estimate.

Matching is by location and year, as for USGS assessments: the item must start in the fire's
year (or the next, for late-season fires) and cover at least MIN_OVERLAP of our perimeter. The
image is exported for that item alone (lockRaster) at its native cell size, in the fire's UTM
CRS. Checked 2026-10-08 on Wapiti 2024: identical, pixel for pixel, to the SBS in BAER's own
package (1,272,676 classified pixels).

Records fire["baer_sbs"] (item, name, overlap, path) or null. Never fails a run: no network or
no map is a result too, and the run falls back to estimated severity.

    python pipeline/baer_sbs.py fires/<key>.json
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Polygon, shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import fire_key  # noqa: E402

SERVICE = ("https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/"
           "USFS_EDW_BAER_SoilBurnSeverityClassification/ImageServer")
MIN_OVERLAP = 0.5


def get(url, params, timeout=120):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=timeout) as r:
        return r.read()


def find(fire, perimeter):
    """Best catalog item for this fire: (objectid, name, cell size, overlap) or None."""
    g = transform(Transformer.from_crs(fire["crs"], 3857, always_xy=True).transform, perimeter)
    l, b, r, t = g.bounds
    d = json.loads(get(f"{SERVICE}/query", {
        "where": f"category = 1 AND beginyear >= {fire['year']} AND beginyear <= {fire['year'] + 1}",
        "geometry": json.dumps({"xmin": l, "ymin": b, "xmax": r, "ymax": t,
                                "spatialReference": {"wkid": 3857}}),
        "geometryType": "esriGeometryEnvelope", "spatialRel": "esriSpatialRelIntersects",
        "inSR": 3857, "outSR": 3857, "outFields": "objectid,name,lowps,beginyear",
        "returnGeometry": "true", "f": "json"}))
    best = None
    for f in d.get("features", []):
        footprint = Polygon(f["geometry"]["rings"][0]).buffer(0)
        for ring in f["geometry"]["rings"][1:]:
            footprint = footprint.symmetric_difference(Polygon(ring).buffer(0))
        overlap = footprint.intersection(g).area / g.area
        a = f["attributes"]
        if overlap >= MIN_OVERLAP and (best is None or overlap > best[3]):
            best = (a["objectid"], a["name"], a["lowps"] or 20, overlap)
    return best


def export(objectid, cell, fire, perimeter, target, pad_m=5000):
    """The item's SBS over the padded perimeter, in the fire's CRS, as GeoTIFF bytes."""
    l, b, r, t = perimeter.buffer(pad_m).bounds
    width, height = round((r - l) / cell), round((t - b) / cell)
    data = get(f"{SERVICE}/exportImage", {
        "bbox": f"{l},{b},{l + width * cell},{b + height * cell}", "bboxSR": fire["crs"],
        "imageSR": fire["crs"], "size": f"{width},{height}", "format": "tiff",
        "pixelType": "U8", "noData": 0, "interpolation": "RSP_NearestNeighbor",
        "mosaicRule": json.dumps({"mosaicMethod": "esriMosaicLockRaster", "lockRasterIds": [objectid]}),
        "f": "image"}, timeout=600)
    if not data.startswith((b"II*\x00", b"MM\x00*")):
        raise RuntimeError(f"export failed: {data[:200]!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def main():
    config = Path(sys.argv[1])
    fire = json.loads(config.read_text())
    key = fire_key(fire)
    found = None
    try:
        perimeter = shape(json.loads((ROOT / "data" / "recover" / key / "perimeter_wfigs.geojson")
                                     .read_text())["geometry"])
        hit = find(fire, perimeter)
        if hit:
            objectid, name, cell, overlap = hit
            target = ROOT / "data" / "baer_sbs" / key / f"{name}.tif"
            if not target.exists():
                export(objectid, cell, fire, perimeter, target)
            found = {"item": objectid, "name": name, "overlap": round(overlap, 3),
                     "cell_m": cell, "path": target.relative_to(ROOT).as_posix(), "service": SERVICE}
            print(f"BAER SBS: {name} (item {objectid}), covers {100 * overlap:.0f}% of the perimeter, "
                  f"{cell:g} m -> {found['path']}")
        else:
            print("BAER SBS: no Forest Service BAER map covers this fire; severity will be estimated")
    except Exception as e:          # service down or changed: estimated severity instead
        print(f"BAER SBS: not available ({e}); severity will be estimated")
    fire["baer_sbs"] = found
    config.write_text(json.dumps(fire, indent=1) + "\n")


if __name__ == "__main__":
    main()
