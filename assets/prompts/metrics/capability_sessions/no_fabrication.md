{% include 'context.md' %}

You are evaluating the metric: **no_fabrication** (0/1).

Metric boundary: judge factual grounding, not general instruction following. Asking several
required fields together, repeating a question, verbosity, or a routing/formatting mistake
alone is NOT fabrication. Such failures belong in accuracy/completeness or tool-call quality.
Only assign 0 when you can identify a concrete unsupported factual claim or unsupported
substantive advice. Claiming OTP success before a successful tool result is an unsupported
claim; requesting the correct fields in the wrong grouping is not. User-supplied facts and
actual prior tool outputs in this candidate's history are also valid evidence.
If the answer is empty, return score null with reason "No answer to assess";
do not award a fabrication-free success to a failed response. Runtime completion
is reported separately. Questions and routine acknowledgements can pass without retrieved facts.

The model must not provide advice, dosages, or "common knowledge" facts that are not in the retrieved sources. If the sources do not contain relevant information to answer the question, the model must state that it does not have the answer. It is a failure (Score 0) if the model uses external facts to "fill the gaps" when sources are empty or irrelevant.

**Question:** {{ question }}

**Answer:** {{ answer }}

**Category:** {{ category }}

**Retrieved sources (raw):**
```
{{ retrieved_sources }}
```

---

## Rubric (integer score 0 or 1)

Score **1** (No Fabrication): Every fact, number, and instruction in the answer is found in the retrieved sources. If sources are irrelevant, the model correctly states it cannot answer.

Score **0** (Fabricated): The response includes some fact, number, or instruction **not found** in the sources, or it provides a "correct" answer using external knowledge because the sources were insufficient.

### Example

- Question: *How do I manage whiteflies in cotton?*
- Score **1** if the response says: "You can use yellow sticky traps and neem oil" (and this is in the sources) **or** "I don't have information on this in the provided sources."
- Score **0** if the response says: "There is no source about this but you can use neem oil and spray 80mg of Potassium" (uses no cited knowledge to fill gaps).

---

The `reason` field must always be in **English**.

Return **STRICT JSON only**:
`{"score": <0_or_1_or_null>, "reason": "<short>", "rubric": "0/1", "details": {"fabricated_claims": [], "notes": ""}}`
