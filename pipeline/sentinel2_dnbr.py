"""Source B: compute a dNBR from Sentinel-2 ourselves, for fires with no BAER package.

Data: Sentinel-2 L2A Collection 1 on Earth Search (AWS, no login). NBR uses the two 20 m
bands BAER uses (its scene IDs end "_20m"): B8A (nir08) and B12 (swir22).

    NBR  = (B8A - B12) / (B8A + B12)
    dNBR = (NBR_pre - NBR_post) * 1000        (no offset: USGS/BAER do not apply one)

Collection 1 is reprocessed with baseline 05.00, which stores surface reflectance with
an additive offset: reflectance = DN * 0.0001 - 0.1 (the assets' raster:bands metadata).
NBR is a ratio, so leaving the -0.1 in would bias every value. It is applied here.

A date is usable when >= 98% of perimeter pixels are clear in the Scene Classification
Layer (cloud, cloud shadow, cirrus, snow, saturated and no-data are not clear). All tiles
of a date are mosaicked onto one 20 m UTM grid. Dates are chosen by rule, not copied from
BAER:

  post-fire  Candidates are clear dates from ignition + 14 d to containment + 30 d.
             Burned fraction (dNBR > 100 inside the perimeter, against a provisional
             pre-fire scene) is tracked across them, and the post-fire date is the first
             one after which it grows by less than PLATEAU (2 points of the perimeter).
             A fire that is still spreading keeps pushing the date later; Grizzly
             plateaus at the first candidate, Wapiti, which burned for three months,
             does not.
  pre-fire   If the post-fire date is within SAME_YEAR_DAYS (30) of ignition: the latest
             clear date in the 60 days before ignition. Otherwise the clear date nearest
             the post-fire date one year earlier (+-30 d), so both scenes share a
             season; a late-summer post scene against an early-summer pre scene reads
             senescence as burn. This is what BAER did for Wapiti (2023-09-15 pre).

Smoke is not a SCL class, so smoky scenes can pass; reported, not corrected.

    python pipeline/sentinel2_dnbr.py fires/wapiti.json
    python pipeline/sentinel2_dnbr.py fires/grizzly.json --pre 2020-08-07 --post 2020-08-27
"""

import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import fiona
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "4")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import fire_key  # noqa: E402
STAC = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-c1-l2a"
RES = 20
PAD_M = 4000                          # wildcat buffers the perimeter by 3 km
CLEAR_MIN = 0.98
NOT_CLEAR = {0, 1, 3, 8, 9, 10, 11}   # no data, saturated, shadow, cloud med/high, cirrus, snow
SCALE, OFFSET = 0.0001, -0.1
FILL = -32768
BURNED_DNBR = 100
PLATEAU = 0.02
SAME_YEAR_DAYS = 30


def load_perimeter(fire):
    if "perimeter" in fire:
        with fiona.open(ROOT / fire["perimeter"]) as c:
            return unary_union([shape(f["geometry"]) for f in c])
    path = ROOT / "data" / "recover" / fire_key(fire) / "perimeter_wfigs.geojson"
    return shape(json.loads(path.read_text())["geometry"])


class Grid:
    def __init__(self, geom, crs):
        left, bottom, right, top = geom.bounds
        left = np.floor((left - PAD_M) / RES) * RES
        top = np.ceil((top + PAD_M) / RES) * RES
        self.crs = crs
        self.width = int(np.ceil((right + PAD_M - left) / RES))
        self.height = int(np.ceil((top - (bottom - PAD_M)) / RES))
        self.transform = from_origin(left, top, RES, RES)
        self.inside = ~geometry_mask([mapping(geom)], (self.height, self.width), self.transform)
        to_wgs = Transformer.from_crs(crs, 4326, always_xy=True)
        x0, y0 = to_wgs.transform(left, top - self.height * RES)
        x1, y1 = to_wgs.transform(left + self.width * RES, top)
        self.bbox_wgs84 = [x0, y0, x1, y1]

    def read(self, items, band):
        """Mosaic one band from all tiles of a date onto the grid (first valid wins)."""
        out = np.zeros((self.height, self.width), dtype="uint16")
        for item in items:
            with rasterio.open(item["assets"][band]["href"]) as src, WarpedVRT(
                    src, crs=self.crs, transform=self.transform, width=self.width,
                    height=self.height, resampling=Resampling.nearest, nodata=0) as vrt:
                data = vrt.read(1)
            out = np.where(out == 0, data, out)
        return out


def search(grid, start, end):
    body = {"collections": [COLLECTION], "bbox": grid.bbox_wgs84, "limit": 500,
            "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z"}
    req = urllib.request.Request(STAC, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    by_date = {}
    for item in json.load(urllib.request.urlopen(req, timeout=180))["features"]:
        by_date.setdefault(item["properties"]["datetime"][:10], []).append(item)
    return by_date


class Scenes:
    """Clear-sky screening and NBR per date, cached so no date is read twice."""

    def __init__(self, grid):
        self.grid, self.clear, self.nbr = grid, {}, {}

    def clear_mask(self, day, items):
        if day not in self.clear:
            tile_cloud = min(i["properties"].get("eo:cloud_cover", 100) for i in items)
            if tile_cloud > 60:
                self.clear[day] = (0.0, None)
                print(f"    {day}: skipped (least-cloudy tile {tile_cloud:.0f}%)")
            else:
                scl = self.grid.read(items, "scl")
                mask = ~np.isin(scl, list(NOT_CLEAR))
                self.clear[day] = (mask[self.grid.inside].mean(), mask)
                print(f"    {day}: {self.clear[day][0]:.1%} of perimeter clear ({len(items)} tiles)")
        return self.clear[day]

    def is_clear(self, day, items):
        return self.clear_mask(day, items)[0] >= CLEAR_MIN

    def nbr_of(self, day, items):
        if day not in self.nbr:
            nir = self.grid.read(items, "nir08").astype(float)
            swir = self.grid.read(items, "swir22").astype(float)
            valid = (nir > 0) & (swir > 0)
            nir, swir = nir * SCALE + OFFSET, swir * SCALE + OFFSET
            with np.errstate(divide="ignore", invalid="ignore"):
                value = (nir - swir) / (nir + swir)
            self.nbr[day] = (value, valid & np.isfinite(value))
        return self.nbr[day]


def latest_clear(scenes, by_date, label):
    for day in sorted(by_date, reverse=True):
        if scenes.is_clear(day, by_date[day]):
            return day
    raise SystemExit(f"no clear {label} scene")


def nearest_clear(scenes, by_date, target, label):
    # Ties go to the earlier date (less autumn senescence and snow risk). Without the
    # explicit tie-break the order came from the STAC response, so the choice depended
    # on API ordering: at Wapiti 2023-09-15 and 2023-10-05 are both 10 d from target.
    def key(d):
        return abs((date.fromisoformat(d) - target).days), d
    for day in sorted(by_date, key=key):
        if scenes.is_clear(day, by_date[day]):
            return day
    raise SystemExit(f"no clear {label} scene near {target}")


def plateau_post(scenes, by_date, provisional_pre, pre_items):
    """First clear post-fire date after which burned fraction grows < PLATEAU."""
    pre_nbr, pre_ok = scenes.nbr_of(provisional_pre, pre_items)
    history = []
    for day in sorted(by_date):
        if not scenes.is_clear(day, by_date[day]):
            continue
        post_nbr, post_ok = scenes.nbr_of(day, by_date[day])
        ok = pre_ok & post_ok & scenes.grid.inside
        burned = ((pre_nbr - post_nbr) * 1000 > BURNED_DNBR)[ok].mean()
        print(f"      burned fraction at {day}: {burned:.1%}")
        if history and burned - history[-1][1] < PLATEAU:
            return history[-1][0], history
        history.append((day, burned))
    if not history:
        raise SystemExit("no clear post-fire scene")
    return history[-1][0], history


def main():
    args = sys.argv[1:]
    fire = json.loads(Path(args[0]).read_text())
    forced = {k: date.fromisoformat(args[args.index(k) + 1]) for k in ("--pre", "--post")
              if k in args}
    ignition = date.fromisoformat(fire["ignition"])
    containment = date.fromisoformat(fire["containment"])
    tag = ("forced_" + "_".join(forced[k].strftime("%Y%m%d") for k in ("--pre", "--post"))
           if forced else "auto")
    out_dir = ROOT / "data" / "s2_dnbr" / fire_key(fire) / tag
    out_dir.mkdir(parents=True, exist_ok=True)

    geom = load_perimeter(fire)
    grid = Grid(geom, f"EPSG:{fire['crs']}")
    scenes = Scenes(grid)
    print(f"grid: {grid.width} x {grid.height} at {RES} m, EPSG:{fire['crs']}; "
          f"perimeter {int(grid.inside.sum())} px")

    history = None
    if forced:
        pre_dates = search(grid, forced["--pre"], forced["--pre"])
        post_dates = search(grid, forced["--post"], forced["--post"])
        pre_day, post_day = forced["--pre"].isoformat(), forced["--post"].isoformat()
        for d, dates in ((pre_day, pre_dates), (post_day, post_dates)):
            scenes.clear_mask(d, dates[d])
    else:
        print("provisional pre-fire (latest clear in the 60 days before ignition):")
        pre_dates = search(grid, ignition - timedelta(days=60), ignition - timedelta(days=1))
        provisional = latest_clear(scenes, pre_dates, "pre-fire")
        print("post-fire, looking for the burned-area plateau:")
        post_dates = search(grid, ignition + timedelta(days=14),
                            containment + timedelta(days=30))
        post_day, history = plateau_post(scenes, post_dates, provisional, pre_dates[provisional])
        gap = (date.fromisoformat(post_day) - ignition).days
        if gap <= SAME_YEAR_DAYS:
            pre_day = provisional
            print(f"pre-fire: post is {gap} d after ignition -> same-year pre {pre_day}")
        else:
            target = date.fromisoformat(post_day) - timedelta(days=365)
            print(f"pre-fire: post is {gap} d after ignition -> anniversary pre near {target}:")
            pre_dates = search(grid, target - timedelta(days=30), target + timedelta(days=30))
            pre_day = nearest_clear(scenes, pre_dates, target, "anniversary pre-fire")

    pre_items, post_items = pre_dates[pre_day], post_dates[post_day]
    pre, pre_ok = scenes.nbr_of(pre_day, pre_items)
    post, post_ok = scenes.nbr_of(post_day, post_items)
    ok = pre_ok & post_ok & scenes.clear[pre_day][1] & scenes.clear[post_day][1]
    dnbr = np.full((grid.height, grid.width), FILL, dtype="int16")
    dnbr[ok] = np.clip(np.round((pre[ok] - post[ok]) * 1000), -2000, 2000).astype("int16")

    profile = dict(driver="GTiff", height=grid.height, width=grid.width, count=1,
                   dtype="int16", crs=grid.crs, transform=grid.transform, nodata=FILL,
                   compress="deflate", tiled=True)
    with rasterio.open(out_dir / "dnbr.tif", "w", **profile) as dst:
        dst.write(dnbr, 1)

    ring = ~geometry_mask([mapping(geom.buffer(1500))], dnbr.shape, grid.transform)
    ring &= ~grid.inside & ok
    summary = dict(pre=pre_day, post=post_day,
                   pre_tiles=[i["id"] for i in pre_items],
                   post_tiles=[i["id"] for i in post_items],
                   burned_fraction_history=history,
                   valid_in_perimeter=float(ok[grid.inside].mean()),
                   dnbr_perimeter_pct10_50_90=np.percentile(
                       dnbr[grid.inside & ok], [10, 50, 90]).tolist(),
                   unburned_ring_median=float(np.median(dnbr[ring])))
    (out_dir / "selection.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if "tiles" not in k}, indent=1))
    print(f"wrote {out_dir / 'dnbr.tif'}")


if __name__ == "__main__":
    main()
