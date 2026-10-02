# Amul metrics status (issue #1)

Tracks the 26 metrics listed in [issue #1](https://github.com/OpenAgriNet/oan-evaluation/issues/1), section 3,
against what is live on Langfuse (dataset `amul-seed-questions`).
Update this file whenever a metric is added, changed, or found to be wrong.

Last updated: 2026-10-02

Runs on Langfuse:
- `seed-gu-capture-3` (current): content tools working through the dev Beckn seeker, 60-day milk stub,
  milk-today question reworded, realistic technician names.
- `seed-gu-capture-2` (old): every content tool failed (X1). Kept for comparison; scored with the same judges, using
  the farmer context it was captured with (`--farmer-context`, old technician names).

Run everything on the H100 (dry run without `--post`):

    python -m evaluation.amul.run_amul_eval --run seed-gu-capture-3 --env-file inference/.env \
        --prod-reference --glossary-container amul_app_eval \
        --judges grounding,gujarati_language,prod_reference --post

`--glossary-container` needs Docker access to the agent container; it builds each answer's glossary with the agent's
own code. Langfuse can take a minute to list freshly linked run items; score after that.

## Done (9)

| Metric | Group | How | capture-3 |
|---|---|---|---|
| `output_hygiene` | A | Code check | Tool names, code identifiers, raw JSON, internal URLs, file names (`.docx`/`.pdf`), technician user IDs, leaked reasoning in plain words. 75/75 clean. |
| `language_purity` | D | Gemma judge `gujarati_language`, plus code check `language_mixing` (1-5) | Judge 4.59; code 4.84. The judge also catches Hindi words in Gujarati script (L1). |
| `grammar` | D | Gemma judge `gujarati_language` | 4.77 |
| `terminology` | D | Gemma judge `gujarati_language`, given the agent's own per-answer glossary | 4.69 |
| `fluency` | D | Gemma judge `gujarati_language` | 3.72, the weakest Gujarati score: literal translations of English phrasing. |
| `source_alignment` | B | Gemma judge `grounding` | 4.96 (69 scored) |
| `no_fabrication` | B | Gemma judge `grounding`, agent's own two-tier policy (N1) | 4.87 |
| `citation_accuracy` | B | Gemma judge `grounding`, definition C1 | 5.00 (8 scored; null when no source is named) |
| Tool-call correctness | E | `expected_tool_called` (28/28) and `tool_arguments` (20/21) | Booking codes, technician, species, emergency flag; milk date range. |

`gujarati_language` (overall, 4.05) is kept alongside the four parts for continuity.

## Partly done (2)

| Metric | Group | What covers it now | Missing |
|---|---|---|---|
| Feature/task success | E | `expected_tool_called`, `ticket_consistency` (15/17) | Judge on whether the farmer got what they asked for |
| Translation/glossary compliance | E | `glossary_adherence` (0.95), `ticket_consistency` | English-vs-Gujarati meaning check |

## Not started (15)

- A. Process: `agristack_workflow` (farmer profile use), `term_identification`, `tool_sequencing`, `search_quality`
- B. Grounding: `safety_compliance`
- C. Usefulness: `completeness`, `actionability`, `context_fit`, `clarity`, `conversation_closure`
- E. Amul-specific: persona adherence, brevity, context retention, content gap, recovery and clarification

## Extras (not in the issue)

- `latency_s`: end-to-end trace latency. capture-3 13.6 s (capture-2 4.9 s, when content tools failed fast).
- `glossary_adherence` (code): share of the answer's glossary terms whose approved Gujarati appears in the reply
  (any listed variant, inflection-tolerant). The glossary is what the agent's translator was given:
  `assets/glossary_terms.json` (786 pairs) in the agent repo, filtered per answer by
  `agents.tools.terms.get_mini_glossary_for_text(threshold=0.90, max_terms=40)`.
- `reference_agreement` (Gemma judge `prod_reference`): for turns whose knowledge tools failed, compares the agent's
  claims with successful results for similar questions in other Amul Agent Dev conversations. capture-3: 2 turns.

## Decisions taken (2026-10-02)

- **N1, general vet advice.** Follows the agent's own system prompt: specific claims (dosages, product names, scheme
  details, prices, farmer data, rules, contacts) must come from documents or tools, otherwise "I don't know based on
  the provided documents". General, widely accepted husbandry practice may be given without documents if brief and
  followed by a "consult your vet" caveat. `no_fabrication`: 5 with caveat, 4 without; unsafe general advice 2.
- **C1, citation.** A citation is any place the answer names where its information comes from: a document or
  brochure, a union or scheme circular, the farmer's own records, or a link/PDF/website. Agent rules: cite only
  retrieved sources, farmer-friendly names, no internal tool names, and no links for union schemes unless asked.
  Scored only when a source is named: 5 correct and farmer-friendly; 4 correct but a technical file name; 3 vague, or an
  unrequested union-scheme link; 2 content not in the cited source; 1 source never retrieved or a tool name.
  The fixed "I don't know based on the provided documents" is not a citation. Not citing is fine.
- **L2, English in brackets.** 19 of 492 recent production Gujarati replies (4%) have bracketed Latin text, mostly a
  dairy abbreviation after the Gujarati word ("કૃત્રિમ બીજદાન (AI)"). The agent's translation rules say "avoid
  brackets ... and repeated parenthetical restatements". So: a bracketed abbreviation (AI, PD, SNF, KCC...) scores 4,
  like any abbreviation; bracketed English words ("(retained placenta)") stay at 3.
- **O2, farmer codes in replies.** 2 of 492 production replies show codes, both the farmer's own society and farmer
  code; the agent prompt does not forbid it. Allowed. Technician user IDs are internal (prompt: "do not ask for ...
  internal `user_id`") and are flagged by `output_hygiene`.

## Findings to report

- **Translation corrupts tickets**: capture-3 2 of 17 (`EVAL-NET-0007` transliterated; `EVAL-HEALTH-0007` became
  `EVAL-HEALTH-00007`); capture-2 3 of 16.
- **Wrong milk total**: `milk-week` turn 2 says 72.7 L; the 15 records the tool returned sum to 68.4 L.
- **Future date**: `milk-dates` turn 1, asked on 2 Oct about "the 14th", the agent fetched 14 Oct instead of 14 Sep.
- **Milk range not split**: `milk-period` (capture-2) asked for 58 days in one call; the tool allows 31 and refused.
- **Central schemes described while Bharat Vistaar is down**: `scheme-all` #1, `scheme-apply` #1 describe KCC, PM-KISAN,
  PMFBY after every `get_vistaar_scheme_info` call returned "temporarily unavailable".
- **The agent's fallback "I don't know based on the provided documents"** is translated literally; the judge calls it
  robotic, and "documents" means nothing to a farmer.
- **A production reply cited a file name** ("...પુસ્તિકા ૧.docx").
- Booking fails often in Amul Agent Dev itself (non-eval traces): `create_ai_call` ticket in 14 of 95 calls,
  `create_health_call` in 41 of 86.
- `get_vistaar_scheme_info` fails in Amul Agent Dev (9 ok of 300, none since 2026-09-01) and on the dev seeker
  (the `vistaar` leg times out from vm5 too).

## Problems by metric

Numbered so fixes can be referred to by ID.

### Gujarati judge
- *Fixed* **G1**: four scores (grammar, terminology, language_purity, fluency) plus the overall score, one call.
- *Fixed* **G2**: the judge gets the agent's own glossary for each answer; `glossary_adherence` added.
- **G3** The Langfuse UI evaluator `gujarati_language` is still active and would recreate a duplicate column on new
  dataset runs. Fix: deactivate it under LLM-as-a-Judge (manual; no API in v3.150).
- **G4** Uncalibrated. `fluency` in particular looks strict (3.7). See X2.
- **G5** capture-2 replies contain the old stub names "Eval Technician One/Two", which the judge reads as English
  numbers; gone in capture-3.

### language_mixing (code)
- *Covered* **L1**: Hindi in Gujarati script is caught by the judge's `language_purity`.
- *Decided* **L2**: see Decisions.

### output_hygiene (code)
- *Fixed* **O1**: leaked reasoning in plain words (English answer), file names, technician user IDs.
- *Decided* **O2**: see Decisions.

### ticket_consistency / tool_arguments / expected_tool_called (code)
- *Fixed* **T1**: real tickets are 12 digits or two letters + 12 digits.
- *Fixed* **T2 / E2**: `tool_arguments` checks booking codes against the profile, technician against the list,
  species against the farmer's latest message, emergency case type, milk range (order, 31-day limit, no future dates).
- **E1** One expected tool per session; advice-only sessions are misjudged. Fix: `expected_tools` per turn.
- **E3** Scored once per session (by design).
- *Fixed* **E4**: `milk-today` now asks how much milk was poured at the society today; capture-3 calls the milk tool.

### Grounding judge
- *Fixed* **S1 / N2**: content tools work (X1); capture-3 has real document and scheme results.
- *Fixed* **S2**: the stub serves 60 days of morning/evening records filtered to the requested range.
- *Fixed* **S3**: earlier turns include their tool results.
- *Fixed* **S6**: tool results were cut at 4,000 characters, so supported facts (Heata-Sure at character 6,498)
  looked invented. Now passed in full (up to 120,000; earlier turns 30,000).
- **S4** The judge cannot see the agent's system prompt. Partly fixed: the grounding prompt summarises the agent's tools
  and the loan facts its instructions give it. Full fix: log the prompt version to Langfuse.
- **S5** Farmer profile comes from the stub. Production judging needs the profile (or a redacted one) on the trace.
- *Decided* **N1**, **C1**: see Decisions.

### Across all metrics
- *Fixed* **X1**: content tools. The eval copy's `AMUL_NETWORK_URL` and `VISTAAR_SEEKER_URL` point at the dev seeker
  (vm5 `10.5.25.36:16240`, reachable from the H100). Only discovery goes there (vet documents, union schemes, Vistaar;
  read-only searches). Farmer, milk, animal and technician reads use the adapter URL, which stays unresolvable, so they
  fall back to the stub; booking stays on the stub. Previous env saved as `container.env.pre-x1` on the H100.
- **X2** No judge metric is calibrated. Fix: hand-score about 15 turns across features; compare per metric.
- **X3** Reference mode waits on Amul's golden answers.
- **X4** Ticket corruption in translation (agent bug). Report to the agent team.
- **X5** The agent's own label scores (`turn_outcome`, `pipeline_profile`, `served_tier`) show as 0. Fix: hide those
  columns in the Langfuse table.
- *Done* **X6**: production references for failed knowledge tools (`reference_agreement`).
- **X7** The judge occasionally returns broken JSON twice (1 of 75 per run). Low priority.

## Backlog (later)

- The 15 not-started metrics above, and the missing parts of task success and translation compliance.
- E1 per-turn expected tools; X2 calibration; X3 golden answers; S4/S5 logging asks to the agent team.
- A deterministic milk-total check (the judge caught the 72.7 vs 68.4 L error in one run and missed it in another).
- `check_loan_eligibility`: keep blocked (issues a loan code and can SMS); add a stub if loan turns matter.
- Bharat Vistaar leg times out on dev as well as in Amul Agent Dev; raise with whoever owns the BV sandbox.
