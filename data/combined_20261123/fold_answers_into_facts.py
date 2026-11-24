"""Drop prepared `answers` from the simulated-farmer profiles; their content moves into `facts` (user, 2026-11-24).

The farmer then writes every reply itself from facts. Gemma (gateway 18869) rewrites each profile's facts so that
everything the prepared answers said is a key-value fact (values copied from the original text). Checks:
  - every digit-bearing token (IDs, OTPs, numbers, dates) in the old answers appears in some fact value;
  - every fact value appears in the session's scripted text (nothing invented; unverifiable facts are dropped).
The old answers stay in `folded_answers` for audit only; the simulator does not read them.
"""
import asyncio
import csv
import json
import re
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
SPECS = HERE / 'sim_specs.json'
URL = 'http://127.0.0.1:18869/simspec/v1/chat/completions'
PROMPT = """A simulated farmer will chat with an agricultural assistant. It answers the assistant's questions ONLY
from its facts. Some details are currently only in prepared answer lines. Produce the complete facts so that
every detail, preference, constraint and yes/no decision in the prepared answers is captured.

CURRENT FACTS:
{facts}

PREPARED ANSWER LINES (to be removed):
{answers}

Rules: keep all current facts; add a key for each missing detail; short snake_case English keys; values copied
exactly from the text above (a constraint or preference may be a short phrase copied from the line, e.g.
"constraint": "use the same mobile, year and season"). Do not invent anything.
Return JSON only: {{"facts": {{"key": "value", ...}}}}"""


def norm(text):
    return re.sub(r'[\s.,;:!?"\'()-]+', ' ', str(text).lower()).strip()


def digit_tokens(text):
    return {t for t in re.findall(r'[A-Za-z]*\d[\w/-]*', text) if len(re.sub(r'\D', '', t)) >= 2 or t.isdigit()}


async def fold(client, sid, spec, q, plan_text, sem):
    answers = [q[a] for a in spec.get('answers', [])]
    if not answers:
        return sid, spec, None
    async with sem:
        last = None
        for _ in range(3):
            try:
                r = await client.post(URL, timeout=180, json=dict(
                    model='google/gemma-4-31b-it', temperature=0, max_tokens=2000, messages=[dict(role='user',
                    content=PROMPT.format(facts=json.dumps(spec.get('facts', {}), ensure_ascii=False),
                                          answers='\n'.join(f'- {a}' for a in answers)))]))
                r.raise_for_status()
                facts = json.loads(re.search(r'\{.*\}', r.json()['choices'][0]['message']['content'], re.S)
                                   .group(0))['facts']
                values = lambda v: v if isinstance(v, list) else [v]
                dropped = sorted({k for k, v in facts.items() for x in values(v)
                                  if norm(x) and norm(x) not in plan_text and norm(x) not in ('yes', 'no')})
                for k in dropped:
                    facts.pop(k)
                covered = norm(' '.join(str(x) for v in facts.values() for x in values(v)))
                missing = sorted({t for a in answers for t in digit_tokens(a) if norm(t) not in covered})
                assert not missing, f'answer details missing from facts: {missing}'
                new = {k: v for k, v in spec.items() if k != 'answers'}
                new.update(facts=facts, folded_answers=spec['answers'],
                           dropped_facts=sorted(set(spec.get('dropped_facts', [])) | set(dropped)))
                return sid, new, None
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last = f'{type(exc).__name__}: {exc}'
        return sid, spec, last


async def main():
    specs = json.loads(SPECS.read_text())
    rows = list(csv.DictReader((HERE / 'text.csv').open()))
    q = {r['question_id']: r['question_english'] for r in rows}
    plan = {}
    for r in rows:
        plan[r['session_id']] = plan.get(r['session_id'], '') + ' ' + r['question_english']
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(fold(client, sid, spec, q, norm(plan[sid]), sem)
                                         for sid, spec in specs.items()))
    failed = {sid: err for sid, _, err in results if err}
    for sid, spec, err in results:
        if not err:
            specs[sid] = spec
    SPECS.write_text(json.dumps(specs, ensure_ascii=False, indent=1))
    print('profiles', len(specs), 'still with answers', sum('answers' in s for s in specs.values()),
          'failed', failed)


if __name__ == '__main__':
    asyncio.run(main())
