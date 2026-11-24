"""Orchestrate the existing scenario runner, preserving Redis tool evidence per pass.

No direct agent calls. APIs must be started by setup_capability_apis.py, and the
bounded Gemma proxy must be running. Existing completed passes are never overwritten.
"""
import argparse
import ast
from collections import Counter
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/prompt_capabilities/expanded'
csv.field_size_limit(100_000_000)
GATEWAY = os.environ.get('BENCHMARK_GATEWAY', 'http://127.0.0.1:18867')


def worktree_path(variant):
    base = Path(os.environ.get('BENCHMARK_WORKTREES_ROOT', ROOT.parent / 'worktrees'))
    return Path(variant['worktree_path']) if variant.get('worktree_path') else base / variant['worktree']
GATEWAY_LIMIT = int(os.environ.get('BENCHMARK_LIMIT', '7'))
# Sessions run at once by one runner process (languages can run as separate processes).
SESSION_CONCURRENCY = int(os.environ.get('BENCHMARK_SESSION_CONCURRENCY', str(min(5, GATEWAY_LIMIT))))


def export_pass(directory, variant, fixture, language, commit):
    logfile = (directory / 'run.log').read_text()
    run_id = re.search(r'run_uuid: (\w+)', logfile)[1]
    outputs = [p for p in directory.glob('*.csv') if p.name != 'fixture.csv']
    assert len(outputs) == 1, outputs
    with outputs[0].open() as source:
        rows = list(csv.DictReader(source))
    with fixture.open() as source:
        expected = list(csv.DictReader(source))
    column = 'question_' + language.lower()
    # Planned questions are sent verbatim; simulated-farmer reply turns ("<id>.r<k>") come on top of them.
    # Planned questions are sent verbatim (or skipped as redundant by the simulated farmer); simulated reply turns
    # ("<id>.r<k>") come on top of them.
    planned_sent = {r['question_id']:r['question'] for r in rows if r.get('kind', 'planned') != 'reply'}
    fixture_questions = {r['question_id']:r[column] for r in expected}
    assert all(fixture_questions.get(k) == v for k, v in planned_sent.items())
    if not any(r.get('kind') for r in rows):  # fixed-question run: every planned question was sent
        assert planned_sent == fixture_questions
    code = dict(English='en', Hindi='hi', Marathi='mr', Gujarati='gu', Malayalam='ml', Kannada='kn',
                Tamil='ta', Telugu='te', Bengali='bn', Assamese='as', Odia='or', Punjabi='pa', Maithili='mai')[language]
    if variant['repo'] == 'voice' and code == 'or':
        code = 'od'
    records = []
    calls_count = Counter()
    for sid in sorted({r['session_id'] for r in rows}):
        session_rows = [r for r in rows if r['session_id'] == sid]
        session_rows.sort(key=lambda r:int(r['turn']) if str(r.get('turn') or '').isdigit()
                          else int(r['question_id'].rsplit('-',1)[-1]))
        prefix = variant.get('redis_key_prefix', f"cap-{variant['repo']}-{variant['variant']}-")
        key = f"{prefix}{run_id}_{code}_{sid}__SVA"
        raw = subprocess.check_output(['docker','exec',variant['redis_container'],
                                      'redis-cli','-n',str(variant['redis_db']),'--raw','GET',key])
        history = (json.loads(raw) if raw.strip() else None) or []
        assert isinstance(history, list), 'Unexpected Redis history format'
        (directory / f'{sid}_history.json').write_text(json.dumps(history,ensure_ascii=False))
        found = []
        current = None
        for message in history:
            for part in message.get('parts', []):
                kind = part.get('part_kind')
                if kind == 'user-prompt':
                    if len(found) < len(session_rows) and session_rows[len(found)]['question'] in str(part.get('content','')):
                        current = dict(session_rows[len(found)], calls=[], returns=[], retries=[])
                        found.append(current)
                    else:
                        current = None
                elif current is not None:
                    if kind == 'tool-call':
                        current['calls'].append(part)
                        calls_count[part['tool_name']] += 1
                    elif kind == 'tool-return': current['returns'].append(part)
                    elif kind == 'retry-prompt': current['retries'].append(part)
        # A failed stream can end the scenario before later turns reach the API.
        # Never invent tool evidence for those turns or discard earlier successes.
        missing = session_rows[len(found):]
        assert all(row['answer'].startswith('ERROR:') for row in missing), (sid,len(found),len(session_rows))
        found.extend(dict(row, calls=[], returns=[], retries=[], history_missing=True) for row in missing)
        for row in found:
            # Session-level Langfuse fields are unused: Redis supplies exact per-turn evidence.
            row.pop('tool_calls',None)
            row.pop('tool_outputs',None)
        records.extend(found)
    registry = worktree_path(variant)/'agents/tools/__init__.py'
    tools = registered_tools(registry)
    loaders = {'load_skill'} if (registry.parents[1] / 'skills.py').exists() else set()
    # Invalid model calls are benchmark evidence, not an exporter failure. The
    # agent rejects them; keep their retry messages for the native tool-call judge.
    unregistered_calls = {name: count for name, count in calls_count.items()
                          if name not in tools | loaders}
    (directory / 'per_turn.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
    summary = dict(variant=variant, language=language, commit=commit, run_uuid=run_id, turns=len(rows),
                   errors=sum(not r['answer'] or r['answer'].startswith('ERROR') for r in rows),
                   calls=dict(calls_count), unregistered_calls=unregistered_calls,
                   unobserved_tools=sorted(tools-set(calls_count)),
                   fixture_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest())
    (directory / 'summary.json').write_text(json.dumps(summary,indent=2))
    print(variant['repo'],variant['variant'],language,'turns',len(rows),'errors',summary['errors'],
          'tools',len(calls_count),flush=True)


def registered_tools(registry):
    tree = ast.parse(registry.read_text())
    bindings = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'TOOLS' for t in node.targets))
    return {node.args[0].id for node in bindings.elts
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Tool'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--languages',default='English')
    parser.add_argument('--repos',default='text,voice')
    parser.add_argument('--variants',default='after,before')
    parser.add_argument('--label',default='paired')
    parser.add_argument('--english-fixture',action='store_true')
    parser.add_argument('--data-dir', type=Path, default=DATA)
    parser.add_argument('--fixture-assets-dir', type=Path, default=DATA)
    args = parser.parse_args()
    data = args.data_dir.resolve()
    assert re.fullmatch(r'[A-Za-z0-9_-]+',args.label)
    requested = args.languages.split(',')
    variants = json.loads(Path(os.environ.get('BENCHMARK_VARIANTS_FILE', data / 'variants.json')).read_text())
    env = dict(os.environ,
               SCENARIO_GEMMA_BASE_URL=GATEWAY + '/v1',SCENARIO_GEMMA_MODEL='openai/google/gemma-4-31b-it',
               LANGFUSE_BASE_URL='',LANGFUSE_PUBLIC_KEY='',LANGFUSE_SECRET_KEY='',PYTHONUNBUFFERED='1')
    # Simulated-farmer profiles for every multi-question session (data/sim_specs.json, from build_sim_specs.py).
    specs = data / 'sim_specs.json'
    if os.environ.get('BENCHMARK_FIXED_QUESTIONS') != '1':
        assert specs.exists(), f'{specs} missing: multi-question sessions need simulated-farmer profiles'
        env['SIM_SPECS'] = str(specs)
    health = httpx.get(GATEWAY + '/health').json()
    assert health['limit'] == GATEWAY_LIMIT
    for language_index,language in enumerate(requested):
        order = args.variants.split(',')
        if language_index % 2:
            order.reverse()
        for repo in args.repos.split(','):
            if repo=='voice' and language in ['Assamese','Maithili']:
                continue
            fixture = data / (repo + ('_english' if args.english_fixture else '') + '.csv')
            for name in order:
                variant = next(v for v in variants if v['repo']==repo and v['variant']==name)
                directory = data / args.label / repo / name / language.lower()
                commit = subprocess.check_output(['git','-C',str(worktree_path(variant)),
                                                  'rev-parse','HEAD'],text=True).strip()
                if (directory / 'summary.json').exists():
                    old = json.loads((directory / 'summary.json').read_text())
                    assert old['commit']==commit and old['fixture_sha256']==hashlib.sha256(fixture.read_bytes()).hexdigest()
                    print('Already exported',directory,flush=True)
                    continue
                directory.mkdir(parents=True,exist_ok=True)
                snapshot = directory/'fixture.csv'
                if snapshot.exists():
                    assert snapshot.read_bytes() == fixture.read_bytes(), 'Do not overwrite a different fixture'
                else:
                    shutil.copyfile(fixture, snapshot)
                sources = [args.fixture_assets_dir/p for p in ['mock_config.json', 'common_registry.json', 'catalog.json']]
                sources += [data/'models.yaml', Path(__file__), ROOT/'inference/run_simulated_scenario.py']
                sources += [specs] if specs.exists() else []
                sources += list((ROOT/'mock_server/beckn_search_proxy').glob('*.py'))
                sources += list((ROOT/'mock_server/capability_aux').glob('*.py'))
                (directory/'source_hashes.json').write_text(json.dumps({
                    str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},indent=2))
                base = f"http://127.0.0.1:{variant['port']}"
                httpx.get(base+'/api/health/live').raise_for_status()
                if repo=='text':
                    response = httpx.post(base+'/api/token',json={})
                    response.raise_for_status()
                    env['TOKEN'] = response.json()['token']
                else:
                    env['TOKEN'] = ''
                env.update(SCENARIO_INPUT_CSV=str(fixture), SCENARIO_OUTPUT_DIR=str(directory))
                print('Starting',repo,name,language,flush=True)
                with (directory/'run.log').open('w') as log:
                    subprocess.run([sys.executable,'inference/run_simulated_scenario.py','--api-kind',repo,
                        '--base-url',base,'--languages',language,'--concurrency',str(SESSION_CONCURRENCY),
                        # Multi-question sessions use the simulated farmer (user rule 2026-11-24).
                        '--fixed-questions' if os.environ.get('BENCHMARK_FIXED_QUESTIONS') == '1' else '--goal-driven'],
                        cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,check=True)
                export_pass(directory,variant,fixture,language,commit)


if __name__=='__main__':
    main()
