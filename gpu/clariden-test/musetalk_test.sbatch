#!/bin/bash
# Test MuseTalk (foto+voce -> clip) su Clariden. Lanciare dal login node:
#   sbatch --account=<IL_TUO_ACCOUNT> gpu/clariden-test/musetalk_test.sbatch
#
# Primo run: download 15GB + compile mmcv -> partition "normal" (3h di margine).
# Run successivi (tutto in cache su scratch): "debug" (max 1h30) basta e avanza:
#   sbatch --account=... --partition=debug --time=00:30:00 musetalk_test.sbatch
#SBATCH --job-name=laura_musetalk_test
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --time=03:00:00
#SBATCH --partition=normal
#SBATCH --environment=musetalk
#SBATCH --output=/iopsstor/scratch/cscs/%u/musetalk/job_%j.out
#SBATCH --error=/iopsstor/scratch/cscs/%u/musetalk/job_%j.out
set -eo pipefail
echo "Job $SLURM_JOB_NAME su $SLURM_NODELIST — $(date)"
export WORK="/iopsstor/scratch/cscs/$USER/musetalk"
bash "$WORK/setup_and_run_clariden.sh"
