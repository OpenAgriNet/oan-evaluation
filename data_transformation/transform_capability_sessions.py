"""Export scenario-runner evidence to native OAN session-level judge rows.

English calls are shared comparison evidence, never a gold answer or target-language source.
By default no tool output or conversation turn is truncated and the contract is the candidate's own system
prompt. Two opt-in settings keep long sessions within the judge's context, identically for every candidate:
- JUDGE_CONTRACT=core with JUDGE_CORE_FILE: instead of the candidate's full system prompt, a shared English core
  prompt, the English instructions of the skills this session needs (JUDGE_SESSION_SKILLS: a reviewed
  session -> skills file, plus owners of any other tool the candidate called, plus required skills), and the
  description and parameters of each tool the session called. Same text for every candidate and language.
- JUDGE_TOTAL_TOKENS=N (or JUDGE_EVIDENCE_TOKENS=N): if tool outputs do not fit the remaining budget, only the
  largest outputs are trimmed (to one shared cap) and marked; smaller outputs stay complete.
- Judge units: a fixture with a judge_group column is judged once per original session (group), even when inference
  chained several original sessions into one conversation. Earlier turns of that conversation are passed as
  context only (earlier_context) and are not scored; tool evidence is the group's own calls.
  JUDGE_GROUPS=<json> gives each group's coverage tools, skills, consent and optional reviewed expected behavior.
"""
import os
import argparse
from collections import defaultdict
from datetime import datetime
import csv
import hashlib
import json
from pathlib import Path
import re
import runpy

ROOT = Path(__file__).resolve().parents[1]
DATE_RE = re.compile(r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), \d{1,2} [A-Z][a-z]+ \d{4}')
JUDGE_CORE = (json.loads(Path(os.environ['JUDGE_CORE_FILE']).read_text())
              if os.environ.get('JUDGE_CONTRACT') == 'core' else None)
JUDGE_SESSION_SKILLS = (json.loads(Path(os.environ['JUDGE_SESSION_SKILLS']).read_text())
                        if os.environ.get('JUDGE_SESSION_SKILLS') else {})
JUDGE_GROUPS = json.loads(Path(os.environ['JUDGE_GROUPS']).read_text()) if os.environ.get('JUDGE_GROUPS') else {}
JUDGE_FIXED_OVERHEAD_TOKENS = 8000  # metric rubric, template text, call index and principles
DATA = ROOT / 'data/prompt_capabilities/expanded'


def calls_for(turn):
    returns = {r['tool_call_id']: r for r in turn['returns']}
    retries = {r['tool_call_id']: r for r in turn['retries'] if r.get('tool_call_id')}
    calls = []
    for call in turn['calls']:
        identifier = call['tool_call_id']
        args = call['args']
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        result = returns.get(identifier)
        retry = retries.get(identifier)
        calls.append(dict(question_id=turn['question_id'], name=call['tool_name'], input=args,
                          output=result['content'] if result else None,
                          retry=retry['content'] if retry else None,
                          returned=result is not None))
    return calls


def estimate_tokens(text):
    """Gemma tokens, not undercounting Indic scripts (ASCII ~4 chars/token, other scripts ~2)."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars // 4 + (len(text) - ascii_chars) // 2


def fit_evidence(calls, budget):
    """Trim only the largest tool outputs, to one shared cap, so all outputs fit within budget tokens."""
    if not budget:
        return calls
    texts = [c['output'] if isinstance(c['output'], str) else json.dumps(c['output'], ensure_ascii=False)
             for c in calls]
    sizes = [estimate_tokens(t) if c['output'] is not None else 0 for t, c in zip(texts, calls)]
    if sum(sizes) <= budget:
        return calls
    low, high = 0, max(sizes)
    while low < high:  # largest cap with sum(min(size, cap)) <= budget
        mid = (low + high + 1) // 2
        if sum(min(size, mid) for size in sizes) <= budget:
            low = mid
        else:
            high = mid - 1
    trimmed = []
    for call, text, size in zip(calls, texts, sizes):
        if size > low:
            keep = max(int(len(text) * low / size), 0)
            call = dict(call, output=text[:keep] + f'\n[... evidence trimmed for the judge: {len(text) - keep} of '
                        f'{len(text)} characters omitted; do not treat omitted content as missing or invented ...]')
        trimmed.append(call)
    return trimmed


def session_skills(core, sid, calls):
    chosen = set((JUDGE_GROUPS.get(sid) or JUDGE_SESSION_SKILLS.get(sid, {})).get('skills', []))
    chosen |= {owner for c in calls for owner in core['tool_skills'].get(c['name'], [])
               if not any(o in chosen for o in core['tool_skills'].get(c['name'], []))}
    pending = list(chosen)
    while pending:
        for required in core['requires'].get(pending.pop(), []):
            if required not in chosen:
                chosen.add(required); pending.append(required)
    return [name for name in core['skills'] if name in chosen]


def core_contract(core, calls, sid=None):
    skills = session_skills(core, sid, calls) if sid is not None else []
    names = sorted({c['name'] for c in calls})
    tools = '\n'.join(f"- {n}: {core['tools'][n]['description'].strip()}\n  parameters: "
                      f"{json.dumps(core['tools'][n]['parameters'], ensure_ascii=False)}"
                      for n in names if n in core['tools'])
    skill_text = ''.join(f'\n\n## Skill instructions: {name}\n' + core['skills'][name] for name in skills)
    return (core['core'] + skill_text + '\n\n## Tools called in this session (descriptions and parameters)\n'
            + (tools or 'No tools were called.'))


def transform(records, fixture, english, contracts, product):
    questions = {r['question_id']: r['question_english'] for r in fixture}
    unit_of_planned = {r['question_id']: r.get('judge_group') or r['session_id'] for r in fixture}
    # Simulated-farmer runs add reply turns ("<planned id>.r<k>") between planned questions. A reply belongs to the
    # judge unit of the planned question it answers; turns are ordered by the run's turn number.
    planned = lambda r: r.get('planned_question_id') or r['question_id']
    unit_of = lambda r: unit_of_planned[planned(r)]
    order = lambda r: (int(r['turn']) if str(r.get('turn') or '').isdigit()
                       else int(r['question_id'].split('.r')[0].rsplit('-', 1)[-1]))
    by_session = defaultdict(list)
    for turn in records:
        by_session[turn['session_id']].append(turn)
    groups = defaultdict(list)
    for turn in records:
        groups[unit_of(turn)].append(turn)
    assert len({r['question_id'] for r in records}) == len(records)
    # A simulated farmer may skip planned short replies the conversation made redundant.
    assert {planned(r) for r in records} <= set(questions)
    english_groups = defaultdict(list)
    for turn in english:
        english_groups[unit_of(turn)].append(dict(
            question_id=turn['question_id'], calls=[{k:v for k,v in c.items()
                if k in ('question_id','name','input')} for c in calls_for(turn)]))
    extras = {s['id']: s for s in json.loads((ROOT/'inference/capability_sessions.json').read_text())['sessions']}
    extras.update(JUDGE_GROUPS)
    rows = []
    for sid, turns in sorted(groups.items(), key=lambda item: (int(item[1][0]['session_id']), item[0])):
        turns.sort(key=order)
        session = turns[0]['session_id']
        last = order(turns[-1])
        mine = {r['question_id'] for r in turns}
        earlier = [dict(question_id=r['question_id'], question=r['question'], answer=r['answer'])
                   for r in sorted(by_session[session], key=order) if order(r) < last and r['question_id'] not in mine]
        contract = contracts[session]
        errors = [r['question_id'] for r in turns if not r['answer'] or r['answer'].startswith('ERROR')]
        english_questions = [dict(question_id=r['question_id'], question=questions.get(r['question_id'], r['question'])) for r in turns]
        reference = dict(
            principles=[
                'The supplied current product contract is authoritative. Tool targets are coverage goals, not unconditional gold calls.',
                'Reuse relevant user-supplied fields, but obey actual workflow-specific initiation and verification prerequisites.',
                'A phone, registration number, application number and OTP are not interchangeable. Never invent missing values.',
                'A failed OTP does not authorize the protected operation. A new user OTP can be retried with the saved workflow fields.',
                'A topic change must not erase earlier AIF/PM-KISAN/PMFBY details or confuse their distinct OTPs.',
                'PM-KISAN status now uses registration numbers supplied at turn 8; do not apply historical phone-only benchmarks.',
                'Chat routes indexed scheme information to search_schemes, not the legacy get_scheme_info route; voice has its own scheme routes.',
                'Voice now requests feedback before closing; the historical immediate-farewell benchmark expectation is obsolete.',
                'For chat AgriStack data, the session consent metadata below determines authorization, not an inferred assertion.',
                'Questions, clarification and honest unavailable-data answers are legitimate. Evaluate actual returned evidence, not mock fixture intent.',
            ],
            coverage_goals=extras.get(sid, {}).get('tools', []),
            agristack_consent=extras.get(sid, {}).get('agristack_consent', 'not supplied'),
        )
        if extras.get(sid, {}).get('expected_behavior'):
            reference['principles'].append('reviewed_expected_behavior was checked against official sources for this '
                'session: judge factual correctness against it, even where the retrieved documents are outdated.')
            reference['reviewed_expected_behavior'] = extras[sid]['expected_behavior']
            reference['judge_checks'] = extras[sid].get('judge_checks', [])
        calls = [c for r in turns for c in calls_for(r)]
        if JUDGE_CORE:  # keep the output-language override line, replace the candidate's own prompt
            # The shared core carries the date it was built on; judge with the date the candidate actually saw
            # (its own system prompt, IST), so runs after midnight IST are not judged against yesterday.
            run_date = DATE_RE.search(contract)
            core = core_contract(JUDGE_CORE, calls, sid)
            if run_date:
                core = DATE_RE.sub(run_date.group(0), core, count=1)
            contract = contract.split('\n', 1)[0] + '\n' + core
        budget = int(os.environ.get('JUDGE_EVIDENCE_TOKENS', '0'))
        if os.environ.get('JUDGE_TOTAL_TOKENS'):
            history_text = json.dumps([dict(question=r['question'], answer=r['answer']) for r in turns],
                                      ensure_ascii=False)
            others = (estimate_tokens(contract) + estimate_tokens(history_text) + JUDGE_FIXED_OVERHEAD_TOKENS
                      + estimate_tokens(json.dumps(earlier, ensure_ascii=False)) + estimate_tokens(json.dumps(reference)))
            if sid in english_groups and turns[0]['language'].lower() not in ('en', 'english'):
                others += estimate_tokens(json.dumps(english_groups[sid], ensure_ascii=False))
            budget = max(int(os.environ['JUDGE_TOTAL_TOKENS']) - others, 5000)
        calls = fit_evidence(calls, budget)
        rows.append(dict(
            id=sid, session_id=sid, inference_session=session, product=product, output_language=turns[0]['language'],
            earlier_context=json.dumps(earlier, ensure_ascii=False) if earlier else '',
            question='Evaluate every numbered user message in the full chronological session above.',
            answer='Evaluate every numbered assistant answer in that session, including clarification turns.',
            category=turns[0].get('category', 'General'),
            conversation_history=json.dumps([dict(question_id=r['question_id'], question=r['question'],
                answer=r['answer'], calls=[{k:v for k,v in c.items() if k != 'output'} for c in calls_for(r)])
                for r in turns], ensure_ascii=False),
            english_question=json.dumps(english_questions, ensure_ascii=False),
            tool_calls_raw=calls, tool_calls_evidence=json.dumps(calls,ensure_ascii=False),
            skill_loaders=[], agent_turns=[],
            retrieved_sources='The complete turn-labeled tool outputs are included above; no additional sources.',
            english_reference=english_groups[sid] if turns[0]['language'].lower() not in ('en','english') else None,
            voice_contract=contract, reviewed_tool_reference=json.dumps(reference), inference_error=errors,
        ))
    assert sum(len(json.loads(r['conversation_history'])) for r in rows) == len(records)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run-dir', required=True, type=Path)
    ap.add_argument('--english-reference-dir', required=True, type=Path)
    ap.add_argument('--fixture', required=True, type=Path)
    ap.add_argument('--model-name', required=True)
    args = ap.parse_args()
    assert re.fullmatch(r'[A-Za-z0-9_-]+', args.model_name)
    summary = json.loads((args.run_dir/'summary.json').read_text())
    records = json.loads((args.run_dir/'per_turn.json').read_text())
    for record in records:
        record['language'] = summary['language']
    english = json.loads((args.english_reference_dir/'per_turn.json').read_text())
    with args.fixture.open() as source:
        fixture = list(csv.DictReader(source))
    product = summary['variant']['repo']
    contracts = {}
    for sid in {r['session_id'] for r in records}:
        if product == 'text':
            # Use the actual locale/date/catalog, not a historical English prompt's calendar.
            history = json.loads((args.run_dir/f'{sid}_history.json').read_text())
            # A session whose every turn failed (e.g. prompt overflow) saves no history; the core contract still applies.
            contracts[sid] = next((p['content'] for m in history for p in m['parts'] if p['part_kind']=='system-prompt'), '')
        else:
            code = dict(English='en', Hindi='hi', Marathi='mr', Gujarati='gu', Malayalam='ml',
                        Kannada='kn', Tamil='ta', Telugu='te', Bengali='bn', Odia='od', Punjabi='pa')[summary['language']]
            history = json.loads((args.run_dir/f'{sid}_history.json').read_text())
            # Pydantic serializes rendered instructions on requests, including the
            # actual locale/date. Never judge against an unrelated baseline template.
            contracts[sid] = next(m['instructions'] for m in history if m.get('instructions'))
            from inference.run_capability_comparison import worktree_path
            registry_path = worktree_path(summary['variant'])/'app/core/languages.py'
            language = runpy.run_path(str(registry_path))['LANGUAGES'][code]
            contracts[sid] += ('\n\nVOICE API PRESENTATION CONTRACT: The API (not the model) prepends '
                'these fixed strings to the first user-facing turn: ' + language.hold_message + ' ' +
                language.recording_disclaimer + '\nIt can also emit this fixed delay message: ' +
                language.delay_message + '\nThese platform-generated strings are permitted. Do not '
                'penalize them as model-generated hold messages, poor grammar, fabrication or unsafe '
                'behavior. The no-hold instruction above prohibits additional model-generated promises '
                'to fetch information, not these API-owned notices. The saved answer includes them '
                'because it records the actual user-facing stream.')
    # Keep the requested output language explicit even for long multilingual evidence packets.
    for sid in contracts:
        contracts[sid] = ('Output-language override for this evaluation: ' + summary['language'] +
                         '. This overrides any English-session language setting in the reference contract.\n' + contracts[sid])
    rows = transform(records, fixture, english, contracts, product)
    folder = ROOT/'data/models'/args.model_name
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/'data.json'
    if target.exists():
        raise FileExistsError(target)
    target.write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    sources = [args.run_dir/'per_turn.json', args.english_reference_dir/'per_turn.json', args.fixture, Path(__file__)]
    (folder/'manifest.json').write_text(json.dumps(dict(
        inference=summary, evaluation_unit="judge_group" if any(r.get("judge_group") for r in fixture) else "whole_session", rows=len(rows),
        source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        caveats=['Synthetic backend outputs test routing and grounding, not live backend quality.',
                 'Observed English calls are not ground truth.', 'All tool outputs preserved, including retries.']),indent=2))
    print(target, len(rows), 'sessions')


if __name__ == '__main__':
    main()
