{% include 'context.md' %}

You are evaluating the metric: **tool_call_quality** (1–4).

Examine the tool calls made during this session and assess whether they were correct, efficient, and well-formed. Focus on mistakes in how tools were invoked — not whether the final answer is correct.

**Question:** {{ question }}

**Answer:** {{ answer }}

**Category:** {{ category }}

Use the complete chronological call evidence in the session context above.

---

## What to check

- **Repeated calls**: Was the same tool called multiple times with identical or near-identical inputs when one call would have sufficed?
- **Wrong tool selected**: Was an inappropriate tool chosen for the information needed (e.g. using a weather tool for a mandi price query)?
- **Wrong parameters / path**: Were tool arguments incorrect — wrong crop name, wrong location, wrong scheme identifier, wrong field path?
- **Mistranslation in inputs**: Were inputs to the tool in the wrong language or script (e.g. transliterated where original script was needed, or vice versa)?
- **Missing calls**: Was a tool that was clearly needed for the question never called?
- **Malformed calls**: Were arguments missing, null, or structurally wrong?

If there are no tool calls, score based on whether tool calls were expected for this question.

---

## Rubric (integer score 1–4)

Score **4** (Excellent): All tool calls are correct, well-formed, non-redundant, and use the right tool with right parameters. Or: no tools needed and none were called.

Score **3** (Good): Tool calls are mostly correct but have one minor issue — a slightly redundant call, a minor parameter mismatch, or a small translation issue that still produced a usable result.

Score **2** (Poor): One significant error — wrong tool selected, clearly wrong parameter, or a required tool was skipped.

Score **1** (Very poor): Multiple errors — repeated unnecessary calls, wrong tools, malformed arguments, or systematic mistranslation in tool inputs that would have caused bad retrieval.

---

The `reason` field must always be in **English**.

Return **STRICT JSON only**:
`{"score": <1-4 integer>, "reason": "<short>", "rubric": "1-4", "details": {"issues": [], "notes": ""}}`
