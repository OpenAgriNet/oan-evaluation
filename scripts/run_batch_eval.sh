#!/usr/bin/env bash
# Transform + evaluate every per-model CSV in a directory, then collate all
# results into one comparison CSV. Use this once you already have one CSV per
# model (e.g. from inference/run_sequential_scenario.py, run once per model) —
# it's a batch shortcut for steps 3-5 in the root README, run over everything
# in CSV_DIR at once, instead of doing them one model at a time by hand.
#
# Required CSV format/arrangement (see README.md's "Batch mode" section):
#   - One CSV per model, all in the same directory.
#   - The CSV's filename stem (without .csv) becomes the model_name — this is
#     also the folder name under data/models/, so make it descriptive
#     (e.g. gemma_v1.csv, qwen3_32b.csv), not something generic.
#   - Each CSV must have the run_sequential_scenario.py / run_simulated_scenario.py
#     output columns: session_id, question_id, question, answer, tool_calls,
#     tool_outputs, TTFT_answer, latency_answer.
#
# Also requires SCENARIO_JUDGE_VLLM_BASE_URL and SCENARIO_JUDGE_VLLM_MODEL to be
# set (no default — see README.md's eval step: never point this at a shared
# GPU endpoint without confirming that specific use first).
#
# Usage:
#   SCENARIO_JUDGE_VLLM_BASE_URL=... SCENARIO_JUDGE_VLLM_MODEL=... \
#       bash scripts/run_batch_eval.sh <csv_dir>

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <csv_dir>" >&2
    exit 1
fi

CSV_DIR="$1"
PYTHON=/mnt/raid/gautam/python_envs/evals/bin/python3
EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG=config.pipeline_config_scenario
RESULTS_CSV="${RESULTS_CSV:-$EVAL_DIR/data/results/all_models.csv}"

cd "$EVAL_DIR"

shopt -s nullglob
csvs=("$CSV_DIR"/*.csv)
if [ ${#csvs[@]} -eq 0 ]; then
    echo "No CSVs found in $CSV_DIR" >&2
    exit 1
fi

model_names=()
for csv in "${csvs[@]}"; do
    model_name=$(basename "$csv" .csv)
    model_names+=("$model_name")

    echo ""
    echo "═══════════════════════════════════════════════════"
    echo " CSV:   $csv"
    echo " Model: $model_name"
    echo "═══════════════════════════════════════════════════"

    echo "[1/2] Transforming CSV..."
    $PYTHON -m data_transformation.transform_per_lang_csv \
        --csv "$csv" \
        --model-name "$model_name" \
        --out-dir "$EVAL_DIR/data/models"

    echo "[2/2] Running evaluation..."
    $PYTHON orchestrate_eval.py \
        --config "$CONFIG" \
        --model-name "$model_name"

    echo "Done: $model_name"
done

echo ""
echo "Collating all models..."
$PYTHON -m data_transformation.collate_all_models --model-names "${model_names[@]}" --out "$RESULTS_CSV"

echo ""
echo "All done. Per-model results in $EVAL_DIR/data/models/, combined CSV at $RESULTS_CSV"
