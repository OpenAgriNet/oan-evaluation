"""
Collate every model's evaluation.json under data/models/ into one combined CSV —
one row per (model, sample), one column per metric score — for side-by-side
comparison/visualization across models.

Reuses flatten_evaluation_to_csv's per-model flattening, then stacks the wide
(pivot) rows from every model with a `model_name` column prepended.

Usage (from the oan-evaluation directory):

    python -m data_transformation.collate_all_models
    python -m data_transformation.collate_all_models --models-dir data/models --out data/results/all_models.csv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Dict, List

from .flatten_evaluation_to_csv import ORIGINAL_COLS, _extract_metric, _extract_original

REPO_ROOT = Path(__file__).resolve().parent.parent
csv.field_size_limit(256 * 1024 * 1024)


def collate(models_dir: Path, out_path: Path, model_names: list[str] | None = None) -> Dict[str, Any]:
    all_rows: List[Dict[str, Any]] = []
    metrics_seen: List[str] = []
    models_found: List[str] = []

    for model_dir in sorted(p for p in models_dir.iterdir() if p.is_dir()):
        if model_names is not None and model_dir.name not in model_names:
            continue
        eval_path = model_dir / "evaluation.json"
        if not eval_path.exists():
            continue
        models_found.append(model_dir.name)

        import json
        with eval_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        wide: Dict[int, Dict[str, Any]] = {}
        for idx, item in enumerate(data):
            orig_fields = _extract_original(idx, item.get("original") or {})
            row = wide.setdefault(idx, {"model_name": model_dir.name, **orig_fields})
            for metric in item.get("metrics") or []:
                m = _extract_metric(metric)
                name = m["metric_name"]
                if not name:
                    continue
                if name not in metrics_seen:
                    metrics_seen.append(name)
                row[f"{name}__score"] = m["score"]
                row[f"{name}__reason"] = m["reason"]

        all_rows.extend(wide[i] for i in sorted(wide.keys()))

    if model_names is not None and set(models_found) != set(model_names):
        raise ValueError(f"Missing evaluations: {sorted(set(model_names) - set(models_found))}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    metric_cols: List[str] = []
    for name in sorted(metrics_seen):
        metric_cols.append(f"{name}__score")
        metric_cols.append(f"{name}__reason")
    fieldnames = ["model_name"] + ORIGINAL_COLS + metric_cols

    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in all_rows:
            w.writerow({k: row.get(k, "") for k in fieldnames})

    return {"models": models_found, "rows": len(all_rows), "metrics": sorted(metrics_seen), "out_path": str(out_path)}


def main() -> None:
    p = argparse.ArgumentParser(description="Collate all models' evaluation.json into one comparison CSV.")
    p.add_argument("--models-dir", type=str, default=str(REPO_ROOT / "data" / "models"))
    p.add_argument("--out", type=str, default=str(REPO_ROOT / "data" / "results" / "all_models.csv"))
    p.add_argument("--model-names", nargs='+', help="Collate only these model directories")
    args = p.parse_args()

    summary = collate(Path(args.models_dir).expanduser().resolve(), Path(args.out).expanduser().resolve(), args.model_names)

    print(f"Models  : {', '.join(summary['models'])}")
    print(f"Rows    : {summary['rows']}")
    print(f"Metrics : {', '.join(summary['metrics'])}")
    print(f"Output  : {summary['out_path']}")


if __name__ == "__main__":
    main()
