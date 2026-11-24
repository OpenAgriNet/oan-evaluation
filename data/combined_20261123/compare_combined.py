"""Judge results for the combined benchmark: bh-dev (before) vs new setup (state), per source and overall.

Usage: compare_combined.py <prefix> <language> [<language> ...]   (reads data/models/<prefix>_text_<arm>_<lang>)
Writes results_<prefix>.json next to this script and prints the tables.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
ARMS = {'before': 'bh-dev', 'state': 'new setup'}
METRICS = ['accuracy_completeness', 'tool_call_quality', 'no_fabrication', 'language_purity', 'grammar_fluency',
           'context_fit', 'answer_relevance', 'safety_compliance', 'translation_accuracy']
SOURCES = ['current-long', 'current-short', 'mined', 'full', 'ffs']


def main():
    prefix, languages = sys.argv[1], sys.argv[2:]
    groups = json.loads((HERE / 'groups.json').read_text())
    out = {}
    for language in languages:
        scores = {}
        for arm in ARMS:
            path = ROOT / 'data/models' / f'{prefix}_text_{arm}_{language}' / 'evaluation.json'
            scores[arm] = {r['original']['session_id']: {m['metric_name']: m['score'] for m in r['metrics']}
                           for r in json.loads(path.read_text())}
        units = sorted(set(scores['before']) & set(scores['state']))
        table = {}
        for source in ['all'] + SOURCES:
            chosen = [u for u in units if source == 'all' or groups[u]['source'] == source]
            row = {}
            for metric in METRICS:
                for arm in ARMS:
                    values = [scores[arm][u].get(metric) for u in chosen]
                    values = [v for v in values if v is not None]
                    if values:
                        row.setdefault(metric, {})[arm] = round(mean(values), 3)
            acc = [(scores['state'][u].get('accuracy_completeness'), scores['before'][u].get('accuracy_completeness'))
                   for u in chosen]
            acc = [(s, b) for s, b in acc if s is not None and b is not None]
            row['units'] = len(chosen)
            row['accuracy_wins_ties_losses'] = [sum(s > b for s, b in acc), sum(s == b for s, b in acc),
                                                sum(s < b for s, b in acc)]
            table[source] = row
        swings = sorted(((scores['state'][u].get('accuracy_completeness') or 0)
                         - (scores['before'][u].get('accuracy_completeness') or 0), u) for u in units)
        out[language] = dict(table=table, worst=[u for d, u in swings[:12] if d < 0],
                             best=[u for d, u in swings[::-1][:12] if d > 0])
        print(f'\n=== {language}: {len(units)} judge units (bh-dev / new setup) ===')
        for source, row in table.items():
            cells = '  '.join(f"{m[:8]}={row[m]['before']:.2f}/{row[m]['state']:.2f}" for m in
                              ['accuracy_completeness', 'tool_call_quality', 'no_fabrication', 'language_purity']
                              if m in row and len(row[m]) == 2)
            w, t, l = row['accuracy_wins_ties_losses']
            print(f'  {source:14s} n={row["units"]:3d}  {cells}  acc new-vs-bh-dev W/T/L={w}/{t}/{l}')
        print('  biggest losses:', out[language]['worst'])
        print('  biggest wins:  ', out[language]['best'])
    (HERE / f'results_{prefix}.json').write_text(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
