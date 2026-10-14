"""Write fires/<key>.json for a fire from its name and year, or its Unique Fire ID.

Everything the pipeline needs comes from the fire's WFIGS interagency perimeter record:
dates, state, a UTM CRS from the perimeter's centroid, the Unique Fire Identifier (see
fires.py) and whether a RECOVER bundle exists (RECOVER is optional, see
pipeline/recover.py). A fire that already has a config (matched by its Unique Fire ID) is
left as is: hand-written fields such as a BAER package or a USGS reference live there. Its
dates are refreshed from WFIGS on every run by pipeline/recover.py.

    python pipeline/new_fire.py "Rush" 2025 [ID]         # state, when the name is not unique
    python pipeline/new_fire.py --id 2025-IDPAF-005844
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from shapely.geometry import Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fires import FIRES, describe, find_config, lookup, slug, wfigs_dates  # noqa: E402
from recover import BUNDLE  # noqa: E402

RECOVER_MEMBERS = ["TopographyElevation_WesternUS_be.tif", "TopographyElevation_WesternUS_be.tfw",
                   "EVT_ExistingVegetationType.tif", "EVT_ExistingVegetationType.tif.vat.dbf"]


def ensure_config(name=None, year=None, state=None, uid=None):
    """Path of the fire's config, writing it first if the fire is new. With WFIGS
    unreachable, a fire already configured (by Unique Fire ID) still runs from its config;
    pipeline/recover.py then falls back to a saved or RECOVER perimeter."""
    try:
        found = lookup(name, year, state, uid, geometry=True)
    except (OSError, RuntimeError) as e:
        existing = find_config(uid.upper()) if uid else None
        if not existing:
            what = uid or f"{name} {year}"
            raise SystemExit(f"WFIGS unreachable ({e}); {what} has not been run here before, so it "
                             "cannot be looked up until WFIGS is back") from None
        print(f"WFIGS unreachable ({e}); using the existing config {existing}")
        return existing
    if len(found) != 1:
        listing = "".join(f"\n  {c['unique_id']}  {c['name']}, {c['county']} County, "
                          f"{c['state']}, {c['acres']:,} acres"
                          for c in map(describe, found))
        what = uid or f"{name} {year}{f' ({state})' if state else ''}"
        raise SystemExit(f"expected one WFIGS perimeter for {what}, got {len(found)}"
                         + (f"; choose one with --id:{listing}" if listing else ""))
    feature = found[0]
    card = describe(feature)
    existing = find_config(card["unique_id"])
    if existing:
        print(f"{existing} exists for {card['unique_id']}; leaving it as is")
        return existing

    a = feature["attributes"]
    ring = max((Polygon(r) for r in feature["geometry"]["rings"]), key=lambda p: p.area)
    lon, lat = ring.centroid.x, ring.centroid.y
    zone = int((lon + 180) // 6) + 1
    ignition, containment = wfigs_dates(a)
    bundle = BUNDLE.format(year=card["year"], unit=a["attr_POOProtectingUnit"],
                           local_id=a["attr_LocalIncidentIdentifier"])
    try:
        urllib.request.urlopen(urllib.request.Request(bundle, method="HEAD"), timeout=60)
        has_bundle = True
    except urllib.error.HTTPError:
        has_bundle = False
    # name_year keeps same-named fires of different years apart; the unit breaks the rare
    # same-name, same-year tie
    key = f"{slug(card['name'])}_{card['year']}"
    if (FIRES / f"{key}.json").exists():
        key += f"_{a['attr_POOProtectingUnit'].lower()}"
    fire = {
        "name": card["name"], "key": key, "unique_id": card["unique_id"],
        "irwin_id": a["attr_IrwinID"],
        "year": card["year"], "state": card["state"], "county": card["county"],
        "ignition": ignition, "containment": containment,
        "crs": (32600 if lat >= 0 else 32700) + zone,
        "wfigs_name": a["poly_IncidentName"],
        "wfigs_unit": a["attr_POOProtectingUnit"],
        "wfigs_local_id": a["attr_LocalIncidentIdentifier"],
        "recover_members": RECOVER_MEMBERS if has_bundle else [],
    }
    out = FIRES / f"{key}.json"
    out.write_text(json.dumps(fire, indent=1) + "\n")
    print(f"wrote {out}: {fire['name']} {fire['year']} ({fire['state']}), {card['unique_id']}, "
          f"{card['acres']:,} acres, EPSG:{fire['crs']}, {fire['ignition']} to "
          f"{fire['containment']}, RECOVER bundle {'found' if has_bundle else 'not found'}")
    return out


def main():
    if sys.argv[1] == "--id":
        print(f"config: {ensure_config(uid=sys.argv[2])}")
    else:
        state = sys.argv[3] if len(sys.argv) > 3 else None
        print(f"config: {ensure_config(sys.argv[1], int(sys.argv[2]), state)}")


if __name__ == "__main__":
    main()
