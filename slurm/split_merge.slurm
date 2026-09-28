#!/bin/bash
# After the piece array of a split fire (submitted by split_prepare.slurm, afterok):
# merge the pieces into <project>_split and score it against the fire's USGS assessment.
#
#   sbatch --dependency=afterok:<array job> --export=ALL,PROJECT=data/wapiti_baer_baer_lidar1m,CONFIG=fires/wapiti.json slurm/split_merge.slurm
#
# Results: <project>_split/exports (segments, basins, outlets), <project>_split/split_meta
# (plan, per-piece timings and memory, steps.json), <project>_split/usgs_check.json and the
# USGS comparison in this log.
#SBATCH --job-name=dfh-merge
#SBATCH --partition=tiny
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=32G
#SBATCH --output=logs/merge_%j.log

set -euo pipefail
: "${PROJECT:?set PROJECT}"
: "${CONFIG:?set CONFIG, the fire's fires/*.json}"
cd "$HOME/scratch/Debris-Flow-Hunters"
source .venv/bin/activate
echo "merge ${PROJECT} on $(hostname), $(date)"
python pipeline/split_fire.py merge "$PROJECT"
python pipeline/usgs_check.py "$CONFIG" "${PROJECT}_split"   # finds, fetches and compares
cat "${PROJECT}_split/split_meta/steps.json"
echo "done $(date)"
