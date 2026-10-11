"""Score the final Amul metrics. Dry run by default; --post explicitly writes scores."""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from evaluation.amul.checks import (
    CHECKS, combine_source_alignment, language_validation, source_validation, ticket_translation,
)
from evaluation.amul.judge import JUDGES, Judge, eval_farmer_context
from evaluation.amul.langfuse_io import Langfuse, load_env_file, load_run

CHECKS_VERSION = 'amul-final-v3'
FINAL_METRICS = [
    'output_hygiene', 'tool_sequencing', 'source_alignment', 'safety_compliance',
    'grammar', 'terminology', 'language_purity', 'fluency', 'task_success',
    'user_context_retention', 'latency_s', 'completeness', 'persona_adherence', 'brevity', 'content_gap',
]


def metric_summary(rows: list[dict]) -> dict:
    summary = {}
    for name in FINAL_METRICS:
        entries = [row for row in rows if row['name'] == name]
        values = [row['value'] for row in entries if row['value'] is not None]
        summary[name] = {
            'attempted': len(entries), 'scored': len(values),
            'null': sum(row['value'] is None and not row.get('error') for row in entries),
            'errors': sum(bool(row.get('error')) for row in entries),
            'mean': round(sum(values) / len(values), 4) if values else None,
            'distribution': dict(Counter(str(value) for value in values)),
        }
    return summary


def finalize_judge(group: str, scores: dict, turn: dict, session: list[dict]) -> dict:
    """Exact validators never average scales or override worse judge failures."""
    scores = dict(scores)
    from evaluation.amul.reliability import deterministic_findings
    findings,_=deterministic_findings(turn,session)
    for name,finding in findings.items():
        if name in scores:
            value,evidence=scores[name]
            scores[name]=(min(value,finding['level']) if value is not None else float(finding['level']),
                          evidence+' Evidence guard: '+finding['reason'])
    if group == 'source_alignment':
        scores['source_alignment'] = combine_source_alignment(scores['source_alignment'], source_validation(turn, session))
    if group == 'gujarati_language':
        if not turn.get('answer_gu'):
            return {name: (None, 'Empty Gujarati reply; metric does not apply.') for name in JUDGES[group]}
        changed = ticket_translation(turn, session)
        if changed:
            score, evidence = scores['grammar']
            scores['grammar'] = (min(score, 2.0) if score is not None else 2.0, evidence + ' ' + changed[1])
    if group == 'user_context_retention' and turn['turn_index'] == 1:
        scores['user_context_retention'] = (None, 'First turn: no earlier farmer message to retain.')
    names = [call.get('name') for call in turn.get('tool_calls') or []]
    if group == 'content_gap' and not {'search_documents', 'get_union_scheme_data', 'get_vistaar_scheme_info'}.intersection(names):
        scores['content_gap'] = (None, 'No knowledge lookup on this turn; earlier results do not make this metric applicable.')
    if group == 'tool_sequencing' and len(names) == 1 and names[0] not in ('create_ai_call', 'create_health_call'):
        scores['tool_sequencing'] = (None, 'One independent lookup; no tool order to judge.')
    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', default='amul-seed-questions')
    parser.add_argument('--run', required=True)
    parser.add_argument('--env-file')
    parser.add_argument('--checks', default=','.join(CHECKS), help='Final code metrics, or none')
    parser.add_argument('--judges', default=','.join(JUDGES), help='Final judge groups, or none')
    parser.add_argument('--farmer-context')
    parser.add_argument('--glossary-container')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--only')
    parser.add_argument('--workers', type=int, default=1, help='Concurrent judge jobs; default 1 for sequential evaluation')
    parser.add_argument('--cache')
    parser.add_argument('--from-cache')
    parser.add_argument('--results-json', help='Incremental results including nulls, errors, evidence and versions')
    parser.add_argument('--resume-results', help='Reuse successful matching judge groups from a prior results JSON')
    parser.add_argument('--post', action='store_true')
    args = parser.parse_args()
    if args.workers < 1 or (args.limit is not None and args.limit < 1):
        parser.error('workers and limit must be positive')
    checks = [] if args.checks == 'none' else [n.strip() for n in args.checks.split(',') if n.strip()]
    judges = [] if args.judges == 'none' else [n.strip() for n in args.judges.split(',') if n.strip()]
    if len(set(checks)) != len(checks) or len(set(judges)) != len(judges):
        parser.error('Duplicate checks or judge groups are not allowed')
    unknown = (set(checks) - set(CHECKS)) | (set(judges) - set(JUDGES))
    if unknown:
        parser.error(f'Unknown final metrics/judges: {sorted(unknown)}')
    load_env_file(args.env_file)
    lf = Langfuse() if args.post or not args.from_cache else None
    turns = json.loads(Path(args.from_cache).read_text(encoding='utf-8')) if args.from_cache else load_run(lf, args.dataset, args.run)
    if args.glossary_container:
        from evaluation.amul.glossary import attach_glossary
        print(f'turns with glossary terms: {attach_glossary(turns, args.glossary_container)}', flush=True)
    if args.cache:
        Path(args.cache).parent.mkdir(parents=True, exist_ok=True)
        Path(args.cache).write_text(json.dumps(turns, ensure_ascii=False, indent=1), encoding='utf-8')
    sessions = defaultdict(list)
    for turn in turns:
        sessions[turn['session_id']].append(turn)
    for session in sessions.values():
        session.sort(key=lambda turn: turn['turn_index'])
    wanted = set(args.only.split(',')) if args.only else None
    targets = [turn for turn in turns if wanted is None or turn.get('question_id') in wanted]
    if wanted and wanted - {turn.get('question_id') for turn in targets}:
        parser.error(f'Unknown question ids: {sorted(wanted - {turn.get("question_id") for turn in targets})}')
    targets = targets[:args.limit] if args.limit else targets
    if not targets:
        parser.error('No target turns')
    print(f'{len(targets)} target turns in {len({t["session_id"] for t in targets})} sessions', flush=True)

    rows, cache = [], {}
    lock = threading.Lock()
    context = Path(args.farmer_context).read_text(encoding='utf-8') if args.farmer_context else eval_farmer_context()
    implementation_hash = hashlib.sha256(''.join(Path(__file__).with_name(name).read_text(encoding='utf-8') for name in ('checks.py', 'judge.py', 'run_amul_eval.py', 'glossary.py','reliability.py')).encode()).hexdigest()
    report = {
        'dataset': args.dataset, 'run': args.run, 'version': CHECKS_VERSION,
        'implementation_sha256': implementation_hash,
        'target_turns': len(targets), 'target_sessions': len({t['session_id'] for t in targets}),
        'checks': checks, 'judges': judges, 'posted': False, 'results': rows, 'judge_cache': cache,
        'judge_audit':{},
    }
    old_cache = {}
    if args.resume_results:
        old = json.loads(Path(args.resume_results).read_text(encoding='utf-8'))
        # Each cache key already verifies model, prompt hash, final-rubric version and all input evidence.
        # Bump CHECKS_VERSION whenever score-composition semantics change; parser-only repairs can reuse valid scores.
        if old.get('version') == CHECKS_VERSION:
            old_cache = old.get('judge_cache', {})
        else:
            print('Resume metric version differs; no old judge results reused.', flush=True)

    def save():
        if not args.results_json:
            return
        report['observed_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['summary'] = metric_summary(rows)
        destination = Path(args.results_json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + '.tmp')
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(destination)

    def make_row(turn, name, value, evidence, version, error=None):
        row = {'trace_id': turn['trace_id'], 'question_id': turn.get('question_id'),
               'session_id': turn['session_id'], 'turn_index': turn['turn_index'],
               'name': name, 'value': value, 'evidence': evidence, 'version': version}
        if error:
            row['error'] = error
        return row

    for turn in targets:
        session = sessions[turn['session_id']]
        turn['language_checks'] = language_validation(turn, session)
        for name in checks:
            outcome = CHECKS[name](turn, session)
            value, evidence = outcome if outcome is not None else (None, 'Metric does not apply.')
            rows.append(make_row(turn, name, value, evidence, CHECKS_VERSION))
    save()

    if judges:
        judge = Judge(context)
        versions = {group: f'{CHECKS_VERSION}/{judge.model}/{judge.prompt_version(group)}' for group in judges}

        def score_group(turn, group):
            session = sessions[turn['session_id']]
            past = [{k: v for k, v in t.items() if k not in ('language_checks','_judge_audit')} for t in session if t['turn_index'] <= turn['turn_index']]
            stable_turn={k:v for k,v in turn.items() if k not in ('language_checks','_judge_audit')}
            fingerprint = hashlib.sha256(json.dumps({'turn': stable_turn, 'session': past, 'profile': context}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            key = f'{turn["trace_id"]}:{group}:{versions[group]}:{fingerprint}'
            if key in old_cache:
                result = old_cache[key]
                # Reapply exact applicability guards even to cached judgments.
                if group in ('content_gap', 'tool_sequencing'):
                    projected = finalize_judge(group, {r['name']: (r['value'], r['evidence']) for r in result}, turn, session)
                    result = [dict(r, value=projected[r['name']][0], evidence=projected[r['name']][1]) for r in result]
            else:
                try:
                    if group == 'user_context_retention' and turn['turn_index'] == 1:
                        judged = {'user_context_retention': (None, 'First turn: no earlier farmer message to retain.')}
                    elif group == 'gujarati_language' and not turn.get('answer_gu'):
                        judged = {name: (None, 'Empty Gujarati reply.') for name in JUDGES[group]}
                    elif group == 'content_gap' and not {'search_documents', 'get_union_scheme_data', 'get_vistaar_scheme_info'}.intersection(c.get('name') for c in turn.get('tool_calls') or []):
                        judged = {'content_gap': (None, 'No knowledge lookup this turn.')}
                    elif group == 'tool_sequencing' and len(turn.get('tool_calls') or []) == 1 and turn['tool_calls'][0].get('name') not in ('create_ai_call', 'create_health_call'):
                        judged = {'tool_sequencing': (None, 'One independent lookup.')}
                    else:
                        judged = judge.run_reliable(group, turn, session)
                    finalized = finalize_judge(group, judged, turn, session)
                    result = [make_row(turn, name, score, evidence, versions[group]) for name, (score, evidence) in finalized.items()]
                except Exception as exc:
                    error = f'{type(exc).__name__}: {exc}'
                    result = [make_row(turn, name, None, 'Judge failed; not a rubric null.', versions[group], error) for name in JUDGES[group]]
            with lock:
                rows.extend(result)
                report['judge_audit'][f"{turn['trace_id']}:{group}"]=turn.get('_judge_audit',{}).get(group,{})
                if not any(row.get('error') for row in result):
                    cache[key] = result
                save()
            return turn, group, result

        completed = 0
        jobs = [(turn, group) for turn in targets for group in judges]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(score_group, turn, group) for turn, group in jobs]
            for future in as_completed(futures):
                turn, group, result = future.result()
                completed += 1
                failures = [row for row in result if row.get('error')]
                if failures:
                    print(f'Judge error: {turn.get("question_id")} / {group}: {failures[0]["error"]}', flush=True)
                if completed % 10 == 0 or completed == len(jobs):
                    print(f'Completed {completed}/{len(jobs)} judge groups', flush=True)

    errors = [row for row in rows if row.get('error')]
    if args.post:
        if errors:
            raise SystemExit('Not posting: resolve judge errors first.')
        by_trace = {}
        for row in rows:
            if row['value'] is None:
                if row['trace_id'] not in by_trace:
                    by_trace[row['trace_id']] = lf.trace(row['trace_id'])
                stable_id = hashlib.sha1(f'{row["trace_id"]}:{row["name"]}'.encode()).hexdigest()
                if any(s.get('id') == stable_id for s in by_trace[row['trace_id']].get('scores') or []):
                    raise SystemExit(f'Not posting: newly null {row["name"]} on {row["question_id"]} has an old score; explicit cleanup required.')
        for row in rows:
            if row['value'] is not None:
                lf.post_score(trace_id=row['trace_id'], name=row['name'], value=row['value'], comment=row['evidence'],
                              metadata={'version': row['version'], 'dataset_run': args.run, 'implementation_sha256': implementation_hash})
        report['posted'] = True
    save()
    print('\nmetric                   scored null errors mean', flush=True)
    for name, summary in metric_summary(rows).items():
        if summary['attempted']:
            mean = '-' if summary['mean'] is None else f'{summary["mean"]:.2f}'
            print(f'{name:25} {summary["scored"]:5} {summary["null"]:4} {summary["errors"]:6} {mean}', flush=True)
    print('Posted final scores.' if args.post else 'Dry run: nothing written to Langfuse.', flush=True)
    if errors:
        raise SystemExit(f'{len(errors)} metric records failed; see results JSON.')


if __name__ == '__main__':
    main()
