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
import time
import urllib.error
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
# Landsat 8/9 Collection 2 Level-2 on Microsoft's Planetary Computer: free, no account. Its
# files are read with a short-lived anonymous token from the SAS endpoint. (Earth Search also
# lists Landsat, but its files sit in USGS's requester-pays bucket, which costs money.)
PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
PC_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsat-c2-l2"
RES = 20
PAD_M = 4000                          # wildcat buffers the perimeter by 3 km
CLEAR_MIN = 0.98
NOT_CLEAR = {0, 1, 3, 8, 9, 10, 11}   # no data, saturated, shadow, cloud med/high, cirrus, snow
SCALE, OFFSET = 0.0001, -0.1
# Landsat QA_PIXEL bits: 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow, 5 snow
LANDSAT_NOT_CLEAR = 0b111111
SENSORS = {
    "Sentinel-2": dict(stac=STAC, collection=COLLECTION, query=None, nir="nir08", swir="swir22",
                       qa="scl", scale=SCALE, offset=OFFSET),
    "Landsat 8/9": dict(stac=PC_STAC, collection="landsat-c2-l2",
                        query={"platform": {"in": ["landsat-8", "landsat-9"]}},
                        nir="nir08", swir="swir22", qa="qa_pixel", scale=0.0000275, offset=-0.2),
}
# Composites, when no single date is clear enough: per pixel, the nearest date clear there.
COMPOSITE_DATE_MIN = 0.2              # a date must be at least this clear to contribute
COMPOSITE_MAX_DATES = 6
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
            with rasterio.open(href(item, band)) as src, WarpedVRT(
                    src, crs=self.crs, transform=self.transform, width=self.width,
                    height=self.height, resampling=Resampling.nearest, nodata=0) as vrt:
                data = vrt.read(1)
            out = np.where(out == 0, data, out)
        return out


SENSOR = SENSORS["Sentinel-2"]       # the sensor in use; main() switches it for the fallback
_token = {}


def href(item, band):
    url = item["assets"][SENSOR[band]]["href"]
    if SENSOR["stac"] == PC_STAC:       # Planetary Computer: append the anonymous read token
        if not _token or time.time() > _token["at"] + 1800:
            _token.update(token=get_json(PC_TOKEN)["token"], at=time.time())
        url += "?" + _token["token"]
    return url


def get_json(url, body=None, tries=4):
    for i in range(tries):      # both services return the odd 502/504
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=180))
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def search(grid, start, end):
    body = {"collections": [SENSOR["collection"]], "bbox": grid.bbox_wgs84, "limit": 500,
            "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z"}
    if SENSOR["query"]:
        body["query"] = SENSOR["query"]
    by_date = {}
    for item in get_json(SENSOR["stac"], body)["features"]:
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
                qa = self.grid.read(items, "qa")
                if SENSOR["qa"] == "scl":
                    mask = ~np.isin(qa, list(NOT_CLEAR))
                else:                   # Landsat QA_PIXEL: no fill, cloud, shadow or snow bit
                    mask = (qa & LANDSAT_NOT_CLEAR) == 0
                self.clear[day] = (mask[self.grid.inside].mean(), mask)
                print(f"    {day}: {self.clear[day][0]:.1%} of perimeter clear ({len(items)} tiles)")
        return self.clear[day]

    def is_clear(self, day, items):
        return self.clear_mask(day, items)[0] >= CLEAR_MIN

    def nbr_of(self, day, items):
        if day not in self.nbr:
            nir = self.grid.read(items, "nir").astype(float)
            swir = self.grid.read(items, "swir").astype(float)
            valid = (nir > 0) & (swir > 0)
            scale, offset = SENSOR["scale"], SENSOR["offset"]
            nir, swir = nir * scale + offset, swir * scale + offset
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


def single_date(scenes, grid, ignition, containment):
    """The date rule above: one clear pre and one clear post date."""
    print("provisional pre-fire (latest clear in the 60 days before ignition):")
    pre_dates = search(grid, ignition - timedelta(days=60), ignition - timedelta(days=1))
    provisional = latest_clear(scenes, pre_dates, "pre-fire")
    print("post-fire, looking for the burned-area plateau:")
    post_dates = search(grid, ignition + timedelta(days=14), containment + timedelta(days=30))
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
    pre, pre_ok = scenes.nbr_of(pre_day, pre_dates[pre_day])
    post, post_ok = scenes.nbr_of(post_day, post_dates[post_day])
    return (pre, pre_ok & scenes.clear[pre_day][1], post, post_ok & scenes.clear[post_day][1],
            dict(method="single date", pre=[pre_day], post=[post_day],
                 burned_fraction_history=history,
                 pre_tiles=[i["id"] for i in pre_dates[pre_day]],
                 post_tiles=[i["id"] for i in post_dates[post_day]]))


def composite(scenes, by_date, target, label):
    """Per pixel, the NBR of the date nearest `target` on which that pixel is clear, from at
    most COMPOSITE_MAX_DATES dates that are each at least COMPOSITE_DATE_MIN clear, until
    CLEAR_MIN of the perimeter is covered. Returns (nbr, valid, dates used)."""
    grid = scenes.grid
    nbr = np.full((grid.height, grid.width), np.nan)
    have = np.zeros((grid.height, grid.width), dtype=bool)
    used = []
    for day in sorted(by_date, key=lambda d: (abs((date.fromisoformat(d) - target).days), d)):
        fraction, mask = scenes.clear_mask(day, by_date[day])
        if fraction < COMPOSITE_DATE_MIN:
            continue
        value, ok = scenes.nbr_of(day, by_date[day])
        take = ok & mask & ~have
        if not take[grid.inside].any():
            continue
        nbr[take] = value[take]
        have |= take
        used.append(day)
        cover = have[grid.inside].mean()
        print(f"      {label} composite: + {day} -> {cover:.1%} of perimeter covered")
        if cover >= CLEAR_MIN or len(used) == COMPOSITE_MAX_DATES:
            break
    cover = have[grid.inside].mean()
    if not used or cover < CLEAR_MIN:
        raise SystemExit(f"no {label} composite covers {CLEAR_MIN:.0%} of the perimeter "
                         f"({cover:.1%} from {len(used)} dates)")
    return nbr, have, used


def composites(scenes, grid, ignition, containment):
    """Pre and post composites. Post: dates from ignition + 14 d to containment + 30 d,
    nearest containment first (the fire at full extent). Pre: as the single-date rule, the
    60 days before ignition when the post dates are within SAME_YEAR_DAYS of it, otherwise
    the same season a year earlier."""
    post_dates = search(grid, ignition + timedelta(days=14), containment + timedelta(days=30))
    post, post_ok, post_used = composite(scenes, post_dates, containment, "post-fire")
    middle = sorted(post_used)[len(post_used) // 2]
    if (date.fromisoformat(middle) - ignition).days <= SAME_YEAR_DAYS:
        pre_dates = search(grid, ignition - timedelta(days=60), ignition - timedelta(days=1))
        pre, pre_ok, pre_used = composite(scenes, pre_dates, ignition, "pre-fire")
    else:
        target = date.fromisoformat(middle) - timedelta(days=365)
        pre_dates = search(grid, target - timedelta(days=45), target + timedelta(days=45))
        pre, pre_ok, pre_used = composite(scenes, pre_dates, target, "anniversary pre-fire")
    return pre, pre_ok, post, post_ok, dict(
        method=f"composite of {len(pre_used)} pre and {len(post_used)} post dates",
        pre=sorted(pre_used), post=sorted(post_used))


def main():
    global SENSOR
    args = sys.argv[1:]
    fire = json.loads(Path(args[0]).read_text())
    forced = {k: date.fromisoformat(args[args.index(k) + 1]) for k in ("--pre", "--post")
              if k in args}
    ignition = date.fromisoformat(fire["ignition"])
    containment = date.fromisoformat(fire["containment"])
    # --sensor, or DFH_DNBR_SENSOR / DFH_DNBR_METHOD (single|composite) to force a fallback
    # through a whole run_fire.py run, for testing
    only = args[args.index("--sensor") + 1] if "--sensor" in args else os.environ.get("DFH_DNBR_SENSOR")
    only_method = os.environ.get("DFH_DNBR_METHOD")
    tag = ("forced_" + "_".join(forced[k].strftime("%Y%m%d") for k in ("--pre", "--post"))
           if forced else "auto" if not only or "--sensor" not in args else f"only_{only}")
    out_dir = ROOT / "data" / "s2_dnbr" / fire_key(fire) / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    geom = load_perimeter(fire)
    grid = Grid(geom, f"EPSG:{fire['crs']}")
    print(f"grid: {grid.width} x {grid.height} at {RES} m, EPSG:{fire['crs']}; "
          f"perimeter {int(grid.inside.sum())} px")

    if forced:
        sensor = "Sentinel-2"
        scenes = Scenes(grid)
        pre_dates = search(grid, forced["--pre"], forced["--pre"])
        post_dates = search(grid, forced["--post"], forced["--post"])
        pre_day, post_day = forced["--pre"].isoformat(), forced["--post"].isoformat()
        for d, dates in ((pre_day, pre_dates), (post_day, post_dates)):
            scenes.clear_mask(d, dates[d])
        pre, pre_ok = scenes.nbr_of(pre_day, pre_dates[pre_day])
        post, post_ok = scenes.nbr_of(post_day, post_dates[post_day])
        pre_ok &= scenes.clear[pre_day][1]
        post_ok &= scenes.clear[post_day][1]
        meta = dict(method="forced dates", pre=[pre_day], post=[post_day])
    else:
        # Fallbacks, all free: Sentinel-2 single dates (the rule above), a Sentinel-2
        # composite, then the same two with Landsat 8/9 (more clear dates, and before 2016).
        # --sensor landsat|sentinel2 tries one sensor only (for comparisons).
        sensors = {"landsat": ["Landsat 8/9"], "sentinel2": ["Sentinel-2"]}.get(
            only, ["Sentinel-2", "Landsat 8/9"])
        failures, result = [], None
        for sensor in sensors:
            SENSOR = SENSORS[sensor]
            scenes = Scenes(grid)
            for method in [m for m in (single_date, composites)
                           if not only_method or m.__name__.startswith(only_method)]:
                print(f"--- {sensor}, {method.__name__.replace('_', ' ')}")
                try:
                    result = method(scenes, grid, ignition, containment)
                    break
                except SystemExit as e:
                    failures.append(f"{sensor} {method.__name__.replace('_', ' ')}: {e}")
                    print(f"    {e}")
            if result:
                break
        if not result:
            raise SystemExit("no usable imagery: " + "; ".join(failures))
        pre, pre_ok, post, post_ok, meta = result
        meta["fallbacks_tried"] = failures

    ok = pre_ok & post_ok
    dnbr = np.full((grid.height, grid.width), FILL, dtype="int16")
    dnbr[ok] = np.clip(np.round((pre[ok] - post[ok]) * 1000), -2000, 2000).astype("int16")
    profile = dict(driver="GTiff", height=grid.height, width=grid.width, count=1,
                   dtype="int16", crs=grid.crs, transform=grid.transform, nodata=FILL,
                   compress="deflate", tiled=True)
    with rasterio.open(out_dir / "dnbr.tif", "w", **profile) as dst:
        dst.write(dnbr, 1)

    ring = ~geometry_mask([mapping(geom.buffer(1500))], dnbr.shape, grid.transform)
    ring &= ~grid.inside & ok
    summary = dict(sensor=sensor, **meta,
                   valid_in_perimeter=float(ok[grid.inside].mean()),
                   dnbr_perimeter_pct10_50_90=np.percentile(
                       dnbr[grid.inside & ok], [10, 50, 90]).tolist(),
                   unburned_ring_median=float(np.median(dnbr[ring])))
    (out_dir / "selection.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if "tiles" not in k}, indent=1))
    print(f"wrote {out_dir / 'dnbr.tif'}")


if __name__ == "__main__":
    main()
