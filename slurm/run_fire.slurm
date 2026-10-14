#!/bin/bash
# One fire, end to end, on a compute node: pipeline/run_fire.py (config from WFIGS,
# USGS's published BAER inputs or Sentinel-2, USGS DEM, STATSGO soils, wildcat, USGS
# check). Submitted by app/server.py
# with --runner slurm, or by hand from the project root:
#
#   mkdir -p logs
#   sbatch --export=ALL,FIRE_ID=2025-IDPAF-005844 slurm/run_fire.slurm       # Unique Fire ID
#   sbatch --export=ALL,FIRE="Rush",YEAR=2025,STATE=ID slurm/run_fire.slurm
#
# The pipeline downloads its inputs, so the node needs outbound HTTPS. The check below
# fails in seconds, naming the host, instead of hanging mid-pipeline.
#
# Sizing: a 10 m run of Wapiti (522 km2, the largest so far) peaks at a few GB and takes
# about 15 minutes, well inside tiny's 6 hours. 1 m LiDAR is a different job
# (slurm/split_prepare.slurm).
#SBATCH --job-name=dfh-fire
#SBATCH --partition=tiny
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --output=logs/fire_%j.log

set -euo pipefail
if [ -z "${FIRE_ID:-}" ]; then
    : "${FIRE:?set FIRE_ID (e.g. 2025-IDPAF-005844) or FIRE and YEAR}"
    : "${YEAR:?set YEAR}"
fi
cd "${SLURM_SUBMIT_DIR:-$HOME/scratch/Debris-Flow-Hunters}"   # the repository the job was submitted from
source .venv/bin/activate
# A private numba cache per job: the app submits several fires at once, and jobs building the
# shared cache at the same time corrupt it
export NUMBA_CACHE_DIR="${TMPDIR:-/tmp}/numba_${SLURM_JOB_ID}"
trap 'rm -rf "$NUMBA_CACHE_DIR"' EXIT

echo "fire: ${FIRE_ID:-${FIRE:-} ${YEAR:-} ${STATE:-}} on $(hostname), $(date)"
# WFIGS, USGS's assessment service, Sentinel-2 search and imagery, USGS DEM tiles,
# STATSGO and USGS assessments (ScienceBase), RECOVER bundles, LANDFIRE (EVT when a fire
# has no RECOVER bundle)
for url in https://services3.arcgis.com https://services.arcgis.com \
           https://earth-search.aws.element84.com/v1 \
           https://sentinel-cogs.s3.us-west-2.amazonaws.com https://prd-tnm.s3.amazonaws.com \
           https://www.sciencebase.gov https://giscenter-sl.isu.edu https://lfps.usgs.gov; do
    if ! curl -s -o /dev/null -m 20 "$url"; then
        echo "no network: cannot reach $url from $(hostname); the pipeline downloads its inputs" >&2
        exit 3
    fi
done

if [ -n "${FIRE_ID:-}" ]; then
    python pipeline/run_fire.py --id "$FIRE_ID"
else
    python pipeline/run_fire.py "$FIRE" "$YEAR" ${STATE:+"$STATE"}
fi
echo "done $(date)"
