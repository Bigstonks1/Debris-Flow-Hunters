"""Build the interactive debris-flow hazard map: one self-contained, offline HTML file.

Reads each fire's wildcat exports, its perimeter, DEM and burn severity, and writes
map/dist/debris_flow_map.html with everything inlined: Leaflet (map/vendor/), the stream
segments as compact JSON, and hillshade and severity overlays as PNGs in Web Mercator.
No server, no internet and no ArcGIS account needed; an online basemap is optional.

The thresholds are wildcat's. The storm slider re-evaluates the same published formulas
(M1 likelihood, Gartner volume, Cannon hazard) in the browser for other storms.

    python map/build_map.py
"""

import base64
import io
import json
import math
from pathlib import Path

import fiona
import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform, unary_union

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import PROJECT_SUFFIXES, fire_key, unique_id_of  # noqa: E402
HERE = Path(__file__).resolve().parent
OUT = HERE / "dist" / "debris_flow_map.html"
OVERLAY_RES = 30          # metres per pixel for hillshade and severity overlays
SIMPLIFY_DEG = 0.00003    # ~3 m; stream lines are drawn at a few pixels wide
DECIMALS = 5              # ~1 m coordinate precision

STATES = {"CO": "Colorado", "ID": "Idaho", "MT": "Montana", "WY": "Wyoming", "UT": "Utah",
          "NV": "Nevada", "OR": "Oregon", "WA": "Washington", "CA": "California",
          "AZ": "Arizona", "NM": "New Mexico"}


def discover():
    """Every fires/*.json whose wildcat project has finished.

    The project is the config's "map_project", else pipeline/run_fire.py's: the run on
    USGS's published BAER inputs when there is one, else the Sentinel-2 run. Optional
    config fields: map_name, map_order (default first), map_inputs (else the project's
    sources.json), usgs_reference.guidance_I15_50."""
    found = []
    for cfg in sorted((ROOT / "fires").glob("*.json")):
        fire = json.loads(cfg.read_text())
        key = fire_key(fire)
        options = [fire["map_project"]] if "map_project" in fire else             [f"data/{key}{s}" for s in PROJECT_SUFFIXES]
        project = next((p for p in options if (ROOT / p / "exports" / "segments.shp").exists()), None)
        if not project:
            print(f"skip {cfg.name}: no finished project at {' or '.join(options)}")
            continue
        path = ROOT / project / "sources.json"
        sources = json.loads(path.read_text()) if path.exists() else {}
        inputs = fire.get("map_inputs") or "; ".join(sources.values())
        sev = sources.get("severity", "")
        if "BARC" in sev:           # satellite classification, not field-checked
            severity = "BARC"
        elif sev.startswith("BAER") or (not sev and "BAER" in inputs and "soil burn severity" in inputs):
            severity = "BAER"       # older projects: only the hand-written description says
        else:
            severity = "estimated from dNBR"
        check = ROOT / project / "usgs_check.json"      # pipeline/usgs_check.py
        check = json.loads(check.read_text()) if check.exists() else None
        services = input_services(sources, fire, check)
        found.append({
            "key": key.replace(" ", "_"), "name": fire.get("map_name", fire["name"]),
            "year": fire["year"], "state": STATES.get(fire["state"], fire["state"]),
            "state_code": fire["state"], "unique_id": unique_id_of(fire),
            "project": project, "inputs": inputs, "severity_source": severity,
            "usgs_median": fire.get("usgs_reference", {}).get("guidance_I15_50"),
            "usgs_check": check, "services": services,
            "order": fire.get("map_order", 100),
            # app bookkeeping: its config, the key its data folders use, and whether it is
            # kept (protected from deletion; fires added in the app start not kept)
            "config": cfg.name, "data_key": key, "kept": fire.get("kept", True),
        })
    return sorted(found, key=lambda f: (f["order"], -f["year"], f["name"]))


# The services the pipeline reads each input from (pipeline/fires.py, recover.py,
# prepare_fire.py, sentinel2_dnbr.py, usgs_inputs.py, usgs_check.py; STATSGO via pfdf).
SERVICES = {
    "wfigs": ("WFIGS Interagency Perimeters (ArcGIS feature service)",
              "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters/FeatureServer"),
    "tnm": ("USGS 3DEP 1/3 arc-second tiles (The National Map, S3)",
            "https://prd-tnm.s3.amazonaws.com/index.html?prefix=StagedProducts/Elevation/13/TIFF/"),
    "dem_service": ("USGS 3DEP elevation image service (fallback when TNM S3 is down)",
                    "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"),
    "statsgo": ("USGS STATSGO soil KF (ScienceBase, via pfdf)",
                "https://www.sciencebase.gov/catalog/item/675083c6d34ea60e894354ad"),
    "landfire": ("LANDFIRE EVT image service", "https://lfps.usgs.gov/arcgis/rest/services"),
    "recover": ("RECOVER archive (Idaho State University GIS TReC)",
                "https://giscenter-sl.isu.edu/aoc/aoc_research/recover2/archive/"),
    "stac": ("Sentinel-2 L2A, Earth Search STAC (Element 84, AWS)",
             "https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a"),
    "sciencebase": ("USGS ScienceBase (published assessment inputs)", "https://www.sciencebase.gov/catalog"),
    "baer_sbs": ("Forest Service BAER soil burn severity (image service)",
                 "https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/"
                 "USFS_EDW_BAER_SoilBurnSeverityClassification/ImageServer"),
    "sda": ("USDA SSURGO, Soil Data Access", "https://sdmdataaccess.sc.egov.usda.gov"),
    "landsat": ("Landsat 8/9 L2, Microsoft Planetary Computer",
                "https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2"),
}


def input_services(sources, fire, check):
    """One row per input: the layer, what it is (sources.json), and the service it came from."""
    usgs_item = (fire.get("usgs_inputs") or {}).get("item")

    def service(layer, label):
        if layer == "perimeter":
            return SERVICES["wfigs"]
        if layer == "dem":
            if "3DEP image service" in label:
                return SERVICES["dem_service"]
            return SERVICES["recover"] if "RECOVER" in label or "LiDAR" in label else SERVICES["tnm"]
        if layer == "kf":
            return SERVICES["statsgo"]
        if layer == "evt":
            return SERVICES["recover"] if "RECOVER" in label else SERVICES["landfire"]
        if "Forest Service BAER map" in label:
            return SERVICES["baer_sbs"]
        if "published with USGS" in label:
            return (SERVICES["sciencebase"][0], usgs_item or SERVICES["sciencebase"][1])
        if layer == "dnbr" and "Landsat" in label:
            return SERVICES["landsat"]
        if layer == "dnbr" and "Sentinel-2" in label:
            return SERVICES["stac"]
        if layer == "severity" and "estimated" in label:
            return ("Computed here from dNBR (125 / 250 / 500)", None)
        return (None, None)

    names = {"perimeter": "Fire perimeter", "dem": "Elevation", "kf": "Soil erodibility (KF)",
             "evt": "Vegetation type (water, developed land)", "dnbr": "Burn imagery (dNBR)",
             "severity": "Burn severity"}
    rows = []
    for layer, label in sources.items():
        name, url = service(layer, label)
        rows.append({"layer": names.get(layer, layer), "source": label, "service": name, "url": url})
        if layer == "kf" and "SSURGO" in label:      # gaps filled from SSURGO (prepare_fire.fill_kf)
            rows.append({"layer": "Soil erodibility gaps", "source": "SSURGO, rescaled to STATSGO",
                         "service": SERVICES["sda"][0], "url": SERVICES["sda"][1]})
    if check and check.get("found"):
        rows.append({"layer": "USGS assessment (comparison)", "source": check.get("code"),
                     "service": "USGS ScienceBase (published assessment)", "url": check.get("source")})
    return rows


def rnd(v, n=2):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return round(float(v), n)


def segments(project):
    """Stream segments as {coords: [[lon,lat],...], p: [values...]} in a compact form."""
    fields = ["Segment_ID", "I15_50", "P_24mmh", "V_24mmh", "H_24mmh", "Area_km2",
              "Terrain_M1", "Fire_M1", "Soil_M1", "Relief_m", "Bmh_km2"]
    # The storm slider recomputes likelihood, volume and hazard from the model inputs, so
    # those keep enough digits for its classes to match wildcat's at the 20/40/60/80% and
    # 10^n m3 edges. wildcat's own 24 mm/h results ride along as a check.
    digits = [0, 1, 6, 2, 0, 3, 6, 6, 6, 2, 6]
    lines, props = [], []
    with fiona.open(ROOT / project / "exports" / "segments.shp") as c:
        for f in c:
            g = shape(f["geometry"]).simplify(SIMPLIFY_DEG, preserve_topology=False)
            coords = [[round(x, DECIMALS), round(y, DECIMALS)] for x, y in g.coords]
            lines.append(coords)
            props.append([rnd(f["properties"][k], d) for k, d in zip(fields, digits)])
    return {"fields": fields, "lines": lines, "props": props}


def perimeter(path):
    path = ROOT / path
    if path.suffix == ".geojson":
        feat = json.loads(path.read_text())
        geom, src = shape(feat["geometry"]), feat.get("crs", {}).get("properties", {}).get("name")
        crs = src or "EPSG:4326"
    else:
        with fiona.open(path) as c:
            geom, crs = unary_union([shape(f["geometry"]) for f in c]), c.crs_wkt
    to_wgs = Transformer.from_crs(crs, 4326, always_xy=True).transform
    geom = shapely_transform(to_wgs, geom).simplify(0.0001)
    return json.loads(json.dumps(mapping(geom)), parse_float=lambda s: round(float(s), 5)), \
        round(geom.area, 6)


def to_mercator(src_path, resampling, make, nodata_out=0):
    """Warp a raster to Web Mercator at OVERLAY_RES; return (array, lat/lon bounds)."""
    with rasterio.open(src_path) as src:
        dst_crs = "EPSG:3857"
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds, resolution=OVERLAY_RES)
        data = make(src)
        out = np.full((height, width), np.nan, dtype="float32")
        reproject(data.astype("float32"), out, src_transform=src.transform, src_crs=src.crs,
                  dst_transform=transform, dst_crs=dst_crs, src_nodata=np.nan,
                  dst_nodata=np.nan, resampling=resampling)
    left, top = transform.c, transform.f
    right, bottom = left + width * transform.a, top + height * transform.e
    to_wgs = Transformer.from_crs(3857, 4326, always_xy=True)
    (w, s), (e, n) = to_wgs.transform(left, bottom), to_wgs.transform(right, top)
    return out, [[s, w], [n, e]]


def png_data_uri(rgba):
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def hillshade(project):
    def make(src):
        z = src.read(1).astype("float64")
        if src.nodata is not None:
            z[z == src.nodata] = np.nan
        dx, dy = abs(src.res[0]), abs(src.res[1])
        gy, gx = np.gradient(z, dy, dx)
        slope = np.arctan(np.hypot(gx, gy))
        aspect = np.arctan2(-gx, gy)
        az, alt = np.radians(315), np.radians(45)
        hs = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
        return np.clip(hs, 0, 1)
    hs, bounds = to_mercator(ROOT / project / "preprocessed" / "dem.tif", Resampling.average, make)
    # Light, low-contrast relief so the stream colours carry the map
    g = np.where(np.isfinite(hs), 176 + 72 * hs, 0).astype("uint8")
    rgba = np.dstack([g, g, g, np.where(np.isfinite(hs), 255, 0).astype("uint8")])
    return png_data_uri(rgba), bounds


# Burn severity drawn in neutral greys so it never competes with the hazard hue
SEVERITY_RGBA = {2: (214, 208, 196, 150), 3: (150, 142, 128, 170), 4: (92, 84, 72, 190)}


def severity(project):
    def make(src):
        s = src.read(1).astype("float32")
        return np.where(np.isin(s, [1, 2, 3, 4]), s, np.nan)
    # preprocessed/ holds severity whether it was supplied (BAER) or estimated by wildcat
    sev, bounds = to_mercator(ROOT / project / "preprocessed" / "severity.tif", Resampling.nearest,
                              make)
    rgba = np.zeros(sev.shape + (4,), dtype="uint8")
    for cls, colour in SEVERITY_RGBA.items():
        rgba[sev == cls] = colour
    return png_data_uri(rgba), bounds


def median(values):
    v = [x for x in values if x is not None]
    return round(float(np.median(v)), 1) if v else None


SUMMARY = ("key", "name", "year", "state", "unique_id", "usgs_median", "usgs_check", "inputs", "services",
           "severity_source", "stats", "kept")   # enough to list a fire; the rest is its map layers


def fire_payload(f):
    """Everything the map shows for one discovered fire (see discover())."""
    seg = segments(f["project"])
    per, _ = perimeter(f"{f['project']}/inputs/perimeter.shp")
    hs_uri, hs_bounds = hillshade(f["project"])
    sev_uri, sev_bounds = severity(f["project"])
    idx = {k: i for i, k in enumerate(seg["fields"])}
    m1 = [p[idx["I15_50"]] for p in seg["props"]]
    return {
        **{k: f[k] for k in ("key", "name", "year", "state", "unique_id", "usgs_median",
                             "usgs_check", "inputs", "services", "severity_source")},
        "segments": seg, "perimeter": per,
        "hillshade": {"uri": hs_uri, "bounds": hs_bounds},
        "severity": {"uri": sev_uri, "bounds": sev_bounds},
        "stats": {"n": len(m1), "m1_median": median(m1), "no_m1": sum(v is None for v in m1)},
    }


def render(data_js):
    """The map page with Leaflet inlined and `data_js` defining FIRES (and APP)."""
    template = (HERE / "template.html").read_text(encoding="utf-8")
    return (template
            .replace("/*LEAFLET_CSS*/", (HERE / "vendor" / "leaflet.css").read_text(encoding="utf-8"))
            .replace("/*LEAFLET_JS*/", (HERE / "vendor" / "leaflet.js").read_text(encoding="utf-8"))
            .replace("/*DATA*/", data_js))


def main():
    fires = []
    for f in discover():
        fires.append(fire_payload(f))
        s = fires[-1]["stats"]
        print(f"{f['name']}: {s['n']} segments, M1 median {s['m1_median']}, "
              f"{s['no_m1']} without M1")
    html = render("const APP = false;\nconst FIRES = "
                  + json.dumps(fires, separators=(",", ":")) + ";")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
