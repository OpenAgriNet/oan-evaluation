{% include 'context.md' %}

You are evaluating the metric: **accuracy_completeness** (1–4).

Evaluates whether the response accurately and fully answers the user's query. The model is allowed to say that it doesn't have the data to respond to the question if that's the case.

This row contains a whole session. Apply the rubric across ALL turns and summarize material defects
with their question IDs. A single correctly handled topic does not establish a perfect session.
An avoidable missing business lookup or an unresolved user request despite supplied prerequisites
must affect this metric; do not hide it behind successful unrelated turns.

**Question:** {{ question }}

**Answer:** {{ answer }}

**Category:** {{ category }}

---

## Rubric (integer score 1–4)

Score **4**: Fully answers **all** aspects of the query with necessary detail (e.g., identifies the pest, provides mechanical control, and lists chemical dosages).

Score **3**: Answers the **main** question but leaves out a **minor** detail (e.g., mentions the pesticide but forgets the water dilution ratio).
**Also Score 3**: The bot explicitly acknowledges a **technical failure or service unavailability** (e.g., "I'm unable to fetch your PM-Kisan status right now due to a technical issue") AND provides meaningful next steps or alternatives — try again later, use the portal directly, call the helpline, etc. An honest graceful-failure response is not a wrong answer.

Score **2**: Provides a **partial** answer or answers something related to the crop but **not** answering the actual question.

Score **1**: The response is **incorrect**, talks about a **different** scheme/crop/topic than what was asked, or gives a vague deflection with no useful guidance whatsoever.

---

The `reason` field must always be in **English**.

Return **STRICT JSON only**:
`{"score": <1-4 integer>, "reason": "<short>", "rubric": "1-4", "details": {"missing_parts": [], "notes": ""}}`
