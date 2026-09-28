"""Fire identity: WFIGS lookups, the Unique Fire Identifier, and where a fire's files live.

Every wildfire has a national Unique Fire Identifier, YEAR-UNIT-NUMBER, e.g.
2025-IDPAF-005844: the 2025 Rush fire, protecting unit IDPAF (Payette National Forest),
local incident 5844. Names are not unique (four 2025 fires are called Rush); the ID is.
WFIGS carries it on every record (attr_UniqueFireIdentifier); the IRWIN ID is often blank.

A fire's files are keyed by `fire_key`: the config's "key" (new fires: name_year, e.g.
rush_2025, so same-named fires of different years never share folders), else the
lowercased name, which is how the first fires were stored (data/wapiti_..., etc.).
"""

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRES = ROOT / "fires"
WFIGS = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
         "WFIGS_Interagency_Perimeters/FeatureServer/0/query")
UID_RE = re.compile(r"^\d{4}-[A-Z]{2}[A-Z0-9]{2,6}-[A-Z0-9]{1,10}$")
FIELDS = ("poly_IncidentName,poly_GISAcres,attr_FireDiscoveryDateTime,attr_ContainmentDateTime,"
          "attr_FireOutDateTime,attr_POOState,attr_POOCounty,attr_POOProtectingUnit,"
          "attr_LocalIncidentIdentifier,attr_UniqueFireIdentifier,attr_IrwinID")


def slug(text):
    return "_".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def fire_key(fire):
    return fire.get("key") or fire["name"].lower()


def query(where, geometry=False):
    params = {"where": where, "outFields": FIELDS, "returnGeometry": str(geometry).lower(),
              "outSR": 4326, "f": "json", "orderByFields": "poly_GISAcres DESC"}
    with urllib.request.urlopen(f"{WFIGS}?{urllib.parse.urlencode(params)}", timeout=120) as r:
        data = json.load(r)
    if "error" in data:
        raise RuntimeError(f"WFIGS query failed: {data['error'].get('message')}")
    return data["features"]


def sql(text):
    return text.replace("'", "''")


def year_clause(year):
    return (f"attr_FireDiscoveryDateTime >= DATE '{year}-01-01' AND "
            f"attr_FireDiscoveryDateTime < DATE '{year + 1}-01-01'")


def lookup(name=None, year=None, state=None, uid=None, geometry=False):
    """WFIGS perimeters for a Unique Fire ID, or for a name and year (and state). An exact
    name comes first; with none, names containing it (e.g. "Big Bear" -> "BIG BEAR COMPLEX")."""
    if uid:
        return query(f"attr_UniqueFireIdentifier = '{sql(uid)}'", geometry)
    where = year_clause(year) + (f" AND attr_POOState = 'US-{state.upper()}'" if state else "")
    found = query(f"poly_IncidentName = '{sql(name)}' AND {where}", geometry)
    return found or query(f"UPPER(poly_IncidentName) LIKE '%{sql(name.upper())}%' AND {where}",
                          geometry)


def describe(feature):
    """A WFIGS record as the app's candidate card."""
    a = feature["attributes"]
    day = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat() if ms else None
    name = a["poly_IncidentName"].strip()
    return {"name": name.title() if name.isupper() else name,
            "unique_id": a["attr_UniqueFireIdentifier"],
            "state": (a["attr_POOState"] or "").replace("US-", ""),
            "county": a["attr_POOCounty"], "acres": round(a["poly_GISAcres"] or 0),
            "unit": a["attr_POOProtectingUnit"], "discovered": day(a["attr_FireDiscoveryDateTime"]),
            "year": int((day(a["attr_FireDiscoveryDateTime"]) or "0")[:4])}


def unique_id_of(config):
    """A config's Unique Fire ID: stored, or rebuilt from the WFIGS unit and local ID."""
    if config.get("unique_id"):
        return config["unique_id"]
    if config.get("wfigs_unit") and config.get("wfigs_local_id"):
        return f"{config['year']}-{config['wfigs_unit']}-{config['wfigs_local_id']}"
    return None


def find_config(uid):
    """fires/*.json for a Unique Fire ID, or None."""
    for path in sorted(FIRES.glob("*.json")):
        if unique_id_of(json.loads(path.read_text())) == uid:
            return path
    return None
