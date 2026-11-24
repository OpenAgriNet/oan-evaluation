"""Paired scenario evidence and native OAN metric CSVs; call equality is not accuracy."""
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]


def write_csv(path, rows):
    if not rows:
        return
    with path.open('w') as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def signature(row, names_only=False):
    result = []
    for call in row['calls']:
        if names_only:
            result.append(call['tool_name'])
        else:
            args = call['args']
            if isinstance(args,str):
                try:
                    args=json.loads(args)
                except ValueError:
                    pass
            result.append((call['tool_name'],args))
    return result


def returned_evidence(row):
    returns = {r['tool_call_id']:r['content'] for r in row['returns']}
    retries = {r['tool_call_id']:r['content'] for r in row['retries'] if r.get('tool_call_id')}
    return [(c['tool_name'],returns.get(c['tool_call_id']),retries.get(c['tool_call_id'])) for c in row['calls']]


def compare(directory):
    summaries, pairs, tools = [], [], []
    for repo in ['text','voice']:
        for before in sorted((directory/repo/'before').glob('*/per_turn.json')):
            language = before.parent.name
            after = directory/repo/'after'/language/'per_turn.json'
            if not after.exists():
                continue
            sides = [json.loads(p.read_text()) for p in (before,after)]
            b,a = [{r['question_id']:r for r in side} for side in sides]
            assert b.keys()==a.keys(), (repo,language,'Unpaired question IDs')
            current = []
            for identifier in b:
                assert b[identifier]['question'] == a[identifier]['question']
                row = dict(repo=repo, language=language, question_id=identifier,
                    before_calls=json.dumps(signature(b[identifier]),ensure_ascii=False),
                    after_calls=json.dumps(signature(a[identifier]),ensure_ascii=False),
                    same_call_names=signature(b[identifier],True)==signature(a[identifier],True),
                    same_calls_and_arguments=signature(b[identifier])==signature(a[identifier]),
                    same_returned_evidence=returned_evidence(b[identifier])==returned_evidence(a[identifier]),
                    same_answer=b[identifier]['answer']==a[identifier]['answer'],
                    before_answer=b[identifier]['answer'], after_answer=a[identifier]['answer'])
                current.append(row)
            pairs.extend(current)
            counts = [Counter(c['tool_name'] for r in side for c in r['calls']) for side in sides]
            for tool in sorted(counts[0].keys()|counts[1].keys()):
                tools.append(dict(repo=repo,language=language,tool=tool,before=counts[0][tool],
                                  after=counts[1][tool],delta=counts[1][tool]-counts[0][tool]))
            summary = dict(repo=repo,language=language,turns=len(current),
                same_call_names=sum(r['same_call_names'] for r in current),
                same_calls_and_arguments=sum(r['same_calls_and_arguments'] for r in current),
                same_answer=sum(r['same_answer'] for r in current))
            for variant, side in zip(['before','after'], sides):
                summary[variant+'_errors']=sum(not r['answer'] or r['answer'].startswith('ERROR') for r in side)
                summary[variant+'_tool_calls']=sum(len(r['calls']) for r in side)
                summary[variant+'_unique_tools']=len({c['tool_name'] for r in side for c in r['calls']})
                timings=[float(r['latency_answer']) for r in side if r.get('latency_answer')]
                summary[variant+'_median_full_turn_ms']=median(timings) if timings else None
            summaries.append(summary)
    write_csv(directory/'tool_call_comparison.csv',pairs)
    write_csv(directory/'tool_call_counts.csv',tools)
    write_csv(directory/'inference_summary.csv',summaries)
    return summaries


def judges(directory, model_prefix):
    metrics=[]
    for path in sorted((ROOT/'data/models').glob(model_prefix+'*/evaluation.json')):
        # orchestrate_eval writes partial checkpoints to this same filename.
        # The native judge runner emits this marker only after validating completion.
        if not (path.parent/'judge_status.json').is_file():
            continue
        manifest=json.loads((path.parent/'manifest.json').read_text())
        info=manifest['inference']
        for row in json.loads(path.read_text()):
            evidence_hash = hashlib.sha256(json.dumps(row['original'],sort_keys=True,
                                                       ensure_ascii=False).encode()).hexdigest()
            for metric in row['metrics']:
                metrics.append(dict(repo=info['variant']['repo'],variant=info['variant']['variant'],
                    language=info['language'],session_id=row['original']['session_id'],
                    metric=metric['metric_name'],score=metric['score'],reason=metric['reason'],
                    evidence_sha256=evidence_hash))
    write_csv(directory/'judge_scores.csv',metrics)
    groups=defaultdict(dict)
    for row in metrics:
        key=tuple(row[k] for k in ['repo','language','session_id','metric'])
        assert row['variant'] not in groups[key], 'Duplicate candidate metric'
        groups[key][row['variant']]=row
    paired=[]
    for (repo,language,sid,metric), sides in groups.items():
        if set(sides)!= {'before','after'}:
            continue
        before,after=sides['before'],sides['after']
        delta=after['score']-before['score'] if before['score'] is not None and after['score'] is not None else None
        paired.append(dict(repo=repo,language=language,session_id=sid,metric=metric,
            before=before['score'],after=after['score'],delta=delta,
            identical_judge_evidence=before['evidence_sha256']==after['evidence_sha256'],
            before_reason=before['reason'],after_reason=after['reason']))
    write_csv(directory/'judge_paired.csv',paired)
    aggregates=defaultdict(list)
    for row in paired:
        aggregates[(row['repo'],row['language'],row['metric'])].append(row)
    summary=[]
    for (repo,language,metric), rows in aggregates.items():
        valid=[r for r in rows if r['delta'] is not None]
        summary.append(dict(repo=repo,language=language,metric=metric,paired_sessions=len(rows),
            scored_pairs=len(valid),unscored_pairs=len(rows)-len(valid),
            before_mean=mean(r['before'] for r in valid) if valid else None,
            after_mean=mean(r['after'] for r in valid) if valid else None,
            mean_delta=mean(r['delta'] for r in valid) if valid else None,
            improved=sum(r['delta']>0 for r in valid),regressed=sum(r['delta']<0 for r in valid)))
    write_csv(directory/'judge_summary.csv',summary)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',required=True,type=Path)
    parser.add_argument('--judge-model-prefix')
    args=parser.parse_args()
    print(json.dumps(compare(args.run_dir),indent=2))
    if args.judge_model_prefix:
        judges(args.run_dir,args.judge_model_prefix)
