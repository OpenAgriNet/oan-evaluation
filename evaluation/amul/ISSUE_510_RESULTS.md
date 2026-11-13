# Issue #510 capture and dry-run results

Historical execution evidence for [OpenAgriNet/amul-oan-api#510](https://github.com/OpenAgriNet/amul-oan-api/issues/510).
These provisional judge results are not a release benchmark or human gold labels.

## Capture

- Dataset: `amul-seed-questions`, unchanged at 75 items.
- Run: `seed-gu-turn-v1-510-fresh-20261102`, captured on 2 November 2026.
- [DEV Langfuse dataset](https://langfuse.dev.amulai.in/project/cml0dzqbw0006p207qemlwqz9/datasets/cmugtigxs0001p207krw6ojy8).
- Run ID: `52d605a9-f0da-4b66-ab95-8402bb1b3e88`.
- 58 distinct linked traces across 21 sessions: 34 health, 10 milk, 14 schemes.
- Excluded seven `ai-*` booking sessions / 17 questions as the issue requested,
  pending booking-mock modernization in #513. No questions were rewritten.
- Original Gujarati questions were sent sequentially, preserving session order.
  Fixed dataset questions were sent directly; no farmer simulator or audio stages
  were included. Judge scoring ran after capture with one worker.
- Isolated H100 app/history/fixtures; no real bookings or private REST writes.
  Existing service IDs and start times matched the before/after snapshots.
- Selected app revision: `2639987a2785fb33568174296444d24e17632c72`, containing
  #445 and #499. DEV model-pipeline configuration and Python package versions
  were matched; this was not a byte-identical DEV image or app revision.

## Final 15-metric dry run

Version `amul-final-v5` attempted all **870 cells = 58 traces × 15 metrics**:
**689 numeric scores, 165 rubric N/A cells, 16 grading errors across 13 traces**.
Means below include only numeric scores. Errors are not zeros or ordinary N/A.

| Metric | Numeric | N/A | Errors | Mean |
|---|---:|---:|---:|---:|
| output_hygiene (0/1) | 58 | 0 | 0 | 1.000 |
| tool_sequencing | 14 | 44 | 0 | 5.000 |
| source_alignment | 44 | 6 | 8 | 4.523 |
| safety_compliance | 5 | 53 | 0 | 5.000 |
| grammar | 57 | 0 | 1 | 5.000 |
| terminology | 56 | 1 | 1 | 5.000 |
| language_purity | 57 | 0 | 1 | 3.772 |
| fluency | 57 | 0 | 1 | 4.579 |
| task_success | 56 | 1 | 1 | 4.679 |
| user_context_retention | 36 | 21 | 1 | 4.833 |
| latency_s (seconds) | 58 | 0 | 0 | 6.278 |
| completeness | 56 | 2 | 0 | 4.643 |
| persona_adherence | 57 | 1 | 0 | 4.737 |
| brevity | 57 | 0 | 1 | 4.754 |
| content_gap | 21 | 36 | 1 | 4.524 |

Quality grades use 1–5 except binary output hygiene; latency is seconds.
Safety and tool-sequencing averages cover only 5 and 14 applicable numeric
judgments respectively. High averages do not establish broad coverage.

The 16 errors comprise eight source-alignment cells, four cells from one language
review, and one each for content gap, context retention, task success and brevity.
Examples include invented source references, supported claims without evidence,
and violation quotations from a prior turn. The scorer attempted the whole matrix
and exited nonzero to retain these failures explicitly. The regular `--post`
path still refuses publication while grading errors remain.

The original dry run did not use `--post`. On 4 November, at explicit user request,
a separate publication uploaded only the 689 saved numeric results to these
existing traces. All 689 were read back and verified, with provisional labels and
known false-high warnings. N/A/error cells remained unscored, with their statuses
in score metadata. No questions or judge calls were repeated for publication.
Existing `gujarati_language` and `turn_outcome` scores are separate automatic
scores, not part of the final 15-metric registry.

## Independent audit findings

- `milk-week-1`: last-seven-days request returned 26 October–2 November inclusive,
  eight days. Source alignment and task success nevertheless received 5. The
  arithmetic matched returned rows, but explicit relative-window cardinality was
  not enforced. This PR does not fix that semantic gap or manually replace scores.
- `health-heat-1`: an original Gujarati age phrase was translated into a pregnancy
  premise. The judge trusted that premise and awarded task success/completeness 5.
  Source alignment separately scored unsupported medical claims at 2.
- `milk-week-4`: context retention 3 correctly penalized re-asking the already
  known Banas dairy.
- Government-versus-union schemes and unsupported no-scheme claims remain review
  cases, alongside other scope and context mismatches.

Human-label calibration remains follow-up work under
[OpenAgriNet/amul-oan-api#483](https://github.com/OpenAgriNet/amul-oan-api/issues/483).
No new dataset or scoring against dataset descriptions is introduced here.

## Reply latency

| Category | Mean, s | Median, s | P95, s |
|---|---:|---:|---:|
| All 58 | 6.278 | 4.798 | 19.008 |
| Health | 4.133 | 2.778 | 10.254 |
| Milk | 4.884 | 2.555 | 19.990 |
| Schemes | 12.483 | 10.912 | 34.192 |

Measured from root `turn` start to end, including translation and excluding later
suggestions. Client first body byte averaged 2.170 s; response completion 6.285 s.
Neither is isolated model TTFT. No recorded generation had `completionStartTime`.
Overlapping model/translation spans must not be added as independent stages.

## Validation and artifacts

115 offline regressions passed on macOS and H100 during the run; the same 115
passed again locally when preparing the PR. They cover legacy/current traces,
root timing, status-bearing SPAN tools, milk summaries/multi-account records,
sequential capture, resume/link reconciliation, and Gujarati borrowing boundaries.

Full run artifacts are intentionally ignored under
`inference/output/amul-capture/seed-gu-turn-v1-510-fresh-20261102/`:
`final-15-results.json`, `TRACE_SCORECARDS.md`, `review-error-cells.json`,
`independent-review-queue.json`, and `langfuse-publication.json`.
This change includes synthetic fixtures and aggregate results, not credentials,
runtime exports, or raw farmer traces. The original dry-run summary was shared
in the [issue comment](https://github.com/OpenAgriNet/amul-oan-api/issues/510#issuecomment-6616459839).
