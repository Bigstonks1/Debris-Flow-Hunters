"""Seam 1: fetch a fire's perimeter and RECOVER inputs, starting from its name and year.

  1. WFIGS interagency perimeters: look up the incident, take its final perimeter and
     the two fields that name the RECOVER bundle (POOProtectingUnit, LocalIncidentIdentifier).
  2. RECOVER archive: read only the listed members out of the bundle zip with HTTP range
     requests. Bundles are large (Wapiti: 852 MB) and mostly NDVI and slope products we
     do not use; the DEM and EVT we need are ~50 MB.

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

from shapely.geometry import Polygon, mapping, shape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import fire_key, wfigs_dates  # noqa: E402
WFIGS = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
         "WFIGS_Interagency_Perimeters/FeatureServer/0/query")
BUNDLE = ("https://giscenter-sl.isu.edu/aoc/aoc_research/recover2/archive/"
          "{year}/Fire_{year}_{unit}_{local_id}.zip")
GIS_TOLERANCE = 0.01     # assembled area vs poly_GISAcres (the polygon's own computed area)
SIZE_TOLERANCE = 0.10    # vs attr_IncidentSize, a reported figure, when GIS acres are stale


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


def main():
    fire = json.loads(Path(sys.argv[1]).read_text())
    out = ROOT / "data" / "recover" / fire_key(fire)
    out.mkdir(parents=True, exist_ok=True)

    feature = wfigs(fire.get("wfigs_name", fire["name"]), fire["year"], fire["crs"],
                    fire.get("wfigs_unit"), fire.get("wfigs_local_id"), fire.get("unique_id"))
    props = feature["properties"]
    # Refresh the dates: a fire first run while burning has a stale containment (or the
    # ignition + 90 d stand-in), and containment sets the post-fire image window.
    # "dates_locked": true in the config keeps hand-set dates.
    ignition, containment = wfigs_dates(props)
    if not fire.get("dates_locked") and (fire.get("ignition"), fire.get("containment")) != (ignition, containment):
        print(f"WFIGS dates updated: ignition {fire.get('ignition')} -> {ignition}, "
              f"containment {fire.get('containment')} -> {containment}")
        fire.update(ignition=ignition, containment=containment)
        Path(sys.argv[1]).write_text(json.dumps(fire, indent=1) + "\n")
    feature["crs"] = {"type": "name", "properties": {"name": f"EPSG:{fire['crs']}"}}
    (out / "perimeter_wfigs.geojson").write_text(json.dumps(feature))
    print(f"WFIGS: {props['poly_IncidentName']}, {props['poly_GISAcres']:,.0f} acres "
          f"({props['poly_GISAcres'] * 0.00404686:.1f} km2); assembled perimeter "
          f"{shape(feature['geometry']).area / 1e6:.1f} km2")

    url = BUNDLE.format(year=fire["year"], unit=props["attr_POOProtectingUnit"],
                        local_id=props["attr_LocalIncidentIdentifier"])
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
