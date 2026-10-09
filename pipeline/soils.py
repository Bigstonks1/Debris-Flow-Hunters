"""SSURGO soil erodibility (KF) from USDA's Soil Data Access, to fill gaps in STATSGO.

STATSGO is the primary KF source (the USGS M1 model was fit with it), but it has no KF for
some map units (rock outcrop, water, some forest units), and a stream draining such ground
gets no M1 threshold (Wapiti: 528 segments). SSURGO, the detailed county-level survey,
often has a value there. Soil Data Access is free, public and needs no login or key:

    POST https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest
    {"format": "JSON+COLUMNNAME", "query": "<T-SQL>"}

Two queries per area: the map-unit polygons clipped to the area (mupolygon), then KF per map
unit. A map unit's KF is the component-percent weighted mean of `kffact` (the rock-free
erodibility factor, as STATSGO's KFFACT) on each component's surface horizon (hzdept_r = 0),
over the components that have one; rock outcrop and water components have none and are left
out. Map units with no such component stay empty, and the caller fills what is left with the
fire's median KF. A failed query is reported, never fatal: STATSGO alone still runs.

SDA returns `{}` (not an error) for a query that fails on the server, and rejects DECLARE;
kffact is stored as text, so it is cast to float.
"""

import json
import math
import urllib.request

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from shapely import wkt
from shapely.geometry import box, mapping
from shapely.ops import transform as shapely_transform

SDA = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
TILE_DEG = 0.2          # query tiles; a 0.35 x 0.25 deg area returned 6.6 MB of polygons in 3 s
MUKEYS_PER_QUERY = 400


def sda(query, timeout=180):
    body = json.dumps({"format": "JSON+COLUMNNAME", "query": query}).encode()
    req = urllib.request.Request(SDA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", errors="replace")
    if text.lstrip().startswith("<"):
        raise RuntimeError(f"Soil Data Access refused the query: {text[:300]}")
    table = json.loads(text).get("Table")
    if table is None:
        return []           # no rows (or a query SDA could not run)
    head = table[0]
    return [dict(zip(head, row)) for row in table[1:]]


def polygons(left, bottom, right, top):
    """SSURGO map-unit polygons (mukey, shapely geometry in WGS 84) clipped to a box."""
    out = []
    nx, ny = math.ceil((right - left) / TILE_DEG), math.ceil((top - bottom) / TILE_DEG)
    for i in range(nx):
        for j in range(ny):
            l, b = left + i * TILE_DEG, bottom + j * TILE_DEG
            r, t = min(right, l + TILE_DEG), min(top, b + TILE_DEG)
            aoi = f"geometry::STGeomFromText('{box(l, b, r, t).wkt}', 4326)"
            rows = sda(f"SELECT p.mukey, p.mupolygongeo.STIntersection({aoi}).STAsText() AS wkt "
                       f"FROM mupolygon p WHERE p.mupolygongeo.STIntersects({aoi}) = 1")
            out += [(r_["mukey"], wkt.loads(r_["wkt"])) for r_ in rows if r_["wkt"]]
    return out


def kf_by_mukey(mukeys):
    """Component-weighted surface-horizon kffact per map unit (only units that have one)."""
    kf = {}
    mukeys = sorted(set(mukeys))
    for i in range(0, len(mukeys), MUKEYS_PER_QUERY):
        chunk = ",".join(f"'{int(m)}'" for m in mukeys[i:i + MUKEYS_PER_QUERY])
        rows = sda(
            "SELECT c.mukey, SUM(c.comppct_r * CAST(h.kffact AS float)) / "
            "NULLIF(SUM(CASE WHEN h.kffact IS NOT NULL THEN c.comppct_r END), 0) AS kf "
            "FROM component c JOIN chorizon h ON h.cokey = c.cokey AND h.hzdept_r = 0 "
            f"WHERE c.mukey IN ({chunk}) GROUP BY c.mukey")
        kf.update({r["mukey"]: float(r["kf"]) for r in rows if r["kf"] is not None})
    return kf


def ssurgo_kf(crs, transform, shape, wanted):
    """KF from SSURGO on the fire grid (float32, NaN where SSURGO has none), fetched only
    over the bounding box of `wanted` (a boolean mask of the pixels to fill)."""
    rows, cols = np.nonzero(wanted)
    x0, y0 = transform * (cols.min(), rows.max() + 1)
    x1, y1 = transform * (cols.max() + 1, rows.min())
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    left, bottom, right, top = to_wgs.transform_bounds(x0, y0, x1, y1, densify_pts=21)
    polys = polygons(left, bottom, right, top)
    values = kf_by_mukey([m for m, _ in polys])
    to_grid = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    shapes = [(mapping(shapely_transform(to_grid, g)), values[m]) for m, g in polys if m in values]
    out = np.full(shape, np.nan, dtype="float32")
    if shapes:
        out = rasterize(shapes, out_shape=shape, transform=transform, fill=np.nan,
                        dtype="float32")
    return out, len(polys), len(values)
