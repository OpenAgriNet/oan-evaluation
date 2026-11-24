{% include 'context.md' %}

Question: {{ question }}
Answer: {{ answer }}

Metric: answer_relevance (1-5). Judge whether the answer addresses the actual request, independently of factual correctness. Necessary clarification or honest unavailability is relevant. Do not demand a final result before prerequisites are supplied.
5 directly addresses the entire request; 4 mostly direct with minor tangents; 3 partially addresses it; 2 mostly off-topic; 1 unrelated, nonresponsive or empty.
Return STRICT JSON: {"score": <1-5>, "reason": "<short English reason>", "rubric": "1-5"}
