"""Check a finished fire against USGS's own assessment of it (CI job 2).

    uv run python pipeline/run_fire.py --id 2020-NMGNF-000232
    uv run python tests/check_fire.py tadpole_2020

Fails if the fire has no finished project, if USGS's assessment was not found or compared,
or if our thresholds or burn imagery drift from USGS's: threshold ratio outside
THRESHOLD_OK, or dNBR ratio outside the pipeline's own DNBR_OK (usgs_check.py).
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import finished_project  # noqa: E402
from usgs_check import DNBR_OK  # noqa: E402

# Tadpole 2020, 2026-10-11 and 2026-10-14 runs: threshold ratio 0.901, dNBR ratio 1.00
THRESHOLD_OK = (0.8, 1.1)


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "tadpole_2020"
    project = finished_project(key)
    if project is None:
        raise SystemExit(f"{key}: no finished project in data/")
    check = json.loads((project / "usgs_check.json").read_text())
    print(json.dumps(check, indent=1))
    if not check.get("found"):
        raise SystemExit(f"{key}: USGS's assessment was not found or could not be compared")
    ratio, f_ratio = check["threshold_ratio"], check["F_ratio"]
    if not THRESHOLD_OK[0] <= ratio <= THRESHOLD_OK[1]:
        raise SystemExit(f"{key}: threshold ratio to USGS {ratio:.3f}, outside {THRESHOLD_OK}")
    if not DNBR_OK[0] <= f_ratio <= DNBR_OK[1]:
        raise SystemExit(f"{key}: dNBR ratio to USGS {f_ratio:.3f}, outside {DNBR_OK}")
    print(f"ok {key}: threshold ratio {ratio:.3f}, dNBR ratio {f_ratio:.3f}")


if __name__ == "__main__":
    main()
