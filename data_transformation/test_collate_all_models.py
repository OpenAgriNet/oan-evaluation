"""Run with python -m unittest data_transformation.test_collate_all_models."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from data_transformation.collate_all_models import collate


class CollationTest(unittest.TestCase):
    def test_batch_selection_and_missing_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / 'models'
            for name in ('current', 'unrelated'):
                folder = models / name
                folder.mkdir(parents=True)
                (folder / 'evaluation.json').write_text(json.dumps([
                    {'original': {'answer': name}, 'metrics': [{'metric_name': 'accuracy', 'score': 4}]}]))
            out = root / 'scores.csv'
            summary = collate(models, out, ['current'])
            self.assertEqual(summary['models'], ['current'])
            with out.open(encoding='utf-8-sig') as stream:
                self.assertEqual([r['model_name'] for r in csv.DictReader(stream)], ['current'])
            saved = out.read_bytes()
            with self.assertRaises(ValueError):
                collate(models, out, ['missing'])
            self.assertEqual(out.read_bytes(), saved)
            self.assertEqual(collate(models, out)['rows'], 2)
