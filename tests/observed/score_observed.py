"""Score the pipeline's rainfall thresholds against observed post-fire debris flows.

Reproduces the white paper's validation (Table 3). First run the 12 fires in fires.txt:

    uv run python pipeline/batch.py all tests/observed/fires.txt --name observed --runner local --workers 3
    uv run python tests/observed/score_observed.py

The observations are downloaded once from ScienceBase into data/observed/ (public, no account):
  * USGS runoff-generated postfire debris-flow inventory v1, https://doi.org/10.5066/P1R9VXC4
  * Selander et al. 2024, western Cascades of Oregon, https://doi.org/10.5066/P13TPP8J
Each row is one watershed in one storm: debris flow or not, the storm's peak 15-minute
intensity at the nearest rain gauge, and the watershed's location (and area).

Matching: our stream segment nearest the observation, within 150 m, whose drainage area is
within a factor of 2 of the observed watershed's. Oregon rows have no area; it comes from the
USGS assessment segment they name (Fire_SegID), which the pipeline's usgs stage downloads.
Predicted flow = observed peak I15 >= the segment's I15_50 (M1, 15 minutes, p = 0.5). The
USGS baseline is USGS's own M1 threshold for the same watershed: computed from the
inventory's T, F, S (HM23, dNBR/1000, KF), or the Oregon release's M1_I15. Only the first
year after each fire, and only observations matched in both, are scored.

Writes data/observed/observed_scores.csv and compares with expected.csv (the white paper).
"""

import json
import sys
from pathlib import Path
from urllib.request import urlopen

import fiona
import numpy as np
import pandas as pd
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform as shapely_transform
from shapely.strtree import STRtree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import finished_project  # noqa: E402
from usgs_check import DNBR_OK  # noqa: E402

DATA = ROOT / "data" / "observed"
SCIENCEBASE = "https://www.sciencebase.gov/catalog/item/{}?format=json"
INVENTORY = ("69612c8ed4be02298529c522", "USGS_RG_PFDF_Inventory_v1.csv")
OREGON = ("65bd7eecd34e18c6baf33d4b", "OR_field_observations.csv")
MATCH_M = 150
M1_15 = (-3.63, 0.41, 0.67, 0.70)   # Staley et al. (2017) M1, 15 minutes: B, Ct, Cf, Cs

# observation fire -> pipeline fire key, state
INVENTORY_FIRES = {("Dolan", 2020): "dolan_2020", ("Grizzly Creek", 2020): "grizzly_creek_2020",
                   ("Cameron Peak", 2020): "cameron_peak_2020", ("Tadpole", 2020): "tadpole_2020",
                   ("East Fork", 2020): "east_fork_2020", ("Cub Creek 2", 2021): "cub_creek_2_2021",
                   ("Cedar Creek", 2021): "cedar_creek_2021", ("Muckamuck", 2021): "muckamuck_2021",
                   ("Elk", 2024): "elk_2024"}
OREGON_FIRES = {"Archie Creek": "archie_creek_2020", "Holiday Farm": "holiday_farm_2020",
                "Riverside": "riverside_2020"}


def download(item, name):
    """A ScienceBase file, saved once in data/observed/."""
    path = DATA / name
    if not path.exists():
        files = json.load(urlopen(SCIENCEBASE.format(item)))["files"]
        url = next(f["downloadUri"] for f in files if f["name"] == name)
        DATA.mkdir(parents=True, exist_ok=True)
        path.write_bytes(urlopen(url).read())
        print(f"downloaded {name} from ScienceBase item {item}")
    return path


def m1_threshold(T, F, S):
    """M1 15-minute rainfall intensity at p = 0.5 (mm/h); negative (unburned) -> NaN."""
    B, Ct, Cf, Cs = M1_15
    with np.errstate(divide="ignore"):
        th = -B / (Ct * np.asarray(T) + Cf * np.asarray(F) + Cs * np.asarray(S)) / 0.25
    return np.where(th >= 0, th, np.nan)


def inventory_observations():
    d = pd.read_csv(download(*INVENTORY), low_memory=False, na_values=[-9999, "-9999"])
    d["key"] = [INVENTORY_FIRES.get((n, y)) for n, y in zip(d.FireName, d.FireYear)]
    d = d[d.key.notna()].dropna(subset=["Response", "PeakI15", "UpstreamArea"])
    return pd.DataFrame({
        "key": d.key, "lat": d.ObservationLatitude, "lon": d.ObservationLongitude,
        "flow": d.Response.astype(int), "peak_i15": d.PeakI15, "gauge_km": d.GageDistance,
        "year1": d.YearAfterFire == 1, "area_km2": d.UpstreamArea,
        "usgs_i15_50": m1_threshold(d.HM23, d["dNBR/1000"], d.KF)})


def oregon_observations():
    d = pd.read_csv(download(*OREGON), encoding="utf-8-sig").dropna(subset=["Response", "Peak_I15"])
    d = d[d.Fire_Name.isin(OREGON_FIRES)]
    ignition = pd.to_datetime(d.Date_Of_Fire_Initiation, format="%m/%d/%Y")
    storm = pd.to_datetime(d.Peak_I15_date.str[:10])
    out = pd.DataFrame({
        "key": d.Fire_Name.map(OREGON_FIRES), "lat": d.Observation_Lat, "lon": d.Observation_Long,
        "flow": d.Response.astype(int), "peak_i15": d.Peak_I15, "gauge_km": d.Gage_Dist,
        "year1": (storm - ignition).dt.days <= 365, "area_km2": np.nan,
        "usgs_i15_50": d.M1_I15, "usgs_seg": d.Fire_SegID})
    for key, rows in out.groupby("key"):
        areas = usgs_segment_areas(key)
        out.loc[rows.index, "area_km2"] = [areas.get(s.rsplit("_", 1)[-1]) for s in rows.usgs_seg]
    return out.drop(columns="usgs_seg")


def usgs_segment_areas(key):
    """{segment id: drainage area km2} from the USGS assessment the usgs stage downloaded."""
    ref = json.loads((ROOT / "fires" / f"{key}.json").read_text()).get("usgs_reference") or {}
    if not ref.get("segments"):
        raise SystemExit(f"{key}: no USGS assessment recorded in fires/{key}.json; run the "
                         "fire's usgs stage first (Oregon rows take their area from it)")
    with fiona.open(ROOT / ref["segments"]) as c:
        return {str(int(f["properties"]["Segment_ID"])): f["properties"][ref["fields"]["area_km2"]]
                for f in c}


def our_thresholds(project, obs):
    """Our I15_50 at each observation: nearest segment within MATCH_M of similar area, else NaN."""
    with fiona.open(project / "exports" / "segments.shp") as c:
        crs = CRS.from_wkt(c.crs_wkt)
        feats = [(shape(f["geometry"]), f["properties"]) for f in c]
    lon0 = np.mean([g.centroid.x for g, _ in feats[:200]])
    utm = CRS.from_epsg(32600 + int((lon0 + 180) // 6) + 1)   # measure in metres
    to_utm = Transformer.from_crs(crs, utm, always_xy=True)
    geoms = [shapely_transform(to_utm.transform, g) for g, _ in feats]
    tree = STRtree(geoms)
    pt_utm = Transformer.from_crs(4326, utm, always_xy=True)
    out = []
    for lon, lat, area in zip(obs.lon, obs.lat, obs.area_km2):
        pt = Point(*pt_utm.transform(lon, lat))
        value = np.nan
        for i in sorted(tree.query(pt.buffer(MATCH_M)), key=lambda i: pt.distance(geoms[i])):
            props = feats[i][1]
            if np.isfinite(area) and props["Area_km2"] and 0.5 <= props["Area_km2"] / area <= 2:
                value = np.nan if props["I15_50"] is None else float(props["I15_50"])
                break
        out.append(value)
    return np.array(out, float)


def counts(df, column):
    predicted = df.peak_i15.to_numpy() >= df[column].to_numpy()
    flow = df.flow.to_numpy().astype(bool)
    tp, fp = int((predicted & flow).sum()), int((predicted & ~flow).sum())
    fn, tn = int((~predicted & flow).sum()), int((~predicted & ~flow).sum())
    den = np.sqrt(float((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)))
    return {"TP": tp, "FP": fp, "TS": tp / (tp + fn + fp) if tp + fn + fp else np.nan,
            "MCC": (tp * tn - fp * fn) / den if den else np.nan}


def score(key, df):
    row = {"key": key, "cases": len(df), "flows": int(df.flow.sum())}
    for who in ("ours", "usgs"):
        row.update({f"{who}_{k}": v for k, v in counts(df, f"{who}_i15_50").items()})
    return row


def main():
    obs = pd.concat([oregon_observations(), inventory_observations()], ignore_index=True)
    rows, scored = [], []
    for key, o in obs.groupby("key", sort=False):
        project = finished_project(key)
        if project is None:
            raise SystemExit(f"{key}: no finished project in data/; run the fires in "
                             "tests/observed/fires.txt first")
        check = project / "usgs_check.json"
        f_ratio = json.loads(check.read_text()).get("F_ratio") if check.exists() else None
        if f_ratio is None or not DNBR_OK[0] <= f_ratio <= DNBR_OK[1]:
            print(f"WARNING {key}: dNBR ratio to USGS's is {f_ratio}, outside {DNBR_OK}: "
                  "its burn inputs are not like USGS's, so its score is not comparable")
        o = o[o.year1].assign(ours_i15_50=lambda d: our_thresholds(project, d))
        o = o[np.isfinite(o.ours_i15_50) & np.isfinite(o.usgs_i15_50)]
        scored.append(o)
        rows.append(score(key, o))
    every = pd.concat(scored)
    rows.append(score("all", every))
    rows.append(score("all_gauge_4km", every[every.gauge_km <= 4]))
    result = pd.DataFrame(rows)
    DATA.mkdir(parents=True, exist_ok=True)
    result.to_csv(DATA / "observed_scores.csv", index=False)

    expected = pd.read_csv(HERE / "expected.csv").set_index("key")
    cols = list(expected.columns)
    got = result.set_index("key")[cols]
    print(f"{'fire':20s}" + "".join(f"{c:>16s}" for c in cols))
    differ = 0
    for key in expected.index:
        cells = []
        for c in cols:
            e, g = expected.at[key, c], got.at[key, c]
            same = pd.isna(e) or e == g
            differ += not same
            cells.append(f"{g}" if same else f"{g} (paper {int(e)})")
        print(f"{key:20s}" + "".join(f"{c:>16s}" for c in cells))
    total = result.set_index("key").loc["all"].to_dict()
    print(f"\nall fires, ours: caught {total['ours_TP']:.0f}/{total['flows']:.0f}, false alarms "
          f"{total['ours_FP']:.0f}/{total['cases'] - total['flows']:.0f}, TS {total['ours_TS']:.2f}, MCC {total['ours_MCC']:.2f}")
    print(f"all fires, USGS: caught {total['usgs_TP']:.0f}/{total['flows']:.0f}, false alarms "
          f"{total['usgs_FP']:.0f}/{total['cases'] - total['flows']:.0f}, TS {total['usgs_TS']:.2f}, MCC {total['usgs_MCC']:.2f}")
    print(f"\n{differ} of {expected.notna().sum().sum()} numbers differ from the white paper "
          f"(wrote {DATA / 'observed_scores.csv'})")


if __name__ == "__main__":
    main()
