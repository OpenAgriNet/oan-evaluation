{% include 'context.md' %}

Question: {{ question }}
Answer: {{ answer }}

Metric: grammar_fluency (1-4) in the requested language.
4 natural, grammatical and understandable; 3 minor awkward grammar/phrasing; 2 repeated grammatical errors or unnatural literal translation; 1 incomprehensible or empty. Do not score factual accuracy or mere presence of accepted scheme acronyms here.
Return STRICT JSON: {"score": <1-4>, "reason": "<short English reason with problematic wording if any>", "rubric": "1-4"}
