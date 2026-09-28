"""Use the BAER inputs USGS published with its own assessment of a fire, when there are any.

Since its 2025 archive format, USGS publishes each assessment's inputs next to the results
on ScienceBase: `inputs.zip` with the dnbr.tif and severity.tif it ran (for Big Bear,
"BAER Team" products that appear in no other public service) plus `metadata.json` naming
every source. With them our pipeline reproduces USGS's Big Bear assessment (threshold
ratio 0.96, against 0.56 on Sentinel-2 with estimated severity); see design.md.

The assessment is found by location (usgs_check.by_location). Because these inputs replace
ours, a candidate must share at least MIN_OVERLAP of our perimeter; a nearby point is not
enough. Found inputs are unpacked to data/usgs/<code>/inputs/ and recorded in the fire
config as "usgs_inputs"; none found records null, and run_fire.py falls back to Sentinel-2
with estimated severity. Never fails: no network or no inputs is a result too.

    python pipeline/usgs_inputs.py fires/big_bear.json
"""

import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from usgs_check import OUT, by_location  # noqa: E402

MIN_OVERLAP = 0.5


def item_files(item_url):
    with urllib.request.urlopen(f"{item_url}?format=json", timeout=120) as r:
        return {f["name"]: f["url"] for f in json.load(r).get("files", [])}


def find(fire):
    """The first overlapping USGS assessment that published inputs, as a config entry."""
    for c in by_location(fire, project=ROOT / "data"):      # WFIGS perimeter from recover.py
        if c["overlap"] < MIN_OVERLAP or not c["item"]:
            continue
        files = item_files(c["item"])
        if "inputs.zip" not in files:
            print(f"{c['code']}: {100 * c['overlap']:.0f}% overlap, but no inputs.zip published")
            continue
        out = OUT / re.sub(r"[^\w.-]", "_", c["code"]) / "inputs"
        out.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(files["inputs.zip"], timeout=600) as r:
            with zipfile.ZipFile(io.BytesIO(r.read())) as z:
                for n in z.namelist():
                    if Path(n).name in ("dnbr.tif", "severity.tif"):
                        (out / Path(n).name).write_bytes(z.read(n))
        if not (out / "dnbr.tif").exists() or not (out / "severity.tif").exists():
            print(f"{c['code']}: inputs.zip has no dnbr.tif/severity.tif")
            continue
        sources = {}
        if "metadata.json" in files:
            meta = json.load(urllib.request.urlopen(files["metadata.json"], timeout=120))
            ds = meta.get("assessment", {}).get("input_datasets", {})
            sources = {k: ds.get(k, {}).get("source") for k in ("dnbr", "severity", "perimeter")}
        rel = lambda p: str(p.relative_to(ROOT)).replace("\\", "/")
        return {"code": c["code"], "item": c["item"], "overlap": c["overlap"],
                "assessment_date": c["assessment_date"],
                "dnbr": rel(out / "dnbr.tif"), "severity": rel(out / "severity.tif"),
                "sources": sources}
    return None


def main():
    config = Path(sys.argv[1])
    fire = json.loads(config.read_text())
    try:
        found = find(fire)
    except Exception as e:          # informative only; the run falls back to Sentinel-2
        print(f"USGS inputs: lookup failed ({e}); using Sentinel-2")
        found = None
    fire["usgs_inputs"] = found
    config.write_text(json.dumps(fire, indent=1) + "\n")
    if found:
        print(f"USGS inputs: {found['code']} (assessed {found['assessment_date']}, "
              f"{100 * found['overlap']:.0f}% perimeter overlap): dNBR from "
              f"{found['sources'].get('dnbr') or '?'}, severity from {found['sources'].get('severity') or '?'}")
    else:
        print("USGS inputs: none published for this fire; using Sentinel-2 and estimated severity")


if __name__ == "__main__":
    main()
