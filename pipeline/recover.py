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
from fires import fire_key  # noqa: E402
WFIGS = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
         "WFIGS_Interagency_Perimeters/FeatureServer/0/query")
BUNDLE = ("https://giscenter-sl.isu.edu/aoc/aoc_research/recover2/archive/"
          "{year}/Fire_{year}_{unit}_{local_id}.zip")


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
        "outFields": "poly_IncidentName,poly_GISAcres,attr_FireDiscoveryDateTime,"
                     "attr_ContainmentDateTime,attr_POOProtectingUnit,attr_LocalIncidentIdentifier",
        "returnGeometry": "true", "outSR": crs, "f": "json",
    }
    with urllib.request.urlopen(f"{WFIGS}?{urllib.parse.urlencode(params)}", timeout=120) as r:
        features = json.load(r)["features"]
    if len(features) != 1:
        raise SystemExit(f"expected one WFIGS perimeter for {name} {year}, got {len(features)}")
    rings = [Polygon(ring).buffer(0) for ring in features[0]["geometry"]["rings"]]
    geometry = reduce(lambda a, b: a.symmetric_difference(b), rings)
    props = features[0]["attributes"]
    expected = props["poly_GISAcres"] * 0.00404686
    if not geometry.is_valid or abs(geometry.area / 1e6 / expected - 1) > 0.01:
        raise SystemExit(f"perimeter area {geometry.area / 1e6:.1f} km2 does not match WFIGS "
                         f"{expected:.1f} km2")
    return {"type": "Feature", "properties": props, "geometry": mapping(geometry)}


def main():
    fire = json.loads(Path(sys.argv[1]).read_text())
    out = ROOT / "data" / "recover" / fire_key(fire)
    out.mkdir(parents=True, exist_ok=True)

    feature = wfigs(fire.get("wfigs_name", fire["name"]), fire["year"], fire["crs"],
                    fire.get("wfigs_unit"), fire.get("wfigs_local_id"), fire.get("unique_id"))
    props = feature["properties"]
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
                raise SystemExit(f"{wanted} not in bundle")
            target = out / wanted
            if not target.exists():
                target.write_bytes(z.read(members[wanted]))
            print(f"  {wanted}: {target.stat().st_size / 1e6:.1f} MB")
    (out / "wfigs.json").write_text(json.dumps({"bundle": url, **props}, indent=1))


if __name__ == "__main__":
    main()
