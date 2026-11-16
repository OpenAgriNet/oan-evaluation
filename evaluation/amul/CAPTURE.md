# Sequential seed capture for API issue #510

`python -m evaluation.amul.capture_run` has two commands: `plan` reads the existing
dataset and writes a local manifest; `capture` sends real requests to the selected
eval app and links their existing traces into a new Langfuse dataset run. Neither
command judges replies or posts scores. This workflow uses the self-hosted
Langfuse v3 [dataset-run-items API](https://github.com/orgs/langfuse/discussions/7083),
not a new SDK experiment wrapper that would create replacement traces.

## Plan without app requests

```sh
python -m evaluation.amul.capture_run plan \
  --env-file /private/path/to/dev-langfuse.env \
  --run seed-gu-turn-v1-510-UNIQUE \
  --chat-url http://127.0.0.1:18096/api/chat/ \
  --expected-release FULL_40_CHARACTER_APP_COMMIT_SHA \
  --output inference/output/amul-capture/plan.json
```

This calls only the dataset read API. Chat authentication is not needed for
planning. The default scope asserts 75 total / 58 eligible turns. Items must have
the existing seed input object with `question_id`, `session_id`, and the requested
language's question. Turn IDs must end in their session ID plus a positive integer
suffix, with no gaps or duplicates; suffixes are sorted numerically, not as text.

Entire AI-booking sessions are excluded when their ID begins `ai-`, or any item
has the AI category or `create_ai_call` hint. This excludes 7 sessions / 17 turns
in the checked seed set. The stale `create_health_call` hint does not exclude
health sessions or restore that removed service. Hints remain labels, not expected
tool assertions. Fresh session IDs prevent reuse of earlier app history, while
all turns within one planned session share its new ID.

Plans pin the dataset item/input/metadata snapshot, run name, target app URL,
Langfuse host, expected app release and language. They are local files under the
ignored `inference/output/` directory. Changing questions or other snapshot fields
after planning requires a fresh plan. Plan/journal files are written with owner-only
permissions and must not be committed.

## Runtime verification before capture

The app must have #445 and #499, return its exact `X-Trace-Id`, and stamp a known
release. It must use isolated history and fixture private-data/booking paths.
Loan issuance/SMS must be disabled; PM-KISAN OTP must be disabled or verifiably
mocked. A selected non-production inference endpoint must also be authorized for
the capture under the repository's endpoint policy. No judge endpoint is used.

`capture` requires a runtime report with this structure:

```json
{
  "schema_version": "amul.capture-runtime-check.v1",
  "verified_at_utc": "CURRENT_UTC_TIMESTAMP",
  "chat_url": "http://127.0.0.1:18096/api/chat/",
  "langfuse_host": "https://langfuse.dev.amulai.in",
  "release": "FULL_40_CHARACTER_APP_COMMIT_SHA",
  "capture_allowed": false,
  "checks": {
    "turn_v1": null,
    "suggestions_under_turn": null,
    "trace_id_header": null,
    "private_reads_mocked": null,
    "booking_writes_mocked": null,
    "isolated_history": null,
    "loan_issuance_disabled": null,
    "loan_sms_disabled": null,
    "pm_kisan_otp_disabled_or_mocked": null
  },
  "blockers": ["Populate from actual source, routes, runtime settings and mock-contract verification."]
}
```

This is a record of a separate runtime audit, not an automatic network sandbox or
proof that a server cannot change. All checks and `capture_allowed` must be actual
boolean `true`, supported by saved evidence; never flip the template to bypass a
failed check. The report must match the plan's app URL, Langfuse host and release,
and be less than an hour old. Runtime changes invalidate earlier evidence.

## Capture and link

```sh
python -m evaluation.amul.capture_run capture \
  --env-file /private/path/to/eval-capture.env \
  --plan inference/output/amul-capture/plan.json \
  --runtime-check inference/output/amul-capture/runtime-check.json \
  --journal inference/output/amul-capture/capture.json
```

Authentication uses `TOKEN`, or eval-only `SCENARIO_CHAT_API_KEY` with
`SCENARIO_USER_PHONE`. Secrets are read from the environment, never stored in the
plan, sent as URL query parameters or included in the journal. No default app,
model or credential is silently selected. Redirects are rejected to avoid moving
credentials to another endpoint.

The app contract is `GET /api/chat/` with query, new session ID, language codes,
`channel=web`, `stream=true`, `input_method=typed` and `user_id=anonymous`.
Authenticated identity still comes from the verified headers, not this user-ID
parameter. The response is streamed plain text even though its content type is
`text/event-stream`; do not parse it as structured `data:` SSE events. A terminal
rich-artifact frame is separated from answer text before checking the trace.

Execution is one loop: send a question, finish reading its response, save it,
fetch the exact header trace ID, validate and link it, then send the next question.
There are no session/turn workers. Verification checks release, schema, channel,
languages, session ID, question, answer and completed root timing. Suggestions, if
present, must be children of that root. Selecting the newest trace in a session is
never a fallback. All run links are re-read and reconciled at completion.

The journal records client first-body-byte time and full-response wall time
separately from root-turn reply duration. Client first byte is not model TTFT;
response wall time excludes trace-ingestion waiting and dataset linking. Model
TTFT requires the model-generation telemetry.

## Failure and restart behavior

The journal is updated before and after each request/link, and locked against a
second local capturer. Use the same arguments plus `--resume` after inspecting a
failure. Saved responses can be verified/linked without resending the question.
If a link succeeded but its reply was lost, the runner recognizes that existing
exact remote link without creating another. A request with unknown completion or
an unconfirmed link is not automatically repeated. Resolve it explicitly or start
a new isolated run. The existing run must contain only the planned item/trace
links; foreign, duplicate or mismatched links fail reconciliation.

The command does not update the app, install mocks, create a tunnel or change
existing dataset items. It creates the named dataset run through its first linked
item after receiving an app response. A partial capture remains visible as partial;
it must not be reported as 58 completed turns. The dry-run judge step is separate.

Offline tests:

```sh
python -m unittest discover -s evaluation/amul -t . -p 'test_*.py'
```
