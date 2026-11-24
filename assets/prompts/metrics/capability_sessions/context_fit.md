{% include 'context.md' %}

Question: {{ question }}
Answer: {{ answer }}

Metric: context_fit (0/1). Does the response/action use the correct workflow, farmer identifiers and supplied information while respecting topic switches? Correctly resuming a pending workflow, asking a genuinely ambiguous field, or starting a new requested topic passes. Reusing unrelated OTPs/identifiers, forgetting supplied fields or staying stuck on an old topic fails. First-turn answers can pass; empty answers fail.
Check the contract's conversation procedure as well as its topic: ask only one missing field at a time, reuse already supplied valid fields, and preserve required verification and confirmation steps. Requesting two or more missing workflow fields together fails this metric even when those fields are genuinely needed. That procedural mistake alone is not fabrication or a factual error; those are separate metrics.
Return STRICT JSON: {"score": <0 or 1>, "reason": "<short English reason>", "rubric": "0/1"}
