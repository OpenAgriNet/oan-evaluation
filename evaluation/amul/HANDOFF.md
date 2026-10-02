# Amul eval — handoff prompt

Paste everything below the line into a new agent chat to continue the work.

---

You are continuing work on automated evaluation of the **Amul dairy assistant** ("Sarlaben") for
[issue #1](https://github.com/OpenAgriNet/oan-evaluation/issues/1) in `OpenAgriNet/oan-evaluation`.
The agent answers Gujarati farmers about AI (artificial insemination) technician visits, veterinary health visits,
milk collection records, union/government schemes and animal health. It reasons in English; a translator turns the
English answer into Gujarati. We capture conversations from an **eval-only copy** of the agent and score them with
metrics posted to Langfuse.

Read `evaluation/amul/METRICS_STATUS.md` first: it is the maintained source of truth (metric status, decisions,
findings, problem IDs like G1/X1/S6, backlog). Keep it updated whenever a metric changes.

## Repo, branch, PR
- Work branch: `amul-eval-capture`, pushed to the fork `nexus69420/oan-evaluation` (remote `fork`).
  [PR #2](https://github.com/OpenAgriNet/oan-evaluation/pull/2) targets `OpenAgriNet/oan-evaluation`.
- Aggregate dev branch of the repo is `common_evals_dev`.
- **Never commit or push without asking the user first, every time.** Never print or copy secret values.

## Hard constraints
- Do not touch `/home/aicloud/docs-pipeline`, `VECTOR_STORE_BACKEND` (marqo), or the shared `amul_app` container
  (H100 port 8000). Only `amul_app_eval` (127.0.0.1:8095) is ours.
- LiteLLM master key / salt / UI password / Postgres password live in `/amulpfsdata/litellm/.env`; never copy them.
- No real bookings, no live writes. Booking stays on the local stub. `check_loan_eligibility` stays blocked
  (it issues a loan code and can SMS).
- Dev servers: read-only unless the user approves otherwise.

## Machines
- **H100** (`ssh amul-gpu-1`): repo copy at `/home/aicloud/oan-evaluation` (Python: `.venv/bin/python`,
  env file `inference/.env` with `LANGFUSE_*`, `LITELLM_API_KEY`, `SCENARIO_GEMMA_BASE_URL`, `SCENARIO_GEMMA_MODEL`).
  Working dir `/home/aicloud/amul-eval-capture/` holds `container.env` (eval copy env; backup `container.env.pre-x1`),
  `run.env` (capture env), turn caches `seed-gu-capture-{2,3}.turns.json`, `farmer-context-capture2.json`,
  and helper scripts `_rescore.sh` (capture 2) and `_score3.sh` (capture 3; pass `--post` to write).
  The H100 copy is NOT a git checkout of this branch's latest files; after editing locally, scp changed files to it
  and run `sed -i 's/\r$//'` on them.
- **Dev servers (UAT)** via a jump host; SSH aliases `amul-dev-jump`, `amul-dev-vm2-db`, `amul-dev-vm4-dashboard`,
  `amul-dev-vm5-ai-backend`, `amul-dev-vm6-ai-vecdb` (copy the `Host amul-*` blocks from the old laptop's
  `~/.ssh/config` and the key). vm5 runs the Beckn stack: seeker `amul-bap-seeker` on `10.5.25.36:16240`,
  adapter `amul-onix-adapter` on `:16250`, BPPs, dev `amul_app`. vm6 hosts dev Langfuse and docs-pipeline.
- **Langfuse**: https://langfuse.dev.amulai.in, project `cml0dzqbw0006p207qemlwqz9`, dataset `amul-seed-questions`
  (75 items), runs `seed-gu-capture-2` (old, tools failed) and `seed-gu-capture-3` (current).
- **LiteLLM judge model**: `gemma-4-31b-it` (strip the `openai/` prefix from `SCENARIO_GEMMA_MODEL`); requests need
  a `User-Agent` header.

## Eval copy setup (H100 `amul_app_eval`)
- Same image as the agent, `--network amul-network`, `-p 127.0.0.1:8095:8000`, `--env-file container.env`,
  mounts `mock_server/amul_pashu_mock/server.py` read-only at `/opt/amul-pashu-mock/server.py`; the container command
  starts the stub on 127.0.0.1:5695 then uvicorn. Restart it (`docker restart amul_app_eval`) after changing the stub.
- `ENABLE_NETWORK=true`. `AMUL_NETWORK_URL` and `VISTAAR_SEEKER_URL` point at the dev seeker
  `http://10.5.25.36:16240` (read-only discovery: vet docs, union schemes, Vistaar). `BECKN_BAP_CALLER_URL` (adapter)
  stays unresolvable so farmer/milk/animal/technician reads fall back to the stub; `AMUL_BOOKING_BPP_URL` is the stub.
- Fixture farmer: mobile 9000000001, Banaskantha, union `banas`, codes `EVAL-UNION/EVAL-SOC/EVAL-FARMER`,
  technicians `EVAL-TECH-1` Rameshbhai Chaudhary, `EVAL-TECH-2` Sureshbhai Patel.

## Capture and scoring flow
1. Capture: on the H100, load `run.env` (guards: BASE_URL must be :8095, phone 9000000001, eval chat key set) and run
   `inference/run_sequential_scenario.py` (reads `inference/amul_seed_questions.csv`, 75 turns / 28 sessions).
   The log prints `run_uuid`; Langfuse session ids are `{run_uuid}_gu_{session_id}`, traces named `chat.translation`.
2. Link: create a dataset run by POSTing `/api/public/dataset-run-items` for each seed row (match traces by session
   and question text; dataset items keyed by `input.question_id`). Link the same day (the experiment table only shows
   traces from the 24 h before the run row). Wait a minute before scoring; run items list lazily.
3. Score: `python -m evaluation.amul.run_amul_eval --run <run> --env-file inference/.env --prod-reference
   --glossary-container amul_app_eval --judges grounding,gujarati_language,prod_reference [--post]`.
   Use `--cache` / `--from-cache` to avoid refetching. For capture 2 add
   `--farmer-context /home/aicloud/amul-eval-capture/farmer-context-capture2.json` (old technician names).

## Code map
- `evaluation/amul/langfuse_io.py`: Langfuse client (`post_score` uses deterministic ids sha1(trace:name), so
  re-posting upserts), `build_turn` (TOOL observations = tool calls; `query_pretranslation` = English question;
  `stream_translation` inputs = English answer; trace output = Gujarati reply), `load_run`.
- `evaluation/amul/checks.py`: code metrics `expected_tool_called`, `ticket_consistency`, `tool_arguments`,
  `output_hygiene`, `language_mixing` (1-5), `glossary_adherence`, `latency_s`.
- `evaluation/amul/glossary.py`: runs the agent's own `get_mini_glossary_for_text(threshold=0.90, max_terms=40)`
  inside the container via `docker exec`; inflection-tolerant adherence.
- `evaluation/amul/judge.py` + `assets/prompts/metrics/amul/*.md`: Gemma judges `grounding` (source_alignment,
  no_fabrication, citation_accuracy), `gujarati_language` (grammar, terminology, language_purity, fluency, overall),
  `prod_reference` (reference_agreement). Whole numbers 1-5 or null; tool outputs passed in full (up to 120k chars).
- `evaluation/amul/prod_reference.py`: references from successful knowledge-tool results in other Amul Agent Dev
  traces, for turns whose own lookups failed.
- `evaluation/amul/run_amul_eval.py`: CLI (dry run unless `--post`).
- `mock_server/amul_pashu_mock/server.py`: farmer/technician fixture, 60-day milk records filtered by
  `fromdate`/`todate`, booking stub (never forwards); `smoke_test.py` tests it.

## Where things stand (capture 3)
source_alignment 4.96, no_fabrication 4.87, citation_accuracy 5.00 (8 scored), grammar 4.77, terminology 4.69,
language_purity 4.59, fluency 3.72, gujarati_language 4.05, glossary_adherence 0.95, expected_tool_called 28/28,
tool_arguments 20/21, ticket_consistency 15/17, output_hygiene 75/75, latency 13.6 s.
Judges are not calibrated yet. Agent bugs found are listed under "Findings to report" in METRICS_STATUS.md.

## Open items
- User manual actions in Langfuse: deactivate the UI evaluator `gujarati_language` (G3); hide the agent's label
  columns `turn_outcome`, `pipeline_profile`, `served_tier` (X5).
- Backlog in METRICS_STATUS.md: 15 not-started metrics (process, safety, usefulness, Amul-specific), task-success
  judge, translation meaning check, per-turn expected tools (E1), calibration (X2), golden answers (X3), prompt/profile
  logging asks (S4/S5), deterministic milk-total check, loan stub, Bharat Vistaar leg timing out on dev too.

## Gotchas
- On Windows PowerShell, inline Python/bash over ssh breaks on quotes and pipes: write a temp script, scp it, run it,
  delete it. Strip CRs on .sh files.
- Langfuse v3.150: the `/api/public/scores` traceId filter is ignored (use the trace detail's `scores`); score
  DELETE is queued (~1/min); there is no evaluator API; API scores and UI evaluator scores make separate columns.
