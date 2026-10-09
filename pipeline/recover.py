"""Seam 1: fetch a fire's perimeter and RECOVER inputs, starting from its name and year.

  1. WFIGS interagency perimeters: look up the incident, take its final perimeter and
     the two fields that name the RECOVER bundle (POOProtectingUnit, LocalIncidentIdentifier).
  2. RECOVER archive: read only the listed members out of the bundle zip with HTTP range
     requests. Bundles are large (Wapiti: 852 MB) and mostly NDVI and slope products we
     do not use; the DEM and EVT we need are ~50 MB.

Perimeter fallbacks, when WFIGS cannot be reached (network or service error; a WFIGS answer
that fails the checks below still stops the run):
  a. the perimeter saved by this fire's last run (data/recover/<key>/perimeter_wfigs.geojson),
     which is WFIGS's own polygon as of that run;
  b. the latest daily perimeter in the fire's RECOVER bundle (layer Fire_<year>_<unit>_<id>
     of its geodatabase), if it is the final one: its acreage within RECOVER_ACRES of the
     acreage WFIGS last reported (recorded in the config as wfigs_acres on every run that
     reaches WFIGS), or, for a config without that record, dated no more than
     RECOVER_MAX_DAYS before containment. RECOVER copies WFIGS's polygons: on 18 of 19 fires
     checked (2022-2025) the latest snapshot was WFIGS's final perimeter (IoU 0.94-1.00).
     Rush 2025's package stopped 120 days before containment, at 77% of the final area; the
     acreage check (and the date check) reject it. A date check alone is not enough: Wood
     2024's final perimeter is dated 41 days before its declared containment.
The source is written into the perimeter's properties (_source), and from there into the
project's sources.json. Dates are not refreshed while WFIGS is unreachable.

The RECOVER bundle does NOT contain a dNBR raster, only a .lyrx pointer to the
Preliminary_Fire_Severity_dNBR image service, which now requires a token (checked
2026-09-21; only the NDVI services are anonymous). dNBR therefore comes from BAER or from
Sentinel-2 (sentinel2_dnbr.py).

    python pipeline/recover.py fires/wapiti.json
"""

import io
import json
import sys
import urllib.parse
import urllib.request
import zipfile
from functools import reduce
from pathlib import Path

from datetime import date
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import transform as shapely_transform

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import fire_key, wfigs_available, wfigs_dates  # noqa: E402
WFIGS = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
         "WFIGS_Interagency_Perimeters/FeatureServer/0/query")
BUNDLE = ("https://giscenter-sl.isu.edu/aoc/aoc_research/recover2/archive/"
          "{year}/Fire_{year}_{unit}_{local_id}.zip")
GIS_TOLERANCE = 0.01     # assembled area vs poly_GISAcres (the polygon's own computed area)
SIZE_TOLERANCE = 0.10    # vs attr_IncidentSize, a reported figure, when GIS acres are stale
RECOVER_ACRES = 0.05     # a RECOVER snapshot within 5% of WFIGS's last acreage is the final one
RECOVER_MAX_DAYS = 30    # without a recorded acreage: the snapshot may predate containment by
                         # this much (Happy Jack 2024: 26 days; but Wood 2024: 41)


class RangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests, so zipfile can read a remote
    archive's central directory and individual members without downloading all of it."""

    def __init__(self, url):
        self.url, self.pos = url, 0
        head = urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60)
        self.size = int(head.headers["Content-Length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def readinto(self, buffer):
        if self.pos >= self.size:
            return 0
        end = min(self.pos + len(buffer), self.size) - 1
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        data = urllib.request.urlopen(request, timeout=120).read()
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)


def wfigs(name, year, crs, unit=None, local_id=None, uid=None):
    """Final WFIGS perimeter (GeoJSON Feature in `crs`) and attributes for an incident.

    Do not request f=geojson: for Wapiti it returns 271 polygons in which the 65 unburned
    islands come back as extra shells nested inside the outer boundary (invalid,
    "nested shells"), so the area reads 641 km2 instead of 522. The Esri JSON rings are
    assembled here with the even-odd rule (XOR of all rings), which also handles burned
    islands inside unburned holes; it gives 522.2 km2 against WFIGS's own 522.3."""
    wfigs_available()
    name = name.replace("'", "''")
    where = (f"poly_IncidentName = '{name}' AND attr_FireDiscoveryDateTime >= "
             f"DATE '{year}-01-01' AND attr_FireDiscoveryDateTime < DATE '{year + 1}-01-01'")
    if uid:     # the Unique Fire Identifier names exactly one incident (see fires.py)
        where = f"attr_UniqueFireIdentifier = '{uid}'"
    elif unit and local_id:
        where += f" AND attr_POOProtectingUnit = '{unit}' AND attr_LocalIncidentIdentifier = '{local_id}'"
    params = {
        "where": where,
        "outFields": "poly_IncidentName,poly_GISAcres,attr_IncidentSize,attr_FireDiscoveryDateTime,"
                     "attr_ContainmentDateTime,attr_FireOutDateTime,attr_POOProtectingUnit,"
                     "attr_LocalIncidentIdentifier",
        "returnGeometry": "true", "outSR": crs, "f": "json",
    }
    with urllib.request.urlopen(f"{WFIGS}?{urllib.parse.urlencode(params)}", timeout=120) as r:
        features = json.load(r)["features"]
    if len(features) != 1:
        raise SystemExit(f"expected one WFIGS perimeter for {name} {year}, got {len(features)}")
    rings = [Polygon(ring).buffer(0) for ring in features[0]["geometry"]["rings"]]
    geometry = reduce(lambda a, b: a.symmetric_difference(b), rings)
    props = features[0]["attributes"]
    area = geometry.area / 1e6
    expected = props["poly_GISAcres"] * 0.00404686
    if not geometry.is_valid:
        raise SystemExit("WFIGS perimeter rings do not form a valid polygon")
    if abs(area / expected - 1) > GIS_TOLERANCE:
        # The area check guards the ring assembly. poly_GISAcres is sometimes stale (Border 6
        # 2026: 1,618 ac for a 2,614 ac polygon whose incident size is 2,525 ac). Accept the
        # polygon then only if the assembly is unambiguous (no ring inside another, so XOR
        # and union agree) and it matches the reported incident size.
        union = reduce(lambda a, b: a.union(b), rings).area / 1e6
        size = (props.get("attr_IncidentSize") or 0) * 0.00404686
        if abs(union - area) > 1e-6 * area or not size or abs(area / size - 1) > SIZE_TOLERANCE:
            raise SystemExit(f"perimeter area {area:.1f} km2 does not match WFIGS {expected:.1f} km2 "
                             f"(GIS acres) or {size:.1f} km2 (incident size)")
        print(f"WFIGS: GIS acres ({expected:.1f} km2) is out of date with the polygon "
              f"({area:.1f} km2); accepted on the incident size ({size:.1f} km2, "
              f"{100 * (area / size - 1):+.1f}%)")
    return {"type": "Feature", "properties": props, "geometry": mapping(geometry)}


def recover_snapshot(fire, out):
    """The latest daily perimeter in the fire's RECOVER bundle, as a feature in the fire's
    CRS, or SystemExit saying why not. The geodatabase is copied out of the bundle with
    HTTP range reads (5-25 MB) and read locally: reading it over the network through GDAL
    can leave the process unable to exit on Windows (prepare_fire.fetch_cache)."""
    import fiona
    from pyproj import Transformer
    if not (fire.get("wfigs_unit") and fire.get("wfigs_local_id")):
        raise SystemExit("no RECOVER bundle address in the config")
    url = BUNDLE.format(year=fire["year"], unit=fire["wfigs_unit"], local_id=fire["wfigs_local_id"])
    local = out / "recover_gdb"
    if not (local / ".done").exists():
        try:
            remote = RangeFile(url)
        except Exception as e:
            raise SystemExit(f"no RECOVER bundle for this fire ({e})") from None
        with zipfile.ZipFile(io.BufferedReader(remote, buffer_size=1 << 20)) as z:
            members = [n for n in z.namelist() if ".gdb/" in n and not n.endswith("/")]
            if not members:
                raise SystemExit("the RECOVER bundle has no geodatabase")
            for n in members:
                target = local / n.split("/", 1)[1]           # <folder>/<name>.gdb/<file>
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(n))
        (local / ".done").write_text(url)
    gdb = next(local.rglob("*.gdb"))
    layer = next((l for l in fiona.listlayers(str(gdb)) if l.startswith("Fire_")), None)
    if not layer:
        raise SystemExit("the RECOVER bundle has no fire perimeter layer")
    with fiona.open(str(gdb), layer=layer) as c:
        to_fire = Transformer.from_crs(c.crs, f"EPSG:{fire['crs']}", always_xy=True).transform
        snaps = [f for f in c if f["geometry"]]
    latest = max(snaps, key=lambda f: f["properties"].get("poly_DateCurrent") or "")
    when = (latest["properties"].get("poly_DateCurrent") or "")[:10]
    acres = latest["properties"].get("poly_GISAcres")
    if not when:
        raise SystemExit("the RECOVER perimeter has no date")
    gap = (date.fromisoformat(fire["containment"]) - date.fromisoformat(when)).days
    known = fire.get("wfigs_acres")
    if known and acres:
        if abs(acres / known - 1) > RECOVER_ACRES:
            raise SystemExit(f"RECOVER's latest perimeter ({when}) is {acres:,.0f} acres, WFIGS last "
                             f"reported {known:,.0f}; not the final perimeter")
        check = f"{acres:,.0f} acres, WFIGS last reported {known:,.0f}"
    elif gap > RECOVER_MAX_DAYS:
        raise SystemExit(f"RECOVER's latest perimeter is from {when}, {gap} days before "
                         f"containment ({fire['containment']}), and no WFIGS acreage is recorded "
                         "to confirm it is the final one")
    else:
        check = f"{max(gap, 0)} days before containment"
    geom = shapely_transform(to_fire, shape(latest["geometry"]).buffer(0))
    props = {k: v for k, v in latest["properties"].items()
             if k.startswith(("poly_", "attr_")) and not isinstance(v, (bytes, bytearray))}
    props["_source"] = f"RECOVER daily perimeter of {when} (WFIGS unreachable)"
    print(f"RECOVER: latest of {len(snaps)} daily perimeters is from {when} ({check}); "
          f"{geom.area / 1e6:.1f} km2")
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


def fallback_perimeter(fire, out, error):
    """WFIGS unreachable: the perimeter saved by the last run, else RECOVER's (module doc)."""
    print(f"WFIGS unreachable ({error}); trying fallback perimeters")
    saved = out / "perimeter_wfigs.geojson"
    if saved.exists():
        feature = json.loads(saved.read_text())
        p = feature["properties"]
        source = p.get("_source", "WFIGS final perimeter").split(" (")[0].split(", saved")[0]
        p["_source"] = f"{source}, saved {p.get('_fetched', 'by an earlier run')} (WFIGS unreachable)"
        print(f"perimeter: {p['_source']}")
        return feature
    try:
        return recover_snapshot(fire, out)
    except SystemExit as e:
        raise SystemExit(f"WFIGS unreachable ({error}), no saved perimeter, and no usable "
                         f"RECOVER perimeter: {e}") from None


def main():
    fire = json.loads(Path(sys.argv[1]).read_text())
    out = ROOT / "data" / "recover" / fire_key(fire)
    out.mkdir(parents=True, exist_ok=True)

    try:
        feature = wfigs(fire.get("wfigs_name", fire["name"]), fire["year"], fire["crs"],
                        fire.get("wfigs_unit"), fire.get("wfigs_local_id"), fire.get("unique_id"))
    except (OSError, RuntimeError, KeyError, ValueError) as e:      # unreachable or failing service
        feature = fallback_perimeter(fire, out, e)
    props = feature["properties"]
    if "_source" not in props:              # straight from WFIGS
        props.update(_source="WFIGS final perimeter", _fetched=date.today().isoformat())
        # Refresh the dates: a fire first run while burning has a stale containment (or the
        # ignition + 90 d stand-in), and containment sets the post-fire image window.
        # "dates_locked": true in the config keeps hand-set dates.
        ignition, containment = wfigs_dates(props)
        changed = {}
        if not fire.get("dates_locked") and (fire.get("ignition"), fire.get("containment")) != (ignition, containment):
            print(f"WFIGS dates updated: ignition {fire.get('ignition')} -> {ignition}, "
                  f"containment {fire.get('containment')} -> {containment}")
            changed.update(ignition=ignition, containment=containment)
        # the acreage the RECOVER fallback checks a snapshot against
        if props.get("poly_GISAcres") and fire.get("wfigs_acres") != round(props["poly_GISAcres"], 1):
            changed["wfigs_acres"] = round(props["poly_GISAcres"], 1)
        if changed:
            fire.update(changed)
            Path(sys.argv[1]).write_text(json.dumps(fire, indent=1) + "\n")
    feature["crs"] = {"type": "name", "properties": {"name": f"EPSG:{fire['crs']}"}}
    (out / "perimeter_wfigs.geojson").write_text(json.dumps(feature))
    print(f"WFIGS: {props['poly_IncidentName']}, {props['poly_GISAcres']:,.0f} acres "
          f"({props['poly_GISAcres'] * 0.00404686:.1f} km2); assembled perimeter "
          f"{shape(feature['geometry']).area / 1e6:.1f} km2")

    url = BUNDLE.format(year=fire["year"], unit=props.get("attr_POOProtectingUnit") or fire["wfigs_unit"],
                        local_id=props.get("attr_LocalIncidentIdentifier") or fire["wfigs_local_id"])
    if not fire.get("recover_members"):
        # No bundle (RECOVER starts in 2022 and does not cover every fire): the only
        # member we use is EVT, and prepare_fire.py runs without it.
        print("RECOVER: no bundle members requested; continuing without EVT")
        (out / "wfigs.json").write_text(json.dumps({"bundle": None, **props}, indent=1))
        return
    remote = RangeFile(url)
    print(f"RECOVER bundle: {url} ({remote.size / 1e6:.0f} MB)")
    with zipfile.ZipFile(io.BufferedReader(remote, buffer_size=1 << 20)) as z:
        members = {Path(n).name: n for n in z.namelist()}
        for wanted in fire["recover_members"]:
            if wanted not in members:
                # Bundles vary by year (Eagle Bluff 2023 has no TopographyElevation DEM). The
                # pipeline's DEM comes from USGS, and a missing EVT falls back to LANDFIRE in
                # prepare_fire.py, so a missing member is noted, not fatal.
                print(f"RECOVER: {wanted} not in this bundle; skipped")
                continue
            target = out / wanted
            if not target.exists():
                target.write_bytes(z.read(members[wanted]))
            print(f"  {wanted}: {target.stat().st_size / 1e6:.1f} MB")
    (out / "wfigs.json").write_text(json.dumps({"bundle": url, **props}, indent=1))


if __name__ == "__main__":
    main()
