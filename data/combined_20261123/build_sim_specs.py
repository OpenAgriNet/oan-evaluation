"""Structured profiles for the simulated farmer, one per multi-question session (user design 2026-11-24).

For each session with more than one question, Gemma (through gateway 18869, open model only) reads the scripted
English turns and classifies every turn as exactly one of:
  - request:    opens a request or topic; sent verbatim, in order (carries the judge unit);
  - test_input: a deliberate partial/wrong/changed input (e.g. "990000.", "246.", a first shop that fails);
                sent verbatim once, the first time the assistant asks for what `use_when` describes;
  - answer:     a plain answer ("Yes.", "Gujarat.", "246810."): NOT replayed; its content goes into `facts`
                and the farmer answers from facts only when the assistant actually asks.
Checks: every turn classified once, turn 1 is a request, every fact value appears in the scripted text.
Profiles are cached in sim_specs.json (re-run only fills missing or failed sessions).

Usage: build_sim_specs.py [--only <session_id> ...]
"""
import argparse
import asyncio
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / 'sim_specs.json'
URL = 'http://127.0.0.1:18869/simspec/v1/chat/completions'
MODEL = 'google/gemma-4-31b-it'
PROMPT = """Below is a scripted conversation plan: the messages an Indian farmer sends to an agricultural assistant,
in order (the assistant's replies are not shown). Turn it into a profile for a simulated farmer.

Classify EVERY turn as exactly one of:
- "request": the farmer starts a new request or topic, or asks a new question (turn 1 is always a request).
- "test_input": a deliberate input that the farmer gives before the correct value or instead of the expected
  choice: an incomplete/partial or wrong ID/number/OTP, a value later corrected, a first choice that is later
  changed. Give "use_when": what the assistant asks for that this input answers (e.g. "asks for the Farmer ID").
- "answer": a plain reply that only supplies a detail or confirmation (yes/no, an ID, OTP, state, crop, quantity,
  shop, date, place...).

Then list "facts": key-value pairs for every detail the farmer knows from the plan (IDs, phone numbers, OTPs,
state, district, village, crop, quantities, chosen shop or option, season, year, cycle, names, answers to yes/no
questions such as "wants FFS: yes"). Use the FINAL correct values (after corrections). Keys are short snake_case
English; values copied exactly from the plan text.

PLAN:
{plan}

Return JSON only:
{{"turns": [{{"n": 1, "type": "request"}}, {{"n": 2, "type": "answer"}}, {{"n": 3, "type": "test_input", "use_when": "..."}}, ...],
  "facts": {{"key": "value", ...}}}}"""


def norm(text):
    return re.sub(r'[\s.,;:!?"\'()-]+', ' ', text.lower()).strip()


def check(spec, turns):
    kinds = {t['n']: t for t in spec.get('turns', [])}
    assert sorted(kinds) == list(range(1, len(turns) + 1)), 'every turn classified once'
    assert kinds[1]['type'] == 'request', 'turn 1 is a request'
    assert all(t['type'] in ('request', 'test_input', 'answer') for t in kinds.values())
    assert all(t.get('use_when') for t in kinds.values() if t['type'] == 'test_input'), 'test_input needs use_when'
    plan_text = norm(' '.join(turns))
    values = lambda v: v if isinstance(v, list) else [v]
    bad = [k for k, v in (spec.get('facts') or {}).items() for x in values(v)
           if norm(str(x)) and norm(str(x)) not in plan_text and not re.fullmatch(r'(yes|no)', norm(str(x)))]
    # A fact that is not copied from the plan could be invented: drop it, keep the rest of the profile.
    for k in set(bad):
        spec['facts'].pop(k, None)
    spec['dropped_facts'] = sorted(set(bad))


async def profile(client, sid, rows, sem):
    turns = [r['question_english'] for r in rows]
    plan = '\n'.join(f'{i}. {q}' for i, q in enumerate(turns, 1))
    last = None
    async with sem:
        for attempt in range(3):
            try:
                r = await client.post(URL, timeout=180, json=dict(
                    model=MODEL, temperature=0, max_tokens=3000,
                    messages=[dict(role='user', content=PROMPT.format(plan=plan))]))
                r.raise_for_status()
                raw = r.json()['choices'][0]['message']['content']
                spec = json.loads(re.search(r'\{.*\}', raw, re.S).group(0))
                check(spec, turns)
                by_n = {t['n']: t for t in spec['turns']}
                return sid, dict(
                    requests=[rows[n - 1]['question_id'] for n in sorted(by_n) if by_n[n]['type'] == 'request'],
                    test_inputs=[dict(question_id=rows[n - 1]['question_id'], use_when=by_n[n]['use_when'])
                                 for n in sorted(by_n) if by_n[n]['type'] == 'test_input'],
                    answers=[rows[n - 1]['question_id'] for n in sorted(by_n) if by_n[n]['type'] == 'answer'],
                    facts=spec.get('facts') or {}, dropped_facts=spec.get('dropped_facts', []))
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last = f'{type(exc).__name__}: {exc}'
    return sid, dict(error=last)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', nargs='*')
    args = ap.parse_args()
    sessions = defaultdict(list)
    for r in csv.DictReader((HERE / 'text.csv').open()):
        sessions[r['session_id']].append(r)
    specs = json.loads(OUT.read_text()) if OUT.exists() else {}
    todo = {sid: sorted(rows, key=lambda r: int(r['question_id'].rsplit('-', 1)[-1]))
            for sid, rows in sessions.items() if len(rows) > 1
            and (args.only and sid in args.only or not args.only and (sid not in specs or 'error' in specs[sid]))}
    print('sessions to profile', len(todo))
    sem = asyncio.Semaphore(12)
    async with httpx.AsyncClient() as client:
        for sid, spec in await asyncio.gather(*(profile(client, s, rows, sem) for s, rows in todo.items())):
            specs[sid] = spec
    OUT.write_text(json.dumps(dict(sorted(specs.items(), key=lambda x: int(x[0]))), ensure_ascii=False, indent=1))
    failed = [s for s, v in specs.items() if 'error' in v]
    print('profiles', len(specs), 'failed', len(failed), failed[:10])


if __name__ == '__main__':
    asyncio.run(main())
