# Amul dataset-run reader

`langfuse_io.build_turn()` normalizes recorded Langfuse traces for the final Amul
metrics. It accepts both the earlier chat traces and the `turn.v1` format used
after OpenAgriNet/amul-oan-api#445 and #499.

| Value | turn.v1 | Earlier traces |
|---|---|---|
| Original question | String `trace.input` | `trace.input.query` |
| English question | `pre_translation.output` | `query_pretranslation.output` |
| Moderation category | `moderation.output.category` | `Moderation.output.category` |
| Reply latency, seconds | Root `turn` SPAN duration | `trace.latency` |

The root turn duration comes from its start/end timestamps, or its own `latency`
when timestamps are unavailable. A nested span named `turn` is not the root.
Incomplete turn.v1 timing stays null; it must not fall back to whole-trace latency,
which can include suggestions generated after the answer. Reply latency includes
translation; it is not model latency or time to first token.

For English input/output that bypass translation, explicit
`amul.source_lang=en` / `amul.target_lang=en` metadata allows the corresponding
trace text to populate the English fields. No language is inferred from text or
from the dataset's expected question. The historical `question_gu`/`answer_gu`
field names are retained for compatibility; language-specific metrics still need
an appropriate dataset.

Run offline reader and existing final-metric regressions from the repository root:

```sh
python -m unittest discover -s evaluation/amul -t . -p 'test_*.py'
```

These tests use synthetic traces and mocked judge responses; they do not contact
Langfuse, invoke models, or book services. Rebuild normalized caches from raw
traces after a reader update: `--from-cache` bypasses the reader and cannot repair
previously missing questions or inflated latency.

Issue #510 also requires a fresh linked dataset run and one judge dry run. Those
are separate from reader tests. Capture writes traces/run links; omitting `--post`
prevents the scoring command from publishing scores. Its initial capture excludes
the seven AI-booking sessions (17 questions), pending compatible booking mocks in
#513. Historical `create_health_call` hints do not restore that removed tool.
The reproducible planner/capture/linker is documented in [CAPTURE.md](CAPTURE.md).
The completed capture, dry-run output and known judge limitations are summarized
in [ISSUE_510_RESULTS.md](ISSUE_510_RESULTS.md).

## Tool and backend evidence

Normalized calls retain observation/parent IDs, level, status message and only the
relevant `amul.tool_status`/`amul.cache` metadata, omitting unrelated resource
metadata. Old payloads without status remain supported; missing status is unknown.
Tool steps may be native `TOOL` observations or current `SPAN` observations with
an explicit, nonempty `amul.tool_status`. A name or cache marker alone does not
make a span a tool. Both forms establish backend ancestry and are excluded from
the backend-call list themselves, avoiding double counting.
Tool status, failure reasons and backend results reach current/history judge
evidence and the indexed source catalog. `success` can mean that a tool correctly
asked for missing information; it is not a booking receipt or a reason for an
automatic high score. Honest unavailability can still be correct task handling.

`ai_call_booking` is recognized as a backend SPAN. Other new names require the
explicit boolean `ok` output contract and TOOL ancestry. Earlier `_api`/`fetch_`
naming remains supported. Backend spans are kept separate from agent TOOL calls;
they do not inflate the number of calls used for sequencing applicability.

## Milk totals and review-required cases

The existing internal `milk_total` validator still feeds `source_alignment`; it
does not add a public metric. It reads current calculated summaries and older
detail tables. Summary/detail differences within display-rounding uncertainty
are permitted. Missing FAT/SNF do not invalidate known quantities/amounts, and
identical same-date/shift rows within a lookup retain their multiplicity because
they may represent separate owned accounts. Deductions are not milk earnings.

Disjoint requested periods can be added. Repeated/overlapping detail lookups use
the maximum occurrence count of matching rows rather than doubling all records.
Conflicting overlapping groups cannot identify account/revision differences from
the current text alone. Such evidence, missing requested numeric cells, material
summary/detail conflicts, unsupported numeric table formats and multiple scalar
total candidates produce `REVIEW_REQUIRED`. Summary-only overlaps cannot be
reconciled without detail records or a proven disjoint range.

A requested total with no numerical claim, such as an honest unavailable reply,
does not become a numerical error. For an actual claim with unresolved evidence,
`source_alignment` is saved as an error needing review, not an ordinary N/A or a
passing score. The source judge is not called for these cases. Other metric jobs
can finish; the command exits nonzero and refuses score posting while errors
remain. The milk validator checks requested totals, not every possible number or
all Gujarati header variants; unresolved numeric tables are not guessed.

Score-composition version is `amul-final-v5` so older cached judgments are not reused.
The borrowing guard requires a leading word boundary, including Gujarati combining
marks: `ચાર્જ` cannot match inside `ડિસ્ચાર્જ`, and approving the latter glossary
term does not exempt an independently used `ચાર્જ`. Inflected forms such as
`બુકિંગ` remain detectable. These checks do not establish native-speaker correctness.
The implementation fingerprint now includes the reader and evidence helpers.
The public final-metric registry remains exactly 15 metrics. A pinned current-app
formatter fixture under `fixtures/` documents the output contract using synthetic
data, with no app, model or booking requests.
