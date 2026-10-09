#!/bin/bash
# One fire of a batch per array task (pipeline/batch.py). Submitted by
#   python pipeline/batch.py run <batch> --runner slurm [--max-running 20]
# from the repository folder. Two modes, set by PHASE:
#   PHASE=wildcat  (default, staged) inputs were prepared on the login node; the task runs
#                  only wildcat (preprocess, assess, export), which needs no network. For
#                  clusters whose compute nodes have no internet, such as Lemhi.
#   PHASE=all      the whole fire, downloads included (--compute-internet); checks first that
#                  every data host is reachable.
#
# Task N is fire N of data/batches/<batch>/fires.tsv. Sizing: a 10 m fire uses mostly one
# core and at most ~2.4 GB (Wapiti, 522 km2), so 4 cores and 16 GB per task; Slurm fits
# several tasks on one 40-core, 192 GB node. Logs: data/batches/<batch>/logs/<key>.<phase>.log.
#SBATCH --job-name=dfh-batch
#SBATCH --partition=tiny
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --output=logs/batch_%A_%a.log

set -euo pipefail
: "${BATCH:?set BATCH, the batch name under data/batches}"
: "${SLURM_ARRAY_TASK_ID:?submit as an array: --array=1-N}"
PHASE="${PHASE:-wildcat}"
cd "${SLURM_SUBMIT_DIR:-$HOME/scratch/Debris-Flow-Hunters}"   # the repository the job was submitted from
source .venv/bin/activate
# A private numba cache per task: tasks building the shared cache (next to the library on the
# shared file system) at the same time corrupt it. Costs ~40 s of compiling per task.
export NUMBA_CACHE_DIR="${TMPDIR:-/tmp}/numba_${SLURM_JOB_ID}"
trap 'rm -rf "$NUMBA_CACHE_DIR"' EXIT

echo "batch ${BATCH}, fire ${SLURM_ARRAY_TASK_ID}, phase ${PHASE} on $(hostname), $(date)"
if [ "$PHASE" = "all" ]; then
    # The whole run downloads its inputs: fail in seconds, naming the host, if one is unreachable
    for url in https://services3.arcgis.com https://services.arcgis.com \
               https://earth-search.aws.element84.com/v1 \
               https://sentinel-cogs.s3.us-west-2.amazonaws.com https://prd-tnm.s3.amazonaws.com \
               https://www.sciencebase.gov https://giscenter-sl.isu.edu https://lfps.usgs.gov; do
        if ! curl -s -o /dev/null -m 20 "$url"; then
            echo "no network: cannot reach $url from $(hostname); use the staged batch" >&2
            exit 3
        fi
    done
fi

python pipeline/batch.py task "$BATCH" "$SLURM_ARRAY_TASK_ID" --phase "$PHASE"
echo "done $(date)"
