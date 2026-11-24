"""Run the native OAN evaluator on exported paired sessions (Gemma only)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
JUDGE_PYTHON = Path(os.environ.get('BENCHMARK_JUDGE_PYTHON', sys.executable))
GATEWAY = os.environ.get('BENCHMARK_GATEWAY', 'http://127.0.0.1:18867')
GATEWAY_LIMIT = int(os.environ.get('BENCHMARK_LIMIT', '7'))


def validate_results(output, rows, product):
    ranges = dict(accuracy_completeness=(1,4),tool_call_quality=(1,4),no_fabrication=(0,1),
                  translation_accuracy=(1,4),language_purity=(1,5),grammar_fluency=(1,4),
                  context_fit=(0,1),answer_relevance=(1,5),safety_compliance=(0,1))
    if product=='voice':
        ranges['voice_ready_text']=(0,1)
    assert len(output)==len(rows)
    assert {r['original']['session_id'] for r in output}=={r['session_id'] for r in rows}
    for row in output:
        assert len(row['metrics'])==len(ranges)
        assert {m['metric_name'] for m in row['metrics']}==set(ranges)
        for metric in row['metrics']:
            if metric['score'] is not None:
                low,high=ranges[metric['metric_name']]
                assert isinstance(metric['score'],(int,float)) and low<=metric['score']<=high, metric


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--languages', default='', help='Optional comma-separated completed language pairs')
    args = parser.parse_args()
    assert re.fullmatch(r'[A-Za-z0-9_-]+', args.prefix)
    run_dir = args.run_dir.resolve()
    completed = sorted(run_dir.glob('*/*/*/summary.json'))
    if args.languages:
        requested = {name.strip().lower() for name in args.languages.split(',')}
        completed = [p for p in completed if p.parent.name in requested]
    if os.environ.get('BENCHMARK_JUDGE_VARIANTS'):  # only these arms (directory names under <repo>/)
        arms = set(os.environ['BENCHMARK_JUDGE_VARIANTS'].split(','))
        completed = [p for p in completed if p.parent.parent.name in arms]
    assert completed, 'No exported inference passes'
    for summary in completed:
        info = json.loads(summary.read_text())
        repo,variant = info['variant']['repo'],info['variant']['variant']
        language = summary.parent.name
        # Judge only paired passes: some other arm must have finished this language too.
        assert os.environ.get('BENCHMARK_ALLOW_UNPAIRED') == '1' or any(
            other.name != variant and (other/language/'summary.json').exists() for other in (run_dir/repo).iterdir())
        name = f'{args.prefix}_{repo}_{variant}_{language}'
        folder = ROOT/'data/models'/name
        if not (folder/'data.json').exists():
            subprocess.run([sys.executable,'data_transformation/transform_capability_sessions.py',
                '--run-dir',str(summary.parent),'--english-reference-dir',
                os.environ.get('BENCHMARK_ENGLISH_REFERENCE', str(run_dir/repo/'before/english')),
                '--fixture',str(summary.parent/'fixture.csv'),'--model-name',name],cwd=ROOT,check=True)
        rows = json.loads((folder/'data.json').read_text())
        expected_metrics = 10 if repo=='voice' else 9
        result = folder/'evaluation.json'
        if result.exists():
            existing = json.loads(result.read_text())
            if len(existing)==len(rows) and all(len(r['metrics'])==expected_metrics for r in existing):
                validate_results(existing,rows,repo)
                errors=[(r['original']['session_id'],m['metric_name'],m['reason']) for r in existing
                        for m in r['metrics'] if m['score'] is None and not
                        (language=='english' and m['metric_name']=='translation_accuracy')]
                (folder/'judge_status.json').write_text(json.dumps(dict(rows=len(existing),unscored=errors),indent=2))
                print('Existing judge output (not overwritten)',name,flush=True)
                continue
            raise RuntimeError(f'Partial output at {result}; inspect before restarting')
        env = dict(os.environ, SCENARIO_JUDGE_VLLM_BASE_URL=GATEWAY + '/v1',
            SCENARIO_JUDGE_VLLM_MODEL='google/gemma-4-31b-it',CAPABILITY_EVAL_PRODUCT=repo,
            LANGFUSE_PUBLIC_KEY='',LANGFUSE_SECRET_KEY='',LANGFUSE_BASE_URL='')
        env.pop('CAPABILITY_EVAL_SAMPLES',None)
        print('Judging',name,len(rows),'sessions',flush=True)
        sources = list((ROOT/'assets/prompts/metrics/capability_sessions').glob('*.md'))
        sources += [ROOT/'config/pipeline_config_capabilities.py',ROOT/'helpers/llm_helper.py']
        (folder/'judge_manifest.json').write_text(json.dumps(dict(
            model=env['SCENARIO_JUDGE_VLLM_MODEL'], endpoint=env['SCENARIO_JUDGE_VLLM_BASE_URL'],
            temperature=0, seed=42, concurrency_limit=GATEWAY_LIMIT, python=str(JUDGE_PYTHON),
            source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}),indent=2))
        with (folder/'judge.log').open('w') as log:
            subprocess.run([str(JUDGE_PYTHON),'orchestrate_eval.py',
                '--config','config.pipeline_config_capabilities','--model-name',name],
                cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
        output=json.loads(result.read_text())
        validate_results(output,rows,repo)
        errors=[(r['original']['session_id'],m['metric_name'],m['reason']) for r in output
                for m in r['metrics'] if m['score'] is None and not
                (language=='english' and m['metric_name']=='translation_accuracy')]
        (folder/'judge_status.json').write_text(json.dumps(dict(rows=len(output),unscored=errors),indent=2))
        print('Completed',name,'unexpected unscored metrics',len(errors),flush=True)


if __name__=='__main__':
    main()
