"""Build a wildcat project for a fire from a fires/*.json config.

Inputs, all in the fire's UTM CRS:
  perimeter  WFIGS final perimeter (pipeline/recover.py)
  dem        USGS 1/3 arc-second DEM, 10 m, read straight from the TNM S3 tiles.
             pfdf's TNM fetcher returns nothing for Idaho (design.md, "Things that will
             bite you"). The RECOVER bundle's DEM is usable but is a different product
             stored as whole metres; at Wapiti it differs from TNM by a median 3.4 m, with
             the share of the fire >= 23 deg within 1%. We use TNM because USGS did.
  evt        LANDFIRE EVT from the RECOVER bundle (masks water and developed land);
             or, with no bundle, from LANDFIRE directly (landfire_evt)
  kf         STATSGO KFFACT via pfdf, as for Grizzly
  dnbr       --dnbr usgs            BAER dNBR USGS published with its assessment
                                    (fires/*.json "usgs_inputs", pipeline/usgs_inputs.py)
             --dnbr baer            BAER package (manual download)
             --dnbr s2:<tag>        pipeline/sentinel2_dnbr.py output data/s2_dnbr/<fire>/<tag>
  severity   --severity usgs        BAER soil burn severity USGS published (as above)
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
import shutil
import subprocess
import sys
import time
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
from rasterio.features import geometry_mask
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import fire_key  # noqa: E402
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


def tnm_tiles(lon_min, lat_min, lon_max, lat_max):
    """1-degree TNM tile names covering a WGS84 box (tile nNNwWWW has its NW corner there)."""
    return [f"n{lat:02d}w{lon:03d}"
            for lat in range(math.floor(lat_min) + 1, math.floor(lat_max) + 2)
            for lon in range(math.ceil(-lon_max), math.ceil(-lon_min) + 1)]


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
        box = BoundingBox(transform.c, transform.f - height * RES,
                          transform.c + width * RES, transform.f, crs=fire["crs"])
        wgs = box.reproject(4326)
        tiles = tnm_tiles(wgs.left, wgs.bottom, wgs.right, wgs.top)
        dem = warp_mosaic(dem_sources(tiles, dem_version), crs, transform,
                          width, height, Resampling.bilinear)
        if np.isnan(dem).any():
            raise SystemExit(f"DEM has {int(np.isnan(dem).sum())} empty pixels; tiles {tiles}")
        write(target, dem, crs, transform, -9999.0, "float32")
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
        shutil.copy(dem_cache, inputs / "dem.tif")
        print(f"dem       : TNM 1/3 arc-second, version {dem_version or 'current'}, "
              f"{width} x {height} at {RES} m, "
              f"{stats.min():.0f}-{stats.max():.0f} m")

    # EVT (LANDFIRE): from the RECOVER bundle when the fire has one, otherwise straight
    # from LANDFIRE. Last resort, none: wildcat then does not mask water and developed
    # land, which matters little on a forest fire.
    config = TEMPLATE_CONFIG.read_text()
    sources = {"perimeter": "WFIGS final perimeter",
               "dem": f"RECOVER 1 m LiDAR averaged to {lidar_res} m" if lidar_res
               else "USGS 10 m DEM", "kf": "STATSGO soils"}
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
            print(f"evt       : LANDFIRE LF{year} EVT ({'forced' if evt_source == 'landfire' else 'no RECOVER bundle'}); "
                  f"{100 * masked:.2f}% water or developed")
        except (OSError, SystemExit) as e:     # network or service failure: run unmasked
            config = config.replace('evt = r"evt"', "evt = None")
            print(f"evt       : none ({e}); water and developed land not masked")

    shutil.copy(ensure_cache("kf", fire, args[0]), inputs / "kf.tif")
    print(f"kf        : STATSGO KFFACT")

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
        print(f"dnbr      : Sentinel-2 {src.parent.name}")
        sources["dnbr"] = "Sentinel-2 dNBR"
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
