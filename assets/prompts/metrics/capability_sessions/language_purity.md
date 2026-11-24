{% include 'context.md' %}

Question: {{ question }}
Answer: {{ answer }}

Metric: language_purity (1-5). Target language: {{ output_language }}.
5 entirely target language; 4 only accepted scheme/source acronyms, proper names or quoted identifiers in another script; 3 limited technical English borrowing while otherwise in the target language; 2 unnecessary code-switching or whole foreign-language phrases; 1 predominantly wrong language, random foreign-script words, or empty.
English is a valid target for en: English words are not mixing in that case. Digits, punctuation, IDs and proper names alone are not language mixing. Hindi and Marathi share a script: inspect language, not just Unicode blocks.
Return STRICT JSON: {"score": <1-5>, "reason": "<short English reason>", "rubric": "1-5", "details": {"mixed_language_examples": [], "foreign_script_found": <boolean>}}
