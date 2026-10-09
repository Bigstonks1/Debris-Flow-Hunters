#!/bin/bash
# One piece of a split fire per array task: wildcat assess + export on
# <project>/split/piece_NN (pipeline/split_fire.py). With BUILD=1 in --export (what
# split_prepare.slurm does) each task first cuts its own piece, so the build runs in
# parallel. By hand, plan and build first, merge after:
#
#   python pipeline/split_fire.py plan  "$PROJECT"        # login node or a small job
#   python pipeline/split_fire.py build "$PROJECT"
#   N=$(ls -d "$PROJECT"/split/piece_* | wc -l)
#   mkdir -p logs
#   jid=$(sbatch --parsable --array=0-$((N-1)) --export=ALL,PROJECT="$PROJECT" slurm/split_pieces.slurm)
#   sbatch --dependency=afterok:$jid --wrap "python pipeline/split_fire.py merge $PROJECT"
#
# Sizing: at 10 m a Wapiti piece peaks under 1 GB; at 1 m the largest piece is ~20% of
# the whole grid (~400 M cells for Wapiti, like Grizzly's full 1 m run at 53 GB), so a
# regular 192 GB node suffices. Lower --target-km2 in `plan` for smaller pieces.
#SBATCH --job-name=dfh-piece
#SBATCH --partition=short
#SBATCH --time=06:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=96G
#SBATCH --output=logs/piece_%A_%a.log

set -euo pipefail
: "${PROJECT:?set PROJECT, e.g. --export=ALL,PROJECT=data/wapiti_baer_baer_lidar1m}"
cd "${SLURM_SUBMIT_DIR:-$HOME/scratch/Debris-Flow-Hunters}"   # the repository the job was submitted from
source .venv/bin/activate
# A private numba cache per task: tasks building the shared cache at the same time corrupt it
export NUMBA_CACHE_DIR="${TMPDIR:-/tmp}/numba_${SLURM_JOB_ID}"
trap 'rm -rf "$NUMBA_CACHE_DIR"' EXIT
echo "piece ${SLURM_ARRAY_TASK_ID} of ${PROJECT} on $(hostname), $(date)"
python pipeline/split_fire.py run "$PROJECT" --piece "${SLURM_ARRAY_TASK_ID}" ${BUILD:+--build}
echo "done $(date)"
