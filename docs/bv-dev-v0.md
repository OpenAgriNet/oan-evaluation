# BharatVistaar evaluation package: bv-dev-v0

This branch packages the combined November benchmark and the code needed to run
it. It does not contain run outputs, real credentials, production trace exports,
or the product API itself. Start isolated product APIs from the branches you want
to compare; never point write/OTP workflows at real farmer services.

## Benchmark

`data/combined_20261123/text.csv` contains 527 questions in 208 conversations,
judged as 263 original units in 13 languages. Source counts:

| Source | Questions | Judge units |
|---|---:|---:|
| current-long | 120 | 60 |
| current-short | 53 | 17 |
| mined regression cases | 91 | 39 |
| full benchmark without overlap | 211 | 141 |
| FFS | 52 | 6 |

Every multi-question conversation uses the simulated farmer in `sim_specs.json`.
Its `user_messages` are sent in order; `facts` provide replies to extra questions.
Single-question cases go straight to the bot. `groups.json` carries each unit's
expected tools, skills and judge notes. The judge uses the candidate's IST date.
Tool outputs and retries are recovered from Redis by call ID, not invented from
assistant text. Frozen judge instructions are in
`data/history_window_20261120/judge_core.json`.

## Install and validate

Use Python 3.12 in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-bv-dev.txt
python data/combined_20261123/build_combined.py
python -m pytest -q data_transformation/test_capability_sessions.py \
  data_transformation/test_collate_all_models.py inference/test_capability_export.py \
  inference/test_bounded_gemma_proxy.py inference/test_simulated_voice.py \
  mock_server/beckn_search_proxy/test_current_search.py \
  mock_server/beckn_search_proxy/test_current_status.py \
  mock_server/beckn_search_proxy/test_khc_discovery.py \
  mock_server/beckn_search_proxy/test_pmfby_contract.py \
  mock_server/ffs_mock/test_mock_ffs.py mock_server/capability_aux/test_geocodes.py
```

Rebuilding is local and uses the included source fixtures and translation cache;
regenerating translations/profiles calls Gemma and is optional. The packaged
Gemma profile uses `google/gemma-4-31b-it`. Dependencies are not a full environment
lock; use a project-specific environment rather than mixing existing environments.

## Mocks and live read-only services

Run services from the repository root in separate terminals. Example ports:

```bash
# Pure fixtures: no network forwarding is needed for this config.
PROXY_PORT=17897 GRIEVANCE_PORT=17898 SATHI_PORT=17899 \
  python mock_server/beckn_search_proxy/server.py
FIXTURE_CALLBACK_PORTS=8060,8063 python -m mock_server.capability_aux.server
python -m mock_server.ffs_mock.mock_ffs 17920
```

FFS is always synthetic and never forwards. Its fake Backend is bundled locally,
including the fixture AES key/IV, OTP, land records, stock and booking responses.
The benchmark uses farmer ID `99000000001`, state 24, OTP `246810`; the unregistered
ID, wrong OTP/state and retailer-zero booking failure are deliberate test cases.
Configure the product APIs' `FFS_BASE_URL=http://127.0.0.1:17920`,
`FFS_ESA_KEY` as 32 `K` characters, and `FFS_ESA_IV` as 16 `I` characters.

For the live-read-only configuration used by the current comparison, set
`PROXY_CONFIG=data/history_window_20261120/mock_config.json` and
`BENCHMARK_NETWORK_ENV_FILE=/absolute/path/to/your/private-network.env` when
starting the proxy. This forwards public search calls to the configured network;
personal status and write workflows remain mocked. The private file supplies
`BAP_ENDPOINT` and optional `BAP_ID`, `BAP_URI`, `BPP_ID`, `BPP_URI`; it is not
included here. Missing configuration fails; there is no production fallback.
Live search results change, so live and pure-fixture scores are not interchangeable.
Marqo, geocoding and other services used directly by the product API must also be
configured for that API. `mock_server/capability_aux/imd.sql` is a local IMD fixture.

## Inference and judging

Configure both bot APIs to use one shared bounded Gemma gateway. Start it with
an explicitly selected, authorized self-hosted endpoint:

```bash
BENCHMARK_GEMMA_UPSTREAM=http://127.0.0.1:8867 BENCHMARK_LIMIT=12 \
  python -m uvicorn inference.bounded_gemma_proxy:app --host 127.0.0.1 --port 18869
```

Use `data/combined_20261123/models.yaml` as an example of the **bot's** model
configuration. Do not start separate gateways for inference, simulators and
judges: the concurrency limit must cover all of them. Production access requires
separate explicit authorization and the gateway's production opt-in.

`bash inference/gemma_load_monitor.sh` records active counts every five seconds.
Set `BENCHMARK_MONITOR_PORTS` to all gateways that share your resource cap;
unavailable gateways and over-cap observations are explicitly flagged.

Copy `data/combined_20261123/variants.example.json` to `variants.local.json`.
Set the product worktree paths, API ports, Redis container/DB and
`redis_key_prefix` to exactly match each running isolated API. Export requires
Docker CLI access to that Redis container. Keep the arm names `before` and `state`
for the included comparison script. The runner records commit and fixture hashes;
use clean product commits or save your exact diff separately.

```bash
export BENCHMARK_VARIANTS_FILE="$PWD/variants.local.json"
export BENCHMARK_GATEWAY=http://127.0.0.1:18869
export BENCHMARK_LIMIT=12
export PYTHON="$PWD/.venv/bin/python"
# English first: it is the reference for other languages. Omit LANGUAGES for all 13.
LANGUAGES='English Hindi' RUN_LABEL=run_bv_v0 JUDGE_PREFIX=bv_v0 \
  setsid bash scripts/run_bv_eval.sh > bv-eval.log 2>&1 &
EVAL_PGID=$!
echo "$EVAL_PGID"
```

The script runs both arms against the same fixtures. English judging completes
first; subsequent judging overlaps the next language's inference. It waits for
specific child PIDs, never for background servers. Check per-stage logs and output
timestamps before calling a run healthy; a living controller alone is insufficient.

Stop with `kill -- -"$EVAL_PGID"`. Check for this run's leftover
`run_simulated_scenario.py` processes by PID and output path, and stop those too.
Verify the bounded gateway's active count returns to its expected background load.
Do not kill other evaluations or shared services.

Compare completed outputs with:

```bash
python data/combined_20261123/compare_combined.py bv_v0 english hindi
```

Outputs live under the chosen `run_*` directory and `data/models/`; keep them local.
Partial judge output is preserved for inspection rather than silently overwritten.

## Data review

Production-derived regression cases retain the question intent and judging
expectations, not trace IDs, raw production responses, or user-session exports.
Ambiguous sample mobiles were replaced consistently across fixtures, translations
and profiles. Existing canonical test mobiles, OTPs, names and records are
synthetic; public crop/location/scheme names are retained. Stored JWT/password/
authorization samples were removed from the inherited Postman collection. This
cleans the branch contents, not old Git history. Never use these fixtures against
real personal-data or booking endpoints.
