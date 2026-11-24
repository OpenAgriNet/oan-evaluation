# oan-evaluation

The current BharatVistaar benchmark package is documented in
[docs/bv-dev-v0.md](docs/bv-dev-v0.md): 527 questions, 263 judge units, 13 languages,
simulated farmers, per-unit judging, and local mocks including standalone FFS.

Judge-scored evaluation for OAN chat models. This README covers the scenario-based
workflow: generate real multi-turn conversations against a live bot, score them, and
collate results across as many models as you want to compare. (Comparison-mode
evaluation — scoring two responses head-to-head — and older one-off configs still
exist under `config/`, but aren't covered here.)

Each step below is, deliberately, one file. Assumes you already have a bot running
somewhere (bharat-oan-api, mh-oan-api, a fine-tuned checkpoint behind an
OpenAI-compatible endpoint, whatever) — standing one up is a separate concern, not
covered here.

## 1. Set up the mock server

bharat-oan-api's scheme/grievance/mandi tools call out to a Beckn BPP — `mock_server/`
stands in for one locally so those tool calls don't need a real network.

```bash
cd mock_server && cp .env.example .env   # fill in values
python3 beckn_search_proxy/server.py     # single entry point: search proxy + grievance mock
```

Point bharat-oan-api at it: `BAP_ENDPOINT=http://127.0.0.1:5689`,
`GRIEVANCE_BASE_URL=http://127.0.0.1:5691`. SATHI seed-availability needs
`vistaar_sathi_mock/server.py` started separately (`SATHI_BASE_URL=http://127.0.0.1:5692`)
— optional, only if you're testing that tool.

## 2. Run inference

[`inference/run_sequential_scenario.py`](inference/run_sequential_scenario.py) sends
real multi-turn conversations (grouped by `session_id`, sent in order) against your
running bot and writes answers + latency to a CSV.

```bash
cd inference && cp .env.example .env   # TOKEN + BASE_URL for the bot under test
SCENARIO_INPUT_CSV=per_lang_csvs/english.csv \
SCENARIO_OUTPUT_CSV=output/sequential_scenario/english_<model_name>.csv \
python3 run_sequential_scenario.py
```

(`output/` is gitignored — every inference script writes there by default, see
`inference/README.md`.)

To test multiple models, run this once per model with a different `BASE_URL` (or
`SCENARIO_GEMMA_BASE_URL`/checkpoint) and a distinct `SCENARIO_OUTPUT_CSV` per run —
each becomes its own row in the comparison at step 5. Nothing here is destructive;
re-running for a new model never touches another model's CSV.

## 3. Transform into eval-ready format

[`data_transformation/transform_per_lang_csv.py`](data_transformation/transform_per_lang_csv.py)
turns one model's scenario CSV into the `data.json` shape the evaluator expects
(one row per session, multi-turn sessions collapsed with full prior context).

```bash
python -m data_transformation.transform_per_lang_csv \
    --csv inference/output/sequential_scenario/english_<model_name>.csv \
    --model-name <model_name> \
    --out-dir data/models
```

## 4. Run evals

[`orchestrate_eval.py`](orchestrate_eval.py) with the scenario config scores
`data/models/<model_name>/data.json` and writes `evaluation.json` next to it.

```bash
SCENARIO_JUDGE_VLLM_BASE_URL=http://<host>:<port>/v1 \
SCENARIO_JUDGE_VLLM_MODEL=<model-served-there> \
python orchestrate_eval.py --config config.pipeline_config_scenario --model-name <model_name>
```

No default endpoint is baked in on purpose — this box's vLLM containers include shared
production instances that must never be hit without explicitly confirming, each time,
that the specific one you're pointing at is OK to use for this. Pick one and set both
env vars yourself; the config will refuse to import otherwise.

Edit [`config/pipeline_config_scenario.py`](config/pipeline_config_scenario.py) to
change which metrics run or which judge model scores them — it's not tied to any one
model being evaluated.

## 5. Collate results into one CSV

[`data_transformation/collate_all_models.py`](data_transformation/collate_all_models.py)
stacks every model's `evaluation.json` under `data/models/` into one wide CSV — one
row per (model, sample), one column per metric score — ready to open, pivot, or chart.

```bash
python -m data_transformation.collate_all_models
# -> data/results/all_models.csv
```

Run steps 2–5 once per model you want in the comparison; step 5 always picks up
everything that's landed in `data/models/` so far.

## Batch mode: evaluating several models you've already generated answers for

If you already have several models' answers sitting as CSVs (step 2 done for each,
separately, maybe on different days) and just want to transform + evaluate + collate
all of them in one go, use
[`scripts/run_batch_eval.sh`](scripts/run_batch_eval.sh) instead of repeating steps
3–5 by hand for each one:

```bash
bash scripts/run_batch_eval.sh <csv_dir>
```

This runs steps 3, 4, and 5 for every CSV in `<csv_dir>`, then step 5's collation once
at the end. For it to work, the CSVs need to be arranged like this:

- **One CSV per model, all in the same directory.**
- **The filename (without `.csv`) becomes the model_name** — this is also the folder
  name under `data/models/`, so name them for what they are (`gemma_v1.csv`,
  `qwen3_32b.csv`), not something generic.
- **Each CSV needs the columns step 2 already produces**: `session_id`, `question_id`,
  `question`, `answer`, `tool_calls`, `tool_outputs`, `TTFT_answer`, `latency_answer`.

## Data

Question-set fixtures (`inference/mh_evaluation_questions.csv`,
`inference/benchmark_questions.csv`) are tracked. Everything generated by running the
pipeline is gitignored, wherever it's produced: `inference/output/` (raw inference
CSVs), `data/models/` (per-model transformed/scored JSON), `data/results/` (the final
collated comparison CSV), `data/production_eval/`. Regenerate by rerunning the steps
above — don't commit any of it. Older one-off datasets live in
`/mnt/raid/gautam/adhoc/oan-evaluation-data/`, kept but not part of this repo.

## Other configs and scripts

`config/pipeline_config_goldenset_compare.py` (comparison-mode: two responses judged
head-to-head) and `config/pipeline_config_legacy.py` still work but aren't part of
this workflow. Older one-off scripts (goldenset transforms, fine-tuned-checkpoint
runners, per-model conversion scripts superseded by the generic ones above) were moved
to `/mnt/raid/gautam/adhoc/oan-evaluation-scripts/` — kept for reference, not part of
this repo.
