"""Combined, de-duplicated chat benchmark (2026-11-23): current long-session eval + full benchmark + mined failures.

Inference runs the current eval's long conversations as they are (several original sessions chained into one
24-turn chat); every other session runs on its own. Judging is per original session: the fixture's judge_group
column names it, and groups.json gives its coverage tools, skills and (mined cases) reviewed expected behavior.

Selection (selection.json, from select.py): the current eval first, then mined failures with deliberate replays
collapsed, then full-benchmark sessions that are not already >= 50% present and do not exceed 2 sessions per
"same question, different place" template. OTP / id / yes-style turns never count as overlap.

Outputs: text.csv (runner fixture), groups.json (judge metadata), report.json (provenance and counts).
"""
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRANSLATIONS = json.loads((HERE / 'translations.json').read_text()) if (HERE / 'translations.json').exists() else {}
ROOT = HERE.parents[1]
CURRENT = ROOT / 'data/history_window_20261120'
FULL = ROOT / 'inference/benchmark_questions.csv'
MINED = ROOT / 'data/prod_failure_hunt_20261103/benchmark_questions_prod_failures.csv'
CASES = ROOT / 'data/prod_failure_hunt_20261103/cases.json'
LANGS = ['english', 'hindi', 'marathi', 'gujarati', 'malayalam', 'kannada', 'tamil', 'telugu', 'bengali',
         'assamese', 'odia', 'punjabi', 'maithili']
CORE = json.loads((CURRENT / 'judge_core.json').read_text())

# Long conversations 901-905 share one 24-turn script (verified by position and category): original sessions.
LONG_GROUPS = [('mandi', [1], ['forward_geocode', 'get_mandi_prices']),
               ('weather', [2], ['forward_geocode', 'weather_forecast']),
               ('crop-advice', [3], ['search_advisory']),
               ('pest-advice', [4], ['search_advisory']),
               ('video', [5], ['search_video']),
               ('scheme-info', [6], ['search_schemes']),
               ('soil-card', [7], ['check_shc_status']),
               ('pmkisan-status', [8, 9], ['initiate_pm_kisan_status_check', 'check_pm_kisan_status_with_otp']),
               ('pmfby-status', [10, 11], ['initiate_pmfby_status_check', 'check_pmfby_status_with_otp']),
               ('pmkisan-grievance', [12, 13, 14, 15, 16], ['pmkisan_grievance_send_otp', 'pmkisan_grievance_status']),
               ('pmfby-grievance', [17, 18, 19, 20, 21, 22, 23],
                ['initiate_pmfby_grievance_otp', 'check_pmfby_grievance_otp', 'pmfby_submit_grievance',
                 'pmfby_grievance_status']),
               ('closing', [24], [])]
# The five chats are not the same script at every position: these turns ask something else (found in the
# 2026-11-23 loss review). 904-3 "fertilizer schedule for tomato" stays crop advice (general timing).
LONG_OVERRIDES = {'902-2': ('crop-watering', ['search_advisory']),
                  '903-3': ('livestock', ['search_documents']),
                  '904-4': ('livestock', ['search_documents'])}

# Per-unit judge context (sent only with that unit as reviewed_expected_behavior; the judge core stays generic).
# NDKSP / AIF drip have a dedicated tool and the scheme skill forbids falling back to search for them (2026-11-24).
NDKSP_NOTE = ('Nanaji Deshmukh Krishi Sanjivani Prakalp (NDKSP) drip irrigation, NDKSP farm pond lining and AIF drip '
              'irrigation have a dedicated tool, call_maha_vistaar_network. The product forbids using search_schemes or '
              'search_documents for them. If that tool returns no details, the correct reply says the details are not '
              'available right now; not falling back to another search is correct, not a failure.')
UNIT_NOTES = {
    '2111': (['call_maha_vistaar_network'], NDKSP_NOTE),
    '2064': (['call_maha_vistaar_network'], NDKSP_NOTE + ' A farm pond scheme in a Maharashtra district maps to NDKSP '
             'farm pond lining; asking the farmer to confirm the scheme first is also acceptable.'),
    '2053': (['call_maha_vistaar_network'], NDKSP_NOTE + ' A farm pond scheme in a Maharashtra district maps to NDKSP '
             'farm pond lining; asking the farmer to confirm the scheme first is also acceptable.'),
    '1007': (None, NDKSP_NOTE),
    # SATHI's catalogue has no tapioca/cassava (tuber group H11: potato, colocasia, yams, turmeric, ginger).
    '2181': (None, 'Tapioca (cassava) is not in the SATHI seed catalogue; colocasia and yams are different crops. '
             'Saying SATHI has no certified tapioca seed data, or asking which listed crop the farmer means, is '
             'correct and not a failure to use the tool data.'),
    '2004': ([], 'Bare "drip irrigation" with no scheme named is covered by three schemes (national PDMC via '
             'search_schemes, NDKSP drip and AIF drip via call_maha_vistaar_network). The product rule is to ask which '
             'one the farmer means before calling a tool; Wardha is in Maharashtra, so going straight to the NDKSP or '
             'PDMC lookup is also acceptable. Never default to search_documents.'),
}


# Mined date rewrites: same intent, a past week with live mandi data (the hunt's week used its own mock).
DATE_EDITS = [('29 October through 4 November 2026', '17 November through 23 November 2026'),
              ('29 अक्टूबर से 4 नवंबर 2026', '17 नवंबर से 23 नवंबर 2026'),
              (' including today (4 November 2026)', ' including today'),
              ('आज (4 नवंबर 2026) समेत', 'आज समेत'),
              # Source typo in benchmark_questions.csv (Gujarati 2049): Akota for Akola (English/Hindi say Akola).
              ('અકોટા', 'અકોલા')]


def tools_for(text, category):
    """Coverage goals for an original session, from its questions (reviewed by hand for multi-turn sessions)."""
    t = text.lower()
    tools = []
    add = lambda *names: tools.extend(n for n in names if n not in tools)
    pmkisan = re.search(r'pm[- ]?kisan', t) and 'maandhan' not in t
    insurance = re.search(r'pmfby|crop insurance|fasal bima', t)
    complaint = re.search(r'grievance|complaint|report that', t)
    complaint_status = re.search(r'status of (my |the )?(pm-kisan )?(grievance|complaint)|grievance been resolved|'
                                 r'complaint i filed', t)
    account_status = re.search(r'beneficiary status|installment status|ekyc|how many pm-kisan installments|'
                               r'installments i have|registered under|check my pm-kisan status|pm kisan ka status', t)
    if pmkisan and account_status:
        add('initiate_pm_kisan_status_check', 'check_pm_kisan_status_with_otp')
    if pmkisan and complaint_status:
        # Filing a new PM-KISAN grievance is switched off (fixed "not available" reply, no tool); status still works.
        add('pmkisan_grievance_send_otp', 'pmkisan_grievance_status')
    if insurance and re.search(r'policy status|claim status|policy for|check my pmfby|check if i have', t):
        add('initiate_pmfby_status_check', 'check_pmfby_status_with_otp')
    if insurance and complaint and re.search(r'otp|phone|mobile', t):
        add('initiate_pmfby_grievance_otp', 'check_pmfby_grievance_otp', 'pmfby_submit_grievance')
    if re.search(r'soil health card|soil report|this card', t) and re.search(r'phone|mobile|my soil|show me my', t):
        add('check_shc_status')
    if re.search(r'gfr|fertili[sz]er recommendation', t):
        add('gfr_get_crop_registries', 'gfr_get_recommendations')
    if re.search(r'sathi|certified .*seed|seed dealers', t):
        add('get_sathi_crop_groups', 'list_sathi_crops_in_group', 'search_sathi_seed_availability')
    if re.search(r'mandi|market price|prices? (for|of)|rate of|rates of|modal price|price in|onion prices|sell first', t) \
            or category == 'Mandi_prices':
        if not re.search(r'\d+\.\d+, \d+\.\d+', t):
            add('forward_geocode')
        add('get_mandi_prices')
    if re.search(r'weather|rain|temperature|forecast', t) or category == 'Weather_Forecast':
        add('forward_geocode', 'weather_forecast')
    if re.search(r'video', t):
        add('search_video')
    if re.search(r'scheme|yojana|subsidy|eligib|documents (are )?required|how to apply|last date|pension|'
                 r'kisan credit card|\bkcc\b|pmksy|drip irrigation|farmer id|how much support|surrender|helpline|'
                 r'collateral|loan', t) or category in ('Scheme_advisory', 'Scheme_actions'):
        if not any(x.startswith(('initiate_', 'check_', 'pmkisan_', 'pmfby_', 'gfr_', 'get_sathi')) for x in tools) \
                and not (pmkisan and complaint and not tools):
            add('search_schemes')
    if category in ('Advisory', 'Agri_services') and not any(
            x.startswith(('initiate_', 'check_', 'pmkisan_', 'pmfby_', 'gfr_', 'get_sathi', 'get_mandi', 'weather'))
            for x in tools) and not (pmkisan and complaint) and 'search_schemes' not in tools:
        livestock = re.search(r'calf|calves|buffalo|cow|cattle|goat|poultry|milk|deworm|animal', t)
        add('search_documents' if livestock else 'search_advisory')
    return tools


def skills_for(tools, text=''):
    chosen = [s for name in tools for s in CORE['tool_skills'].get(name, [])[:1]]
    # Filing a PM-KISAN grievance expects no tool call, but the judge needs that skill's "lodge service is not
    # available" override to score it (2166/2167 were marked down for not calling a tool).
    t = text.lower()
    if re.search(r'pm[- ]?kisan', t) and re.search(r'grievance|complaint|report that', t):
        chosen.append('pm_kisan.grievances')
    return list(dict.fromkeys(chosen))


def main():
    selection = json.loads((HERE / 'selection.json').read_text())
    kept = {(src, sid) for src, sid in selection['kept']}
    cases = json.loads(CASES.read_text())
    case_by_id = {c['id']: c for c in cases['cases']}
    aliases = cases['benchmark_fixture_case_aliases']
    current = list(csv.DictReader((CURRENT / 'text.csv').open()))
    capability = {s['id']: s for s in json.loads((ROOT / 'inference/capability_sessions.json').read_text())['sessions']}
    reviewed_skills = json.loads((CURRENT / 'judge_session_skills.json').read_text())
    rows, groups, edits = [], {}, []

    def emit(sid, n, src, src_sid, category, consent, group, case_id, source_row):
        row = dict(session_id=str(sid), question_id=f'{sid}-{n}', category=category, agristack_consent=consent,
                   judge_group=group, source=src, source_session=src_sid, case_id=case_id)
        for lang in LANGS:
            text = (source_row.get('question_' + lang) or '').strip()
            for old, new in DATE_EDITS:
                if old in text:
                    edits.append(dict(question_id=row['question_id'], language=lang, old=old, new=new))
                    text = text.replace(old, new)
            row['question_' + lang] = text
        # Missing translations come from translate_missing.py (Gemma), keyed by the final English text.
        row['translated'] = ''
        for lang in LANGS:
            cached = TRANSLATIONS.get(f"{lang}\t{row['question_english']}")
            if not row['question_' + lang] and cached:
                row['question_' + lang] = cached
                row['translated'] += (' ' if row['translated'] else '') + lang
        rows.append(row)

    # 1. Current eval: short sessions as they are; long conversations split into original sessions for judging.
    by_sid = defaultdict(list)
    for r in current:
        by_sid[r['session_id']].append(r)
    for sid, turns in sorted(by_sid.items(), key=lambda x: int(x[0])):
        turns.sort(key=lambda r: int(r['question_id'].rsplit('-', 1)[-1]))
        if int(sid) < 1000:
            position = {p: (f'{sid}-{name}', tools) for name, positions, tools in LONG_GROUPS for p in positions}
            for i, r in enumerate(turns, 1):
                group, tools = position[i]
                if f'{sid}-{i}' in LONG_OVERRIDES:
                    name, tools = LONG_OVERRIDES[f'{sid}-{i}']
                    group = f'{sid}-{name}'
                emit(sid, i, 'current-long', sid, r['category'], r.get('agristack_consent', ''), group, '', r)
                groups.setdefault(group, dict(session_id=sid, source='current-long', source_session=sid,
                                              tools=tools, skills=skills_for(tools), why=f'Long conversation {sid}: '
                                              + group.split('-', 1)[1], agristack_consent='not supplied'))
        else:
            for i, r in enumerate(turns, 1):
                emit(sid, i, 'current-short', sid, r['category'], r.get('agristack_consent', ''), sid, '', r)
            info = capability.get(sid, {})
            tools = [{'get_scheme_info': 'search_schemes'}.get(x, x) for x in info.get('tools', [])] or [t for t in CORE['tools'] if any(
                t in CORE['tool_skills'] and s in CORE['tool_skills'][t] for s in reviewed_skills[sid]['skills'])]
            groups[sid] = dict(session_id=sid, source='current-short', source_session=sid, tools=tools,
                               skills=reviewed_skills[sid]['skills'], why=reviewed_skills[sid]['why'],
                               agristack_consent=info.get('agristack_consent', turns[0].get('agristack_consent')
                                                          or 'not supplied'))
    # 2. Mined production failures (deliberate replays collapsed by select.py), ids 3001+.
    mined = defaultdict(list)
    for r in csv.DictReader(MINED.open()):
        if ('mined', r['session_id']) in kept:
            mined[r['session_id']].append(r)
    for index, (src_sid, turns) in enumerate(sorted(mined.items()), 1):
        sid = 3000 + index
        case_id = turns[0]['case_id']
        case = case_by_id.get(case_id) or case_by_id.get((aliases.get(case_id) or [''])[0])
        assert case, case_id
        category = 'Mandi_prices' if case_id.startswith('MANDI') else 'Scheme_advisory'
        for i, r in enumerate(turns, 1):
            emit(sid, i, 'mined', src_sid, category, '', str(sid), case_id, r)
        text = ' '.join(r['question_english'] for r in turns)
        tools = [] if case_id == 'PMKISAN-STATUS-COUNT-HI-001' else tools_for(text, category)  # reasons on a quoted status
        expected = case['expected']
        if case_id.startswith(('MANDI', 'LONG-MULTITOPIC', 'TOPIC-SWITCH', 'PMFBY-TOPIC')):
            # The hunt's mandi numbers came from its own synthetic mock; this benchmark uses live dev prices.
            expected = ' '.join(s for s in re.split(r'(?<=[.;])\s+', expected)
                                if not re.search(r'synthetic|fixture|ground truth|/7|\d,\d{3}\.\d\d', s, re.I))
            expected += (' Prices are live dev-network data: judge arithmetic, ranking, date and market attribution '
                         'against the prices this candidate actually received; days without a reported price are '
                         'not zero.')
        for old, new in [('29 Oct–4 Nov 2026', '17–23 Nov 2026'), ('As of 4 November 2026', 'As of 23 November 2026')]:
            expected = expected.replace(old, new)
        groups[str(sid)] = dict(session_id=str(sid), source='mined', source_session=src_sid, case_id=case_id,
                                tools=tools, skills=skills_for(tools), why=case.get('observed_failure', '')[:300],
                                agristack_consent='not supplied', expected_behavior=expected,
                                judge_checks=case.get('judge_checks', []))
    # 3. Full benchmark sessions, ids 2000 + original id.
    full = defaultdict(list)
    for r in csv.DictReader(FULL.open()):
        if ('full', r['session_id']) in kept:
            full[r['session_id']].append(r)
    for src_sid, turns in sorted(full.items(), key=lambda x: int(x[0])):
        sid = 2000 + int(src_sid)
        turns.sort(key=lambda r: int(r['question_id'].rsplit('-', 1)[-1]) if '-' in r['question_id'] else 0)
        for i, r in enumerate(turns, 1):
            emit(sid, i, 'full', src_sid, r['category'], '', str(sid), '', r)
        tools = tools_for(' '.join(r['question_english'] for r in turns), turns[0]['category'])
        groups[str(sid)] = dict(session_id=str(sid), source='full', source_session=src_sid, tools=tools,
                                skills=skills_for(tools, ' '.join(r['question_english'] for r in turns)),
                                why=f'Full benchmark session {src_sid} '
                                f'({turns[0]["category"]}, {len(turns)} turns)', agristack_consent='not supplied')
    # 4. FFS fertilizer purchase (ffs_sessions_en.json; runs only against the local FFS mock), ids 4001+.
    #    Other languages come from translate_missing.py like every other missing translation.
    ffs_spec = json.loads((HERE / 'ffs_sessions_en.json').read_text())
    for index, s in enumerate(ffs_spec['sessions'], 1):
        sid = 4000 + index
        for i, q in enumerate(s['turns'], 1):
            emit(sid, i, 'ffs', s['id'], 'Agri_services', '', str(sid), s['id'], {'question_english': q})
        groups[str(sid)] = dict(session_id=str(sid), source='ffs', source_session=s['id'], case_id=s['id'],
                                tools=s['tools'], skills=list(dict.fromkeys(['ffs.booking'] + skills_for(s['tools']))),
                                why=f"FFS {s['kind']}: {s['source']}", agristack_consent='not supplied',
                                expected_behavior=s['expected_behavior'] + ' FFS responses come from a fixed mock: '
                                + json.dumps(ffs_spec['mock_fixture']),
                                judge_checks=['ffs_flow', 'no_false_booking_claims', 'one_confirmation'])
    for sid, (tools, note) in UNIT_NOTES.items():
        tools = groups[sid]['tools'] if tools is None else tools  # None: keep the unit's own expected tools
        groups[sid].update(tools=tools, skills=skills_for(tools) or ['schemes.information'], expected_behavior=note,
                           judge_checks=groups[sid].get('judge_checks', []) + ['scheme_routing'])
    # Checks.
    assert len({r['question_id'] for r in rows}) == len(rows)
    assert set(groups) == {r['judge_group'] for r in rows}
    unknown = {t for g in groups.values() for t in g['tools']} - set(CORE['tools'])
    assert not unknown, unknown
    with (HERE / 'text.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n"); w.writeheader(); w.writerows(rows)
    (HERE / 'groups.json').write_text(json.dumps(groups, ensure_ascii=False, indent=1))
    src = Counter(r['source'] for r in rows)
    report = dict(questions=len(rows), questions_by_source=src, inference_sessions=len({r['session_id'] for r in rows}),
                  judge_units=len(groups), judge_units_by_source=Counter(g['source'] for g in groups.values()),
                  missing={l: sum(not r['question_' + l] for r in rows) for l in LANGS},
                  translated={l: sum(l in r['translated'].split() for r in rows) for l in LANGS}, date_edits=edits,
                  untooled_units=[k for k, g in groups.items() if not g['tools']],
                  dropped=selection['dropped'])
    (HERE / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k not in ('dropped', 'date_edits')}, indent=1))
    print('date edits', len(edits))


if __name__ == '__main__':
    main()
