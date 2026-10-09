"""Build a wildcat project for a fire from a fires/*.json config.

Inputs, all in the fire's UTM CRS:
  perimeter  WFIGS final perimeter (pipeline/recover.py)
  dem        USGS 1/3 arc-second DEM, 10 m, read straight from the TNM S3 tiles by name
             (current, or a pinned version with --dem-version). pfdf's TNM fetcher depends
             on TNM's search index, which listed no Idaho tiles until ~2026-09-24, and cannot
             fetch older versions; it is used here only to cross-check the tile list
             (design.md, "Things that will bite you"). Tiles that do not exist (ocean,
             outside the US) are skipped; empty pixels stop the run only inside the
             perimeter. The RECOVER bundle's DEM is usable but is a different product
             stored as whole metres; at Wapiti it differs from TNM by a median 3.4 m, with
             the share of the fire >= 23 deg within 1%. We use TNM because USGS did.
  evt        LANDFIRE EVT from the RECOVER bundle (masks water and developed land);
             or, with no bundle, from LANDFIRE directly (landfire_evt)
  kf         STATSGO KFFACT via pfdf, as USGS uses; its gaps in the buffered perimeter
             filled from SSURGO (USDA Soil Data Access, pipeline/soils.py), then with the
             fire's median (fill_kf). --kf-fill none leaves them empty, as USGS does;
             --kf-fill median skips SSURGO.
  dnbr       --dnbr usgs            BAER dNBR USGS published with its assessment
                                    (fires/*.json "usgs_inputs", pipeline/usgs_inputs.py)
             --dnbr baer            BAER package (manual download)
             --dnbr s2:<tag>        pipeline/sentinel2_dnbr.py output data/s2_dnbr/<fire>/<tag>
  severity   --severity usgs        BAER soil burn severity USGS published (as above)
             --severity baersbs     the Forest Service BAER map (fires/*.json "baer_sbs",
                                    pipeline/baer_sbs.py)
             --severity baer        BAER soil burn severity from the same package
             --severity estimate    none supplied; wildcat estimates from the dNBR
  --evt landfire  LANDFIRE EVT even when a RECOVER bundle exists (to compare the two)
  --lidar-res N  DEM from the fire's RECOVER 1 m LiDAR layer (fires/*.json "recover_lidar"),
             averaged to N m and kept in its native Albers CRS; see lidar_dem()
  --dem-version YYYYMMDD  use that published DEM version instead of the current one
             (local tiles under data/USGS_raw_DEMS* if present, else TNM historical/).
             To reproduce a past assessment, use the version current at the time:
             TNM revises these tiles, and at Wapiti the 2024-07-01 and 2026-05-19
             versions differ by >1 m on 2-45% of pixels (up to 155 m).

    python pipeline/prepare_fire.py fires/wapiti.json --dnbr baer --severity baer
"""

import glob
import json
import math
import re
import shutil
import subprocess
import sys
import time
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import fiona
import numpy as np
import rasterio
from pfdf.data.usgs import statsgo
from pfdf.projection import BoundingBox
from affine import Affine
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.features import geometry_mask, shapes
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform, unary_union

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import fire_key  # noqa: E402
import soils  # noqa: E402
# wildcat's default configuration.py (as `wildcat initialize` writes it); each project
# gets a copy with the fire-specific changes below
TEMPLATE_CONFIG = ROOT / "pipeline" / "wildcat_config_template.py"
TNM = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{t}/USGS_13_{t}.tif"
# A specific published version; TNM keeps every version under historical/
TNM_VERSION = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/historical/"
               "{t}/USGS_13_{t}_{v}.tif")
# Tiles downloaded by hand (e.g. from Google Drive) are used instead of the network
LOCAL_DEM_GLOB = "data/USGS_raw_DEMS*/**/USGS_13_{t}_{v}.tif"
RES = 10
PAD_M = 5000  # wildcat buffers the perimeter by 3 km
BUFFER_M = 3000  # wildcat's buffer_km in the template configuration
GAP_LIMIT = 0.001  # largest share of the used LiDAR area that may be filled
FILL = -32768
MIN_OVERLAP_PX = 10_000  # pixels with both STATSGO and SSURGO KF needed to rescale SSURGO (1 km2)


def tnm_tiles(lon_min, lat_min, lon_max, lat_max):
    """1-degree TNM tile names covering a WGS84 box (tile nNNwWWW has its NW corner there)."""
    return [f"n{lat:02d}w{lon:03d}"
            for lat in range(math.floor(lat_min) + 1, math.floor(lat_max) + 2)
            for lon in range(math.ceil(-lon_max), math.ceil(-lon_min) + 1)]


def tile_exists(source):
    """Whether a DEM tile exists: a local file, or an S3 object (HEAD). A missing tile is
    normal over the ocean and outside the US; any other failure is raised, not skipped."""
    if not source.startswith("/vsicurl/"):
        return Path(source).exists()
    request = urllib.request.Request(source[len("/vsicurl/"):], method="HEAD")
    try:
        urllib.request.urlopen(request, timeout=60).close()
        return True
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):          # S3 answers 403 for a missing public object
            return False
        raise


DEM_SERVICE = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"
SERVICE_BLOCK = 2000       # export blocks; 4000 x 4000 drew HTTP 500s (limit is 8000)


def service_get(url, tries=5):
    """The 3DEP service answers the odd 500/502/503: retry with backoff."""
    for i in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=600)
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == tries - 1:
                raise
            time.sleep(10 * (i + 1))


def dem_from_service(tiles, crs, transform, width, height):
    """The same 1/3 arc-second tiles from USGS's 3DEP image service, for when TNM's S3 is
    unreachable. The service mosaics every 3DEP resolution (1 m LiDAR included), so the
    export is locked to its 1/3 arc-second catalog items (cell ~10.3 m), named as the TNM
    tiles; their pubdate is the tile version, recorded so a run says what it used. The
    service resamples to our UTM grid with bilinear interpolation, as warp_mosaic does.
    Current versions only: the service keeps no older ones. Returns (dem, {tile: version})."""
    epsg = int(str(crs).split(":")[1])
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    left, top = transform.c, transform.f
    w, s, e, n = to_wgs.transform_bounds(left, top - height * RES, left + width * RES, top,
                                         densify_pts=21)
    with service_get(f"{DEM_SERVICE}/query?" + urllib.parse.urlencode({
            "where": "Category = 1 AND LowPS > 10 AND LowPS < 11", "geometry": f"{w},{s},{e},{n}",
            "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": "OBJECTID,Name,pubdate",
            "returnGeometry": "false", "f": "json"})) as r:
        items = [f["attributes"] for f in json.load(r)["features"]
                 if f["attributes"]["Name"] in tiles]
    if not items:
        raise SystemExit(f"3DEP image service has none of the tiles {tiles}")
    versions = {i["Name"]: str(i["pubdate"]) for i in items}
    rule = json.dumps({"mosaicMethod": "esriMosaicLockRaster",
                       "lockRasterIds": [i["OBJECTID"] for i in items]})
    dem = np.full((height, width), np.nan, dtype="float64")
    for row in range(0, height, SERVICE_BLOCK):
        for col in range(0, width, SERVICE_BLOCK):
            h, w_ = min(SERVICE_BLOCK, height - row), min(SERVICE_BLOCK, width - col)
            x0, y1 = left + col * RES, top - row * RES
            params = {"bbox": f"{x0},{y1 - h * RES},{x0 + w_ * RES},{y1}", "bboxSR": epsg,
                      "imageSR": epsg, "size": f"{w_},{h}", "format": "tiff", "pixelType": "F32",
                      "noData": -9999, "interpolation": "RSP_BilinearInterpolation",
                      "mosaicRule": rule, "f": "image"}
            with service_get(f"{DEM_SERVICE}/exportImage?" + urllib.parse.urlencode(params)) as r:
                data = r.read()
            if not data.startswith((b"II*\x00", b"MM\x00*")):
                raise SystemExit(f"3DEP image service export failed: {data[:200]!r}")
            with rasterio.MemoryFile(data) as mem, mem.open() as src:
                block = src.read(1).astype("float64")
                if src.nodata is not None:
                    block[block == src.nodata] = np.nan
            block[block <= -9999] = np.nan
            dem[row:row + h, col:col + w_] = block
    return dem, versions


def pfdf_tile_check(tiles, wgs):
    """Cross-check our tile list with pfdf's TNM search (current versions only). Advisory:
    TNM's index has lagged the published tiles before (Idaho returned none until about
    2026-09-24), so a difference is reported, never acted on."""
    try:
        from pfdf.data.usgs.tnm import dem as tnm_dem
        listed = {m.group(0) for t in tnm_dem.tiles(wgs)
                  if (m := re.search(r"n\d{2}w\d{3}", t["title"]))}
    except Exception as e:      # the search API is down or changed: the S3 read goes on
        print(f"dem       : pfdf/TNM tile search unavailable ({type(e).__name__}); not cross-checked")
        return
    ours = set(tiles)
    if listed != ours:
        print(f"dem       : tile lists differ; ours {sorted(ours)}, TNM search {sorted(listed)} "
              f"(only in ours: {sorted(ours - listed)}; only in TNM: {sorted(listed - ours)})")


def warp_mosaic(sources, crs, transform, width, height, resampling, dtype="float32"):
    out = np.full((height, width), np.nan, dtype="float64")
    for src_path in sources:
        with rasterio.open(src_path) as src, WarpedVRT(
                src, crs=crs, transform=transform, width=width, height=height,
                resampling=resampling) as vrt:
            data = vrt.read(1).astype("float64")
            if vrt.nodata is not None:
                data[data == vrt.nodata] = np.nan
        out = np.where(np.isnan(out), data, out)
    return out


LANDFIRE_EVT = ("https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF{y}/"
                "LF{y}_EVT_CONUS/ImageServer/exportImage")
LANDFIRE_YEARS = (2016, 2022, 2023, 2024, 2025)    # EVT image services that exist (no 2020)


def landfire_evt(fire, crs, transform, width, height):
    """LANDFIRE EVT for fires without a RECOVER bundle (the layer RECOVER itself repackages;
    same class codes, so wildcat's water 7292 and developed 7296-7300 masks still apply).

    Read from LANDFIRE's public ImageServer rather than the LFPS job API, which requires
    an email address and a queued job. Vintage: the newest EVT not after the fire year,
    i.e. pre-fire vegetation (2016 for fires before 2022). The native 30 m, EPSG:5070
    extract is cached per fire, then warped onto the project grid."""
    import urllib.request
    year = max([y for y in LANDFIRE_YEARS if y <= fire["year"]] or [LANDFIRE_YEARS[0]])
    cache = ROOT / "data" / "recover" / fire_key(fire) / f"evt_landfire_LF{year}.tif"
    if not cache.exists():
        box = BoundingBox(transform.c, transform.f - height * RES,
                          transform.c + width * RES, transform.f, crs=fire["crs"]).reproject(5070)
        left, bottom = math.floor(box.left / 30) * 30, math.floor(box.bottom / 30) * 30
        right, top = math.ceil(box.right / 30) * 30, math.ceil(box.top / 30) * 30
        query = (f"bbox={left},{bottom},{right},{top}&bboxSR=5070&imageSR=5070"
                 f"&size={int((right - left) / 30)},{int((top - bottom) / 30)}"
                 "&format=tiff&pixelType=S16&interpolation=RSP_NearestNeighbor&f=image")
        with urllib.request.urlopen(f"{LANDFIRE_EVT.format(y=year)}?{query}", timeout=300) as r:
            data = r.read()
        if not data.startswith((b"II*\x00", b"MM\x00*")):      # a JSON error, not a TIFF
            raise SystemExit(f"LANDFIRE EVT request failed: {data[:200]!r}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    return warp_mosaic([cache], crs, transform, width, height, Resampling.nearest), year


def write(path, array, crs, transform, nodata, dtype):
    profile = dict(driver="GTiff", height=array.shape[0], width=array.shape[1], count=1,
                   dtype=dtype, crs=crs, transform=transform, nodata=nodata,
                   compress="deflate", tiled=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(dtype), 1)


def fire_grid(fire):
    """10 m grid over the WFIGS perimeter padded by PAD_M, and that perimeter."""
    feature = json.loads((ROOT / "data" / "recover" / fire_key(fire)
                          / "perimeter_wfigs.geojson").read_text())
    geom = shape(feature["geometry"])
    left, bottom, right, top = geom.bounds
    left, top = math.floor((left - PAD_M) / RES) * RES, math.ceil((top + PAD_M) / RES) * RES
    width = math.ceil((right + PAD_M - left) / RES)
    height = math.ceil((top - (bottom - PAD_M)) / RES)
    return geom, from_origin(left, top, RES, RES), width, height


def cache_paths(fire, dem_version=None):
    recover = ROOT / "data" / "recover" / fire_key(fire)
    suffix = f"_{dem_version}" if dem_version else ""
    return {"dem": recover / f"dem_tnm_{fire['crs']}_{RES}m{suffix}.tif",
            "kf": recover / f"kf_statsgo_{fire['crs']}.tif"}


def fetch_cache(kind, fire, dem_version=None):
    """Child-process body: download one network input into its cache, then mark it done.

    Any GDAL network read (pfdf's statsgo.read, or /vsicurl on the TNM or STATSGO S3
    files) leaves the process unable to exit on Windows: the work finishes and every
    file is written and closed, then the process hangs at shutdown indefinitely. It
    ignores GDAL_HTTP_MULTIPLEX=NO, GDAL_HTTP_VERSION=1.1, AWS_NO_SIGN_REQUEST,
    CPL_VSIL_CURL_USE_HEAD=NO, os._exit and even TerminateProcess on itself; only being
    killed by another process ends it. Root cause not found. So the fetch runs here, in
    a child the parent kills once the ".done" marker appears."""
    crs = f"EPSG:{fire['crs']}"
    _, transform, width, height = fire_grid(fire)
    target = cache_paths(fire, dem_version)[kind]
    if kind == "dem":
        geom, _, _, _ = fire_grid(fire)
        box = BoundingBox(transform.c, transform.f - height * RES,
                          transform.c + width * RES, transform.f, crs=fire["crs"])
        wgs = box.reproject(4326)
        tiles = tnm_tiles(wgs.left, wgs.bottom, wgs.right, wgs.top)
        record = {"source": "TNM S3", "version": dem_version or "current"}
        try:
            if os.environ.get("DFH_DEM_SOURCE") == "service":       # to test the fallback
                raise OSError("DFH_DEM_SOURCE=service")
            sources = dem_sources(tiles, dem_version)
            present = [(t, s) for t, s in zip(tiles, sources) if tile_exists(s)]
            if len(present) < len(tiles):
                print(f"dem       : no tile {sorted(set(tiles) - {t for t, _ in present})} "
                      f"(ocean or outside the US); using {[t for t, _ in present]}")
            if not present:
                raise SystemExit(f"no DEM tile exists for {tiles}")
            if not dem_version:
                pfdf_tile_check([t for t, _ in present], wgs)
            dem = warp_mosaic([s for _, s in present], crs, transform, width, height,
                              Resampling.bilinear)
            record["tiles"] = [t for t, _ in present]
        except (OSError, urllib.error.URLError, rasterio.errors.RasterioIOError) as e:
            # TNM's S3 unreachable: the same tiles from USGS's 3DEP image service. Only for
            # the current version; a pinned version exists nowhere else, so that stops.
            if dem_version:
                raise SystemExit(f"TNM S3 unreachable ({e}) and version {dem_version} is "
                                 "only there") from None
            print(f"dem       : TNM S3 unreachable ({e}); using USGS's 3DEP image service")
            dem, versions = dem_from_service(tiles, crs, transform, width, height)
            record = {"source": "3DEP image service", "version": "current",
                      "tiles": versions, "service": DEM_SERVICE}
            print(f"dem       : 3DEP image service, 1/3 arc-second tiles {versions}")
        empty = np.isnan(dem)
        if empty.any():
            # No elevation (ocean, beyond the border) is left as nodata. Inside the perimeter
            # (a WFIGS perimeter that crosses into Mexico or Canada) main() clips the
            # perimeter to the DEM's coverage; with no elevation anywhere inside, stop.
            inside = ~geometry_mask([mapping(geom)], dem.shape, transform)   # geom is in the fire's UTM CRS
            if not (inside & ~empty).any():
                raise SystemExit(f"DEM has no elevation anywhere inside the fire perimeter; "
                                 f"tiles {record.get('tiles')}")
            lost = int((empty & inside).sum())
            print(f"dem       : {int(empty.sum())} pixels have no elevation (no land tile, or "
                  f"outside the US); left as nodata"
                  + (f"; {lost} of them inside the perimeter, which is clipped" if lost else ""))
            dem[empty] = -9999.0
        write(target, dem, crs, transform, -9999.0, "float32")
        # which source and tile versions this cache came from (read by main for sources.json)
        Path(f"{target}.source.json").write_text(json.dumps(record, indent=1))
    else:
        box = BoundingBox(transform.c, transform.f - height * RES,
                          transform.c + width * RES, transform.f, crs=fire["crs"])
        statsgo.read("KFFACT", box, timeout=300).save(target, overwrite=True)
    Path(f"{target}.done").write_text("ok")
    print(f"cached {target.name}", flush=True)


def dem_sources(tiles, version=None):
    """Paths or URLs for the 1/3 arc-second tiles: the current version, or a named one,
    preferring local copies of a named version over the network."""
    if not version:
        return ["/vsicurl/" + TNM.format(t=t) for t in tiles]
    sources = []
    for t in tiles:
        local = glob.glob(str(ROOT / LOCAL_DEM_GLOB.format(t=t, v=version)), recursive=True)
        sources.append(local[0] if local else "/vsicurl/" + TNM_VERSION.format(t=t, v=version))
    return sources


def ensure_cache(kind, fire, config_path, timeout_s=1800, dem_version=None):
    """Run fetch_cache in a child process unless the cache is complete; kill the child
    as soon as it marks the cache done (see fetch_cache for why)."""
    target = cache_paths(fire, dem_version)[kind]
    done = Path(f"{target}.done")
    if target.exists() and done.exists():
        return target
    done.unlink(missing_ok=True)
    command = [sys.executable, __file__, "--fetch-cache", kind, str(config_path)]
    if dem_version:
        command.append(dem_version)
    child = subprocess.Popen(command)
    start = time.monotonic()
    while not done.exists():
        if child.poll() is not None and not done.exists():
            raise SystemExit(f"fetching {kind} failed (exit {child.returncode})")
        if time.monotonic() - start > timeout_s:
            kill_tree(child)
            raise SystemExit(f"fetching {kind} timed out after {timeout_s} s")
        time.sleep(2)
    if child.poll() is None:
        kill_tree(child)
    return target


def kill_tree(process):
    """Kill a child and its descendants. The venv's python.exe is a launcher that starts
    the real interpreter as a second process, so killing the child alone leaves the
    hung interpreter running."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True)
    else:
        process.kill()
    process.wait()


def lidar_dem(fire, res):
    """DEM at `res` m from the fire's RECOVER 1 m LiDAR layer, kept in its own CRS.

    The layer (fire["recover_lidar"]) is a 1 m float32 BigTIFF in USGS Albers
    (ESRI:102039) covering ~115 x 102 km. We read only the window over the padded
    perimeter; for res > 1 the 1 m cells are averaged, as M3 did for Grizzly. The DEM is
    left in Albers rather than reprojected to UTM: resampling 2 billion pixels would cost
    time and blur the data it exists to provide, and wildcat reprojects the coarse inputs
    (dNBR, severity, KF, EVT, perimeter) onto the DEM anyway. Albers scale error at this
    latitude is well under 1%.

    The layer's nodata is +3.4e38. The Grizzly LiDAR's -3.4e38 once made rasterio.merge
    return an all-zero mosaic silently, so fill is mapped to an explicit -9999 here and
    the elevation range is checked before anything downstream uses it.

    Local file only (no network read), so no child process is needed."""
    source = ROOT / fire["recover_lidar"]
    target = ROOT / "data" / "recover" / fire_key(fire) / f"dem_lidar_albers_{res:g}m.tif"
    if target.exists():
        return target
    geom, *_ = fire_grid(fire)
    with rasterio.open(source) as src:
        to_src = Transformer.from_crs(f"EPSG:{fire['crs']}", src.crs, always_xy=True)
        left, bottom, right, top = to_src.transform_bounds(
            *geom.buffer(PAD_M).bounds, densify_pts=21)
        # Snap the window to whole output cells on the source grid (1 m for RECOVER)
        factor = round(res / abs(src.res[0]))
        if factor < 1 or abs(factor * abs(src.res[0]) - res) > 1e-6:
            raise SystemExit(f"res {res} m is not a whole multiple of the source {src.res[0]} m")
        window = from_bounds(left, bottom, right, top, src.transform)
        window = window.round_offsets().round_lengths()
        height = int(window.height) // factor * factor
        width = int(window.width) // factor * factor
        window = Window(window.col_off, window.row_off, width, height)
        data = src.read(1, window=window, out_shape=(height // factor, width // factor),
                        resampling=Resampling.average)
        nodata = src.nodata
        transform = src.window_transform(window) * Affine.scale(factor, factor)
        crs = src.crs
    fill = ~np.isfinite(data) | (np.abs(data) >= 1e38)
    if nodata is not None:
        fill |= data == nodata
    # Gaps are only fatal where wildcat reads the DEM: the perimeter plus its 3 km buffer
    # (plus a 250 m margin). The window is a PAD_M (5 km) bounding box, and in Albers its
    # corners reach further still, so LiDAR voids out there are expected and harmless:
    # they become nodata. The first Wapiti 10 m run found 94,907 empty 10 m cells in the
    # window, which the old any-gap check rejected outright.
    used = shapely_transform(to_src.transform, geom.buffer(BUFFER_M + 250))
    inside = ~geometry_mask([mapping(used)], data.shape, transform)
    # Small voids inside (LiDAR misses water surfaces and odd spots; the Wapiti 1 m run
    # had 5,202 empty 1 m cells = 0.001% of the used area) are filled from the same
    # LiDAR averaged over 10x coarser cells, which GDAL computes ignoring nodata. Voids
    # over GAP_LIMIT of the used area, or with no valid coarse value, still stop the run.
    gaps = fill & inside
    if gaps.any() and gaps.sum() <= GAP_LIMIT * inside.sum():
        with rasterio.open(source) as src:
            coarse = src.read(1, window=window, resampling=Resampling.average,
                              out_shape=(-(-data.shape[0] // 10), -(-data.shape[1] // 10)))
        rows, cols = np.nonzero(gaps)
        patch = coarse[rows // 10, cols // 10].astype("float64")
        ok = np.isfinite(patch) & (np.abs(patch) < 1e38)
        if nodata is not None:
            ok &= patch != nodata
        data[rows[ok], cols[ok]] = patch[ok]
        fill[rows[ok], cols[ok]] = False
        print(f"            filled {int(ok.sum())} empty cells inside the buffered perimeter "
              f"({ok.sum() * res * res:.0f} m2) from the same LiDAR at {10 * res:g} m")
    if (fill & inside).any():
        raise SystemExit(f"LiDAR has {int((fill & inside).sum())} empty cells within the "
                         f"perimeter + {BUFFER_M / 1000:g} km buffer at {res} m "
                         f"({(fill & inside).sum() * res * res / 1e6:.2f} km2)")
    if fill.any():
        print(f"            {int(fill.sum())} empty cells ({fill.sum() * res * res / 1e6:.1f} km2) "
              f"outside the buffered perimeter set to nodata")
    data = np.where(fill, -9999.0, data)
    valid = data[~fill]
    if not (0 < valid.min() and valid.max() < 9000):
        raise SystemExit(f"implausible LiDAR elevations {valid.min()}-{valid.max()}")
    profile = dict(driver="GTiff", height=data.shape[0], width=data.shape[1], count=1,
                   dtype="float32", crs=crs, transform=transform, nodata=-9999.0,
                   compress="deflate", tiled=True, BIGTIFF="IF_SAFER")
    with rasterio.open(target, "w", **profile) as dst:
        dst.write(data.astype("float32"), 1)
    return target


def fill_kf(fire, statsgo_path, geom, crs, transform, width, height, mode, has_dem=None):
    """KF on the fire grid: STATSGO, its gaps filled from SSURGO and then the fire's median.

    A gap is a pixel STATSGO leaves empty or at 0 or below (its COG stores missing map units
    as 0, sometimes -0.1); wildcat drops those (constrain_kf) and the streams draining them
    get no M1 threshold. Only gaps where wildcat reads KF (the perimeter plus its 3 km
    buffer) and has elevation (has_dem: across a border or over the ocean there is neither
    US soil nor DEM, and wildcat models nothing there) are filled. mode: "ssurgo" (SSURGO, then median for what is left), "median"
    (median only). USGS does not fill KF in its own assessments (Big Bear 2025: 16 segments
    without a threshold, Wapiti 2024: 517), so the fill is recorded in sources.json and
    `--kf-fill none` keeps STATSGO as is. Returns the array and a sources.json note."""
    kf = warp_mosaic([statsgo_path], crs, transform, width, height, Resampling.nearest)
    used = ~geometry_mask([mapping(geom.buffer(BUFFER_M + 250))], kf.shape, transform)
    if has_dem is not None:
        used &= has_dem
    gaps = used & ~(kf > 0)
    total = int(used.sum())
    if not gaps.any():
        print("kf        : STATSGO KFFACT, no gaps")
        return kf, "STATSGO soils"
    print(f"kf        : STATSGO KFFACT has no value for {100 * gaps.sum() / total:.1f}% of the "
          f"buffered perimeter")
    notes = []
    if mode == "ssurgo":
        cache = ROOT / "data" / "recover" / fire_key(fire) / f"kf_ssurgo_{fire['crs']}.tif"
        try:
            if cache.exists():
                with rasterio.open(cache) as src:
                    ssurgo = src.read(1)
            else:
                ssurgo, n_poly, n_units = soils.ssurgo_kf(crs, transform, kf.shape, gaps)
                write(cache, ssurgo, crs, transform, np.nan, "float32")
                print(f"            SSURGO: {n_poly} map-unit polygons, {n_units} map units with KF")
            # The two surveys are on different scales: where both have KF, SSURGO's is lower
            # (Wapiti: median ratio 0.74 over 8.8 M pixels). Raw SSURGO in the gaps would lower
            # S there and raise thresholds, so it is rescaled to STATSGO's scale, the one M1
            # was fit with, by this fire's own median ratio.
            both = (kf > 0) & (ssurgo > 0)
            scale = float(np.median(kf[both] / ssurgo[both])) if both.sum() >= MIN_OVERLAP_PX else 1.0
            take = gaps & (ssurgo > 0)
            kf[take] = ssurgo[take] * scale
            gaps &= ~take
            if take.any():
                notes.append(f"SSURGO x{scale:.2f} {100 * take.sum() / total:.1f}%")
            print(f"            filled {100 * take.sum() / total:.1f}% from SSURGO, rescaled x{scale:.3f} "
                  f"to STATSGO ({int(both.sum())} pixels with both)")
        except Exception as e:          # SDA down or refused: carry on with the median
            print(f"            SSURGO unavailable ({e}); median only")
    if gaps.any():
        median = float(np.median(kf[used & (kf > 0)]))
        kf[gaps] = median
        notes.append(f"fire median {median:.2f} {100 * gaps.sum() / total:.1f}%")
        print(f"            filled {100 * gaps.sum() / total:.1f}% with the fire's median KF {median:.3f}")
    return kf, "STATSGO soils; gaps filled: " + ", ".join(notes) + " of the area used"


def baer_file(fire, suffix):
    return Path(glob.glob(str(ROOT / fire["baer_dir"] / "baer_*" / f"*_{suffix}"))[0])


def main():
    args = sys.argv[1:]
    fire = json.loads(Path(args[0]).read_text())
    dnbr_source = args[args.index("--dnbr") + 1]
    severity_source = args[args.index("--severity") + 1]
    dem_version = args[args.index("--dem-version") + 1] if "--dem-version" in args else None
    lidar_res = int(args[args.index("--lidar-res") + 1]) if "--lidar-res" in args else None
    evt_source = args[args.index("--evt") + 1] if "--evt" in args else "auto"
    kf_mode = args[args.index("--kf-fill") + 1] if "--kf-fill" in args else "ssurgo"
    if kf_mode not in ("ssurgo", "median", "none"):
        raise SystemExit("--kf-fill ssurgo, median or none")
    key = fire_key(fire)
    recover = ROOT / "data" / "recover" / key
    crs = f"EPSG:{fire['crs']}"
    label = dnbr_source.replace(":", "_") + "_" + severity_source
    if dem_version:
        label += f"_dem{dem_version}"
    if lidar_res:
        label += f"_lidar{lidar_res}m"
    if evt_source == "landfire":
        label += "_evtlf"
    project = ROOT / "data" / f"{key}_{label}"
    if project.exists():
        shutil.rmtree(project)
    inputs = project / "inputs"
    inputs.mkdir(parents=True)

    # Perimeter (WFIGS)
    feature = json.loads((recover / "perimeter_wfigs.geojson").read_text())
    geom = shape(feature["geometry"])
    schema = {"geometry": geom.geom_type, "properties": {"name": "str:40"}}
    with fiona.open(inputs / "perimeter.shp", "w", driver="ESRI Shapefile", crs=crs,
                    schema=schema) as out:
        out.write({"geometry": mapping(geom), "properties": {"name": fire["name"]}})
    print(f"perimeter : WFIGS, {geom.area / 1e6:.1f} km2")

    # Common 10 m grid over the padded perimeter
    _, transform, width, height = fire_grid(fire)

    # DEM (TNM 1/3 arc-second) and KF (STATSGO) come over the network, so each is fetched
    # once per fire into a cache by a child process (see fetch_cache) and reused.
    if lidar_res:
        dem = lidar_dem(fire, lidar_res)
        shutil.copy(dem, inputs / "dem.tif")
        with rasterio.open(dem) as src:
            print(f"dem       : RECOVER 1 m LiDAR at {lidar_res} m, {src.width} x {src.height} "
                  f"({src.width * src.height / 1e6:.0f} Mpx), {src.crs}")
    else:
        dem_cache = ensure_cache("dem", fire, args[0], dem_version=dem_version)
        with rasterio.open(dem_cache) as src:
            assert src.transform == transform and src.shape == (height, width)
            stats = src.read(1)
            stats = stats[stats != src.nodata]
        shutil.copy(dem_cache, inputs / "dem.tif")
        origin = Path(f"{dem_cache}.source.json")
        origin = json.loads(origin.read_text())["source"] if origin.exists() else "TNM S3"
        print(f"dem       : 1/3 arc-second from {origin}, version {dem_version or 'current'}, "
              f"{width} x {height} at {RES} m, "
              f"{stats.min():.0f}-{stats.max():.0f} m")

    # EVT (LANDFIRE): from the RECOVER bundle when the fire has one, otherwise straight
    # from LANDFIRE. Last resort, none: wildcat then does not mask water and developed
    # land, which matters little on a forest fire.
    config = TEMPLATE_CONFIG.read_text()
    sources = {"perimeter": "WFIGS final perimeter",
               "dem": f"RECOVER 1 m LiDAR averaged to {lidar_res} m" if lidar_res
               else "USGS 10 m DEM", "kf": "STATSGO soils"}
    # A WFIGS perimeter that crosses into Mexico or Canada (Border 6 2026) has no US elevation
    # on the far side: clip the perimeter to where the DEM has data, so wildcat models the
    # US part, and record what was removed. A perimeter fully covered is left untouched.
    has_dem = None
    if not lidar_res:
        with rasterio.open(dem_cache) as src:
            has_dem = src.read(1) != src.nodata
        inside = ~geometry_mask([mapping(geom)], has_dem.shape, transform)
        if (inside & ~has_dem).any():
            covered = unary_union([shape(s) for s, v in shapes(
                has_dem.astype("uint8"), mask=has_dem, transform=transform) if v == 1])
            clipped = geom.intersection(covered)
            removed = (geom.area - clipped.area) / 1e6
            geom = clipped
            with fiona.open(inputs / "perimeter.shp", "w", driver="ESRI Shapefile", crs=crs,
                            schema={"geometry": "MultiPolygon" if geom.geom_type == "MultiPolygon"
                                    else "Polygon", "properties": {"name": "str:40"}}) as out:
                out.write({"geometry": mapping(geom), "properties": {"name": fire["name"]}})
            sources["perimeter"] = (f"WFIGS final perimeter, clipped to US elevation coverage: "
                                    f"{removed:.1f} of {removed + geom.area / 1e6:.1f} km2 removed")
            print(f"perimeter : clipped to the DEM's coverage, {removed:.1f} km2 removed "
                  f"(outside the US); {geom.area / 1e6:.1f} km2 modeled")
    record = Path(f"{dem_cache}.source.json") if not lidar_res else None
    if record and record.exists() and json.loads(record.read_text())["source"] != "TNM S3":
        r = json.loads(record.read_text())
        sources["dem"] = ("USGS 10 m DEM (3DEP image service, tiles "
                          + ", ".join(f"{t} {v}" for t, v in r["tiles"].items()) + ")")
    evt_path = recover / "EVT_ExistingVegetationType.tif"
    if evt_path.exists() and evt_source != "landfire":
        evt = warp_mosaic([evt_path], crs, transform, width, height, Resampling.nearest)
        write(inputs / "evt.tif", np.nan_to_num(evt, nan=FILL), crs, transform, FILL, "int16")
        sources["evt"] = "RECOVER LANDFIRE EVT"
        print(f"evt       : RECOVER LANDFIRE EVT")
    else:
        try:
            evt, year = landfire_evt(fire, crs, transform, width, height)
            write(inputs / "evt.tif", np.nan_to_num(evt, nan=FILL), crs, transform, FILL, "int16")
            sources["evt"] = f"LANDFIRE LF{year} EVT"
            masked = np.isin(evt, [7292, 7296, 7297, 7298, 7299, 7300]).mean()
            print(f"evt       : LANDFIRE LF{year} EVT ({'forced' if evt_source == 'landfire' else 'no RECOVER EVT'}); "
                  f"{100 * masked:.2f}% water or developed")
        except (OSError, SystemExit) as e:     # network or service failure: run unmasked
            config = config.replace('evt = r"evt"', "evt = None")
            print(f"evt       : none ({e}); water and developed land not masked")

    statsgo_kf = ensure_cache("kf", fire, args[0])
    if kf_mode == "none":
        shutil.copy(statsgo_kf, inputs / "kf.tif")
        print("kf        : STATSGO KFFACT, gaps left empty (--kf-fill none)")
    else:
        # has_dem (above) limits the fill to pixels with elevation; None for the LiDAR DEM,
        # which is on its own grid, and then the whole buffer is filled
        kf, sources["kf"] = fill_kf(fire, statsgo_kf, geom, crs, transform, width, height,
                                    kf_mode, has_dem)
        write(inputs / "kf.tif", np.nan_to_num(kf, nan=-9999.0), crs, transform, -9999.0, "float32")

    # dNBR
    if dnbr_source == "baer":
        with rasterio.open(baer_file(fire, "dnbr.tif")) as d, \
             rasterio.open(baer_file(fire, "rdnbr.tif")) as r:
            data, fill = d.read(1).astype("int16"), r.read(1) == r.nodata
            profile = d.profile
        # BAER declares nodata = 0, which is also a real dNBR value; the true fill is
        # rdnbr's nodata (and -32768 in the dnbr itself for this package).
        fill |= data == FILL
        data[fill] = FILL
        profile.update(nodata=FILL)
        with rasterio.open(inputs / "dnbr.tif", "w", **profile) as dst:
            dst.write(data, 1)
        print(f"dnbr      : BAER {baer_file(fire, 'dnbr.tif').name}, {int(fill.sum())} fill px")
        sources["dnbr"] = "BAER dNBR"
    elif dnbr_source == "usgs":
        # The BAER dNBR USGS published with its assessment (pipeline/usgs_inputs.py);
        # wildcat reprojects it onto the DEM grid
        u = fire["usgs_inputs"]
        shutil.copy(ROOT / u["dnbr"], inputs / "dnbr.tif")
        print(f"dnbr      : USGS-published {u['sources'].get('dnbr') or 'BAER'} dNBR ({u['code']})")
        sources["dnbr"] = f"{u['sources'].get('dnbr') or 'BAER'} dNBR, published with USGS assessment {u['code']}"
    elif dnbr_source.startswith("s2:"):
        src = ROOT / "data" / "s2_dnbr" / key / dnbr_source[3:] / "dnbr.tif"
        shutil.copy(src, inputs / "dnbr.tif")
        # which sensor and method sentinel2_dnbr.py ended up using (its fallbacks)
        sel = src.parent / "selection.json"
        sel = json.loads(sel.read_text()) if sel.exists() else {}
        sensor, method = sel.get("sensor", "Sentinel-2"), sel.get("method", "single date")
        print(f"dnbr      : {sensor} {src.parent.name}, {method}")
        sources["dnbr"] = f"{sensor} dNBR" + ("" if method in ("single date", "forced dates")
                                              else f" ({method})")
    else:
        raise SystemExit(f"unknown --dnbr {dnbr_source}")

    # Severity
    if severity_source == "baer":
        with rasterio.open(baer_file(fire, "sbs.tif")) as s:
            sbs, profile = s.read(1), s.profile
        # Declared nodata is 0 but the fill is 255
        sbs = np.where(np.isin(sbs, [1, 2, 3, 4]), sbs, 0).astype("uint8")
        profile.update(nodata=0)
        with rasterio.open(inputs / "severity.tif", "w", **profile) as dst:
            dst.write(sbs, 1)
        counts = {int(k): int(v) for k, v in zip(*np.unique(sbs[sbs > 0], return_counts=True))}
        print(f"severity  : BAER SBS, BARC4 counts {counts}")
        sources["severity"] = "BAER soil burn severity"
    elif severity_source == "usgs":
        u = fire["usgs_inputs"]
        with rasterio.open(ROOT / u["severity"]) as s:
            sbs, profile = s.read(1), s.profile
        # BARC4 classes 1-4; anything else (0, the 255 fill) is no data, as for BAER above
        sbs = np.where(np.isin(sbs, [1, 2, 3, 4]), sbs, 0).astype("uint8")
        profile.update(nodata=0)
        with rasterio.open(inputs / "severity.tif", "w", **profile) as dst:
            dst.write(sbs, 1)
        counts = {int(k): int(v) for k, v in zip(*np.unique(sbs[sbs > 0], return_counts=True))}
        print(f"severity  : USGS-published {u['sources'].get('severity') or 'BAER'} "
              f"soil burn severity ({u['code']}), BARC4 counts {counts}")
        # the source USGS names: "BAER Team" (field-checked soil burn severity) or
        # "EROS BARC Package" (the satellite classification before any field check)
        sources["severity"] = (f"{u['sources'].get('severity') or 'BAER'} burn severity, "
                               f"published with USGS assessment {u['code']}")
    elif severity_source == "baersbs":
        # The Forest Service BAER team's map, from its public image service (baer_sbs.py)
        b = fire["baer_sbs"]
        with rasterio.open(ROOT / b["path"]) as s:
            sbs, profile = s.read(1), s.profile
        sbs = np.where(np.isin(sbs, [1, 2, 3, 4]), sbs, 0).astype("uint8")
        profile.update(nodata=0)
        with rasterio.open(inputs / "severity.tif", "w", **profile) as dst:
            dst.write(sbs, 1)
        counts = {int(k): int(v) for k, v in zip(*np.unique(sbs[sbs > 0], return_counts=True))}
        print(f"severity  : Forest Service BAER SBS {b['name']}, BARC4 counts {counts}")
        sources["severity"] = f"BAER Team soil burn severity (Forest Service BAER map {b['name']})"
    elif severity_source == "estimate":
        config = config.replace('severity = r"severity"', "severity = None")
        config += "\nestimate_severity = True\nseverity_thresholds = [125, 250, 500]\n"
        print("severity  : estimated by wildcat from the dNBR")
        sources["severity"] = "severity estimated from dNBR"
    else:
        raise SystemExit(f"unknown --severity {severity_source}")
    if lidar_res:
        # Lift wildcat's 6.5-11 m guardrail, and scale the
        # pixel-based confinement neighborhood to hold its 40 m physical distance.
        neighborhood = max(1, round(40 / lidar_res))
        config += "\n".join(["", f"# --- LiDAR DEM at {lidar_res} m ---",
                             'resolution_check = "none"',
                             f"confinement_neighborhood = {neighborhood}", ""])
    (project / "configuration.py").write_text(config)
    (project / "sources.json").write_text(json.dumps(sources, indent=1))   # read by map/
    print(f"project   : {project}")


if __name__ == "__main__":
    if sys.argv[1] == "--fetch-cache":
        fetch_cache(sys.argv[2], json.loads(Path(sys.argv[3]).read_text()),
                    sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        main()
