{% include 'context.md' %}

Question: {{ question }}
Answer: {{ answer }}

Metric: voice_ready_text (0/1). Is the answer ready to be spoken without chat/markup artifacts? Fail markdown headings/tables/bullets, code blocks, raw tool syntax or URLs where the voice contract prohibits them. Normal punctuation, apostrophes and hyphenated names alone are not failures. Follow the contract's spoken-number and brevity requirements. Empty answers fail.
Return STRICT JSON: {"score": <0 or 1>, "reason": "<short English reason>", "rubric": "0/1", "details": {"spoken_artifacts": []}}
