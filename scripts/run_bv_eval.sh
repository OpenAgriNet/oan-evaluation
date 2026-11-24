#!/usr/bin/env bash
# Already-running isolated bot APIs and mocks are configured by the caller.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
D=data/combined_20261123
ASSETS=data/history_window_20261120
RUN_LABEL=${RUN_LABEL:-run_$(date -u +%Y%m%d_%H%M%S)}
JUDGE_PREFIX=${JUDGE_PREFIX:-$RUN_LABEL}
LANGUAGES=${LANGUAGES:-English Hindi Marathi Gujarati Malayalam Kannada Tamil Telugu Bengali Assamese Odia Punjabi Maithili}
VARIANTS=${VARIANTS:-before,state}
export BENCHMARK_GATEWAY=${BENCHMARK_GATEWAY:?Set the shared bounded gateway URL}
export BENCHMARK_LIMIT=${BENCHMARK_LIMIT:-12}
export BENCHMARK_SESSION_CONCURRENCY=${BENCHMARK_SESSION_CONCURRENCY:-3}
export BENCHMARK_VARIANTS_FILE=${BENCHMARK_VARIANTS_FILE:?Set your variants.local.json path}
export JUDGE_CONTRACT=core JUDGE_TOTAL_TOKENS=85000
export JUDGE_CORE_FILE="$PWD/$ASSETS/judge_core.json" JUDGE_GROUPS="$PWD/$D/groups.json"
export BENCHMARK_ENGLISH_REFERENCE="$PWD/$D/$RUN_LABEL/text/before/english"
mkdir -p "$D/$RUN_LABEL"
curl -fsS http://127.0.0.1:17920/health | "$PYTHON" -c \
  'import json,sys; h=json.load(sys.stdin); assert h["mock"] and not h["forwards"]'

judge() {
  local language=$1 pids=()
  for arm in ${VARIANTS//,/ }; do
    BENCHMARK_JUDGE_VARIANTS=$arm "$PYTHON" inference/run_capability_judges.py \
      --run-dir "$D/$RUN_LABEL" --prefix "$JUDGE_PREFIX" --languages "$language" \
      > "$D/$RUN_LABEL/judge_${arm}_${language}.log" 2>&1 &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do wait "$pid"; done
  echo "$(date -u +%FT%TZ) judged $language"
}

judges=()
for language in $LANGUAGES; do
  echo "$(date -u +%FT%TZ) inference $language"
  pids=()
  for arm in ${VARIANTS//,/ }; do
    "$PYTHON" inference/run_capability_comparison.py --repos text --variants "$arm" \
      --languages "$language" --label "$RUN_LABEL" --data-dir "$D" --fixture-assets-dir "$ASSETS" \
      > "$D/$RUN_LABEL/infer_${arm}_${language}.log" 2>&1 &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do wait "$pid"; done
  # Later languages need the completed English inference as their reference.
  if [[ $language == English ]]; then judge "$language"; else judge "$language" & judges+=("$!"); fi
done
for pid in "${judges[@]}"; do wait "$pid"; done
echo "$(date -u +%FT%TZ) ALL DONE: $D/$RUN_LABEL; judge prefix $JUDGE_PREFIX"
