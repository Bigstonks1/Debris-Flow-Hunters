"""Compare our wildcat inputs and thresholds with USGS's operational assessment of a fire.

The reference is USGS's own segment file, named in the fire config (`usgs_reference`) with
its field names for T, F, S, the 15-minute p = 0.5 threshold, and drainage area:
  Grizzly  grz2020 archive (https://doi.org/10.5066/P142R8E2): L_X1..3, RainIntAtP; 25.0 mm/h
  Wapiti   wap2024 (2024 collection, https://doi.org/10.5066/P13GTMAX): T, F, S, I15_50;
           34.8 mm/h segment median (wap2024-median-thresholds.csv)

USGS segments are cut differently from ours, so raw medians are not comparable. Each USGS
segment is matched to our nearest segment and kept only if it is within MATCH_M and
drains a similar area (ratio in [0.5, 2]); metrics are medians of per-pair ratios.

    python pipeline/compare_usgs.py fires/<fire>.json data/<project> [data/<project> ...]
"""

import json
import sys
from pathlib import Path

import fiona
import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import shape
from shapely.ops import transform
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
MATCH_M = 20


def load(path):
    with fiona.open(path) as c:
        return CRS.from_wkt(c.crs_wkt), [(shape(f["geometry"]), f["properties"]) for f in c]


def value(props, key):
    v = props.get(key)
    return np.nan if v is None else float(v)


def compare(ref, project, usgs=None):
    """Our project against a USGS reference (the config's `usgs_reference`): matched pairs,
    median per-pair ratios for T, F, S and the threshold, and both sides' medians."""
    fields = ref["fields"]
    ucrs, usgs = usgs or load(ROOT / ref["segments"])
    ocrs, ours = load(Path(project) / "exports" / "segments.shp")
    # Match in metres. wildcat exports in WGS 84 degrees, so measuring in our CRS (as this
    # did until 2026-09-26) made MATCH_M a 20-degree limit: any USGS segment could pair
    # with our nearest one of similar area, even on a different fire 70 km away.
    metric = ucrs if ucrs.is_projected else ocrs if ocrs.is_projected else CRS.from_epsg(5070)
    to_m_ours = Transformer.from_crs(ocrs, metric, always_xy=True).transform
    to_m_usgs = Transformer.from_crs(ucrs, metric, always_xy=True).transform
    ours_m = [transform(to_m_ours, g) for g, _ in ours]
    tree = STRtree(ours_m)
    pairs = []
    for geom, up in usgs:
        area = value(up, fields["area_km2"])
        if not area > 0:
            continue
        pt = transform(to_m_usgs, geom.representative_point())
        i = tree.nearest(pt)
        og, op = ours_m[i], ours[i][1]
        if pt.distance(og) > MATCH_M or not 0.5 <= op["Area_km2"] / area <= 2:
            continue
        pairs.append((value(op, "Terrain_M1"), value(up, fields["T"]),
                      value(op, "Fire_M1"), value(up, fields["F"]),
                      value(op, "Soil_M1"), value(up, fields["S"]),
                      value(op, "I15_50"), value(up, fields["I15_50"])))
    a = np.array(pairs, float).reshape(-1, 8)

    def ratio(i, j):
        ok = (a[:, j] > 0) & np.isfinite(a[:, i]) & np.isfinite(a[:, j])
        return float(np.median(a[ok, i] / a[ok, j])) if ok.any() else float("nan")

    return {"pairs": len(a), "T_ratio": ratio(0, 1), "F_ratio": ratio(2, 3),
            "S_ratio": ratio(4, 5), "threshold_ratio": ratio(6, 7),
            "usgs_matched_median": float(np.nanmedian(a[:, 7])) if len(a) else float("nan"),
            "our_matched_median": float(np.nanmedian(a[:, 6])) if len(a) else float("nan"),
            "our_fire_wide": float(np.nanmedian([value(p, "I15_50") for _, p in ours])),
            "usgs_guidance": ref.get("guidance_I15_50")}


def main():
    fire = json.loads(Path(sys.argv[1]).read_text())
    ref = fire["usgs_reference"]
    fields = ref["fields"]
    usgs = load(ROOT / ref["segments"])

    def med(key):
        return np.nanmedian([value(p, fields[key]) for _, p in usgs[1]])

    print(f"USGS {fire['name']}: {len(usgs[1])} segments; guidance {ref['guidance_I15_50']} mm/h; "
          f"medians T {med('T'):.3f} F {med('F'):.3f} S {med('S'):.3f} I15_50 {med('I15_50'):.1f}\n")
    print(f"{'project':<34}{'pairs':>7}{'T ratio':>9}{'F ratio':>9}{'S ratio':>9}"
          f"{'thr ratio':>11}{'USGS thr':>10}{'our thr':>9}{'our fire-wide':>15}")
    for project in map(Path, sys.argv[2:]):
        r = compare(ref, project, usgs)
        print(f"{project.name:<34}{r['pairs']:>7}{r['T_ratio']:>9.2f}{r['F_ratio']:>9.2f}"
              f"{r['S_ratio']:>9.2f}{r['threshold_ratio']:>11.2f}{r['usgs_matched_median']:>10.1f}"
              f"{r['our_matched_median']:>9.1f}{r['our_fire_wide']:>15.1f}")


if __name__ == "__main__":
    main()
