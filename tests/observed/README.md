# Validation against observed debris flows

Reproduces the white paper's Table 3: the pipeline's rainfall thresholds, and the USGS
assessment's, scored against debris flows (and storms without one) observed after 12 western
wildfires.

## Run

From the repository root, after `uv sync`:

```bash
uv run python pipeline/batch.py all tests/observed/fires.txt --name observed --runner local --workers 3
uv run python tests/observed/score_observed.py
```

The first command runs the full pipeline on each fire, exactly as the app does: WFIGS
perimeter, Sentinel-2 burn severity, Forest Service BAER severity map, elevation, soils,
wildcat, then the comparison with USGS's assessment of the same fire. It needs internet access.
The second scores the results, prints them next to the white paper's numbers (`expected.csv`)
and writes `data/observed/observed_scores.csv`.

## Files

| File | What |
|---|---|
| `fires.txt` | The 12 fires, by Unique Fire ID |
| `score_observed.py` | Downloads the observations, matches them to stream segments, scores |
| `expected.csv` | The white paper's numbers: cases, flows, flows caught (TP) and false alarms (FP), ours and USGS's |

## Data (public, no account)

| Observations | Source | Fires |
|---|---|---|
| USGS runoff-generated postfire debris-flow inventory v1 | Graber et al. 2026, https://doi.org/10.5066/P1R9VXC4, `USGS_RG_PFDF_Inventory_v1.csv` | Dolan (CA), Grizzly Creek and Cameron Peak (CO), Tadpole (NM), East Fork (UT), Cub Creek 2, Cedar Creek and Muckamuck (WA), Elk (WY) |
| Debris flows after the 2020 fires in the western Cascades of Oregon | Selander et al. 2024, https://doi.org/10.5066/P13TPP8J, `OR_field_observations.csv` | Archie Creek, Holiday Farm, Riverside (OR) |

Both are downloaded from ScienceBase into `data/observed/` on the first run.

## Method

- **Matching.** Each observation is one watershed in one storm. It is matched to our stream
  segment nearest the observation point, within 150 m, whose drainage area is within a factor
  of 2 of the watershed's. Oregon rows have no area, so it is taken from the USGS assessment
  segment they name. Observations with no such segment, or no threshold, are left out.
- **Prediction.** A flow is predicted when the storm's observed peak 15-minute intensity meets
  the segment's `I15_50`, the M1 model's 50% threshold (Staley et al. 2017).
- **Baseline.** The USGS assessment's threshold for the same watershed: M1 from the
  inventory's T, F, S (`HM23`, `dNBR/1000`, `KF`), or the Oregon release's `M1_I15`.
- **Scope.** Scored are the first year after each fire and observations matched in both
  assessments. TS = TP / (TP + FN + FP); MCC is the Matthews correlation coefficient.

## Fires left out

These fires have observations but cannot be run reproducibly, so they are not in the test:

- **East Troublesome 2020 (CO).** It burned in late October, and snow cover leaves no usable
  post-fire Sentinel-2 or Landsat image, so the run stops.
- **Hermits Peak–Calf Canyon 2022 (NM).** The WFIGS record carries the merged fire's acreage
  but only part of its polygon, so the pipeline refuses it. Calf Canyon's own record runs, but
  only on a Landsat composite and a BAER map covering 62% of the fire, so its burn inputs are
  not comparable to USGS's.
- **Beachie Creek 2020 (OR).** The automatic post-fire date falls before the fire's main run
  (the Labor Day blow-up), so its burn severity is far below USGS's (dNBR ratio −0.02).

The scorer warns if a fire's dNBR, compared with USGS's on matched streams, is outside
0.8–1.25×, the pipeline's own flag for inputs that are not like for like.

## Verified

On 2026-10-14 a fresh clone of this repository (commit `012b4ac` plus this folder), with no
cached downloads, ran the two commands above on a 12-thread Windows laptop. All 12 fires
finished (1,545 s for the batch, 3 at a time), and all 81 numbers in `expected.csv` were
reproduced exactly:

| | Flows caught | False alarms | TS | MCC |
|---|---|---|---|---|
| Our pipeline | 99 / 127 (78%) | 316 / 777 (41%) | 0.22 | 0.26 |
| USGS assessment | 89 / 127 (70%) | 289 / 777 (37%) | 0.21 | 0.23 |

With the rain gauge within 4 km: 52 and 51 of 60 flows caught.

Later runs can differ slightly if a source changes: WFIGS can revise a perimeter or dates,
and new satellite scenes can change the automatic choice of pre- and post-fire dates.
