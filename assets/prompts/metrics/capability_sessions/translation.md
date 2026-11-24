{% include 'context.md' %}

Metric: translation_accuracy (1-4). Measure cross-language semantic fidelity, NOT fluency
or script purity. The answer is not a literal translation assignment: compare how it conveys
actual user meaning and English tool evidence into the requested language. Preserve numbers,
units, dates, locations, schemes, negation, uncertainty, eligibility and action status.
Equivalent paraphrases and concise relevant selection are valid. Do not treat another model's
English answer as gold. Do not blame the answer for dataset question-translation discrepancies.

Question: {{ question }}
Answer: {{ answer }}

4: Meaning faithfully conveyed without material distortion.
3: Minor mistranslation that does not alter the main meaning or action.
2: Material change to an important fact, condition, unit, status or user intention.
1: Meaning largely reversed, lost, or conveyed in the wrong requested language.
English responses have no cross-language translation to judge: return score null and
details.applicable=false. For an empty answer also return null, explicitly noting failure.
For native-language clarification assess preservation of workflow/field meaning; do not invent
a missing translation source. If cross-language meaning cannot be assessed, return null.

Return STRICT JSON: {"score": <1-4 or null>, "reason": "<short English reason with examples>",
"rubric": "1-4", "details": {"applicable": <boolean>, "semantic_errors": []}}
