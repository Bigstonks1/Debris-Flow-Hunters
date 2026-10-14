#!/bin/bash
# A 1 m (or any) LiDAR run of a fire through the watershed split, all from one submit:
#   1. prepare_fire.py: inputs on the LiDAR grid (the fire's RECOVER LiDAR, averaged to RES m)
#   2. split_fire.py preprocess | plan: whole-fire preprocess, cut into pieces
#   3. submits split_pieces.slurm as an array (one piece per task; each task builds its own
#      piece, then assesses it) and split_merge.slurm after it (afterok), which merges and
#      scores against USGS.
#
# CONFIG is a fires/*.json with "recover_lidar" (the 1 m LiDAR file) and, for DNBR/SEVERITY
# baer, "baer_dir" (the BAER package); usgs uses the inputs USGS published (usgs_inputs.py).
#
#   mkdir -p logs
#   sbatch --export=ALL,CONFIG=fires/wapiti.json,RES=1 slurm/split_prepare.slurm
#   sbatch --export=ALL,CONFIG=fires/wapiti.json,RES=1,DNBR=usgs,SEVERITY=usgs slurm/split_prepare.slurm
#   sbatch --export=ALL,CONFIG=fires/wapiti.json,RES=1,SKIP_PREP=1 slurm/split_prepare.slurm   # reuse the preprocess
#
# This job never runs assess, so it needs far less than the whole-fire 1 m run: Wapiti's
# whole-grid preprocess (~2 billion cells) peaked at 47.8 GB, its largest piece at 40 GB.
# PIECE_MEM/PIECE_PARTITION/PIECE_TIME size the array tasks; if a piece runs out of
# memory, resubmit with smaller pieces: --export=ALL,RES=1,PLAN_ARGS="--target-km2 30"
#SBATCH --job-name=dfh-prep
#SBATCH --partition=short
#SBATCH --time=12:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=64G
#SBATCH --output=logs/prep_%j.log

set -euo pipefail
: "${CONFIG:?set CONFIG, e.g. --export=ALL,CONFIG=fires/wapiti.json,RES=1}"
RES="${RES:-1}"
DNBR="${DNBR:-baer}"            # baer | usgs
SEVERITY="${SEVERITY:-$DNBR}"   # baer | usgs | estimate
PIECE_MEM="${PIECE_MEM:-64G}"   # Wapiti 1 m: largest piece 333 M cells, 40 GB peak
PIECE_PARTITION="${PIECE_PARTITION:-short}"
PIECE_TIME="${PIECE_TIME:-12:00:00}"
cd "${SLURM_SUBMIT_DIR:-$HOME/scratch/Debris-Flow-Hunters}"   # the repository the job was submitted from
source .venv/bin/activate
export DFH_STAGE_TIMEOUT=$((11 * 3600))

KEY=$(python -c "import json,sys; sys.path.insert(0,'pipeline'); from fires import fire_key; print(fire_key(json.load(open(sys.argv[1]))))" "$CONFIG")
PROJECT="data/${KEY}_${DNBR}_${SEVERITY}_lidar${RES}m"      # prepare_fire.py's naming
echo "${CONFIG} at ${RES} m -> ${PROJECT} on $(hostname), $(date)"

if [ -z "${SKIP_PREP:-}" ]; then
    python pipeline/prepare_fire.py "$CONFIG" --dnbr "$DNBR" --severity "$SEVERITY" --lidar-res "$RES"
    python pipeline/split_fire.py preprocess "$PROJECT"
fi
python pipeline/split_fire.py plan "$PROJECT" ${PLAN_ARGS:-}   # e.g. PLAN_ARGS="--target-km2 30"
rm -rf "$PROJECT"/split/piece_* "$PROJECT"/split/run_*.json   # the tasks rebuild their pieces

N=$(python -c "import json,sys; print(len(json.load(open(sys.argv[1]))['pieces']))" "$PROJECT/split/plan.json")
jid=$(sbatch --parsable --array=0-$((N-1)) -p "$PIECE_PARTITION" --mem="$PIECE_MEM" -t "$PIECE_TIME" \
      --export=ALL,PROJECT="$PROJECT",BUILD=1 slurm/split_pieces.slurm)
mid=$(sbatch --parsable --dependency=afterok:"$jid" \
      --export=ALL,PROJECT="$PROJECT",CONFIG="$CONFIG" slurm/split_merge.slurm)
echo "submitted ${N} pieces as array ${jid} (each builds its own), merge ${mid}; $(date)"
