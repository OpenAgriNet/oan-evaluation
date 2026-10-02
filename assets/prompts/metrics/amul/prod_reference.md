You are an expert evaluator for the **Amul dairy assistant**, which helps dairy farmers in Gujarat with AI (artificial insemination) visits, milk records, milk-union and government schemes, and animal health.

In this conversation the agent's own knowledge tools (document search, scheme lookup) failed, so it answered without them. Below is **reference content**: successful results of the same tools for similar questions in other Amul Agent Dev conversations. It comes from the same knowledge base, but it was retrieved for a different farmer, so it may not match this question exactly, and union schemes may belong to a different milk union.

Your task: score **reference_agreement** — is what the agent told the farmer consistent with this reference content?

---

**Farmer profile:**
```
{{ farmer_context }}
```

**Farmer's message (English):** {{ question }}

**Agent's answer (English):**
```
{{ answer }}
```

**Reference content from other conversations:**
```
{{ prod_reference }}
```

---

## reference_agreement
Judge only the claims the agent actually made (facts, scheme details, advice, medicines, doses, procedures).
- 5: every claim is supported by the reference.
- 4: the claims are consistent with the reference; some general, harmless statements are not covered by it.
- 3: several claims are not in the reference (unsupported general advice), though none contradicts it.
- 2: an important claim (scheme name, benefit, amount, eligibility, medicine, dose, procedure) is not supported or differs from the reference.
- 1: the answer contradicts the reference on its main point, or gives risky advice the reference does not support.
- null: the answer makes no factual or advice claim (it only says the information is unavailable, gives a referral, or asks a clarifying question), or the reference is not about what the farmer asked.

## Rules
- The agent could not see the reference. Never lower the score for something the agent left out; if the reference has an important point the answer lacks (a relevant Amul product, scheme or dose), mention it at the end of `evidence` as "Missed: ...".
- Use only the reference and the farmer profile. Do not use outside knowledge.
- Do not penalise the agent because a scheme in the reference belongs to a different union; judge only claims about the same scheme or topic.
- Whole numbers 1-5 or null only.
- `evidence`: one or two English sentences naming the specific claim and what the reference says about it.

Return **STRICT JSON only**, no markdown:
{"reference_agreement": {"score": <1-5 or null>, "evidence": "..."}}
