"""
Flatten a regular evaluation.json into two CSVs:

  1. combined.csv  — long format, one row per (sample x metric)
  2. pivot.csv     — wide format, one row per sample, one column per metric score

Usage (from the oan-evaluation directory):

    python -m data_transformation.flatten_evaluation_to_csv \\
        --evaluation data/models/gemma_fine_tuned/english_gemma/evaluation.json \\
        --out-dir    data/models/gemma_fine_tuned/english_gemma/metric_csvs

    # run for all four models at once:
    for model in gemma_fine_tuned/english_gemma gemma_fine_tuned/hindi_gemma \\
                 gpt_fine_tuned_prompts/english_gpt gpt_fine_tuned_prompts/hindi_gpt; do
        python -m data_transformation.flatten_evaluation_to_csv \\
            --evaluation data/models/$model/evaluation.json \\
            --out-dir    data/models/$model/metric_csvs
    done
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent

csv.field_size_limit(256 * 1024 * 1024)

ORIGINAL_COLS = [
    "sample_index",
    "session_id",
    "question_id",
    "configuration",
    "output_language",
    "category",
    "num_turns",
    "TTFT_answer",
    "latency_answer",
    "question",
    "answer",
]

METRIC_COLS = [
    "metric_name",
    "score",
    "reason",
    "rubric",
    "elapsed_seconds",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
]


def _extract_original(idx: int, original: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sample_index":    idx,
        "session_id":      original.get("session_id", ""),
        "question_id":     original.get("question_id", original.get("id", "")),
        "configuration":   original.get("configuration", ""),
        "output_language": original.get("output_language", ""),
        "category":        original.get("category", ""),
        "num_turns":       original.get("num_turns", 1),
        "TTFT_answer":     original.get("TTFT_answer", ""),
        "latency_answer":  original.get("latency_answer", ""),
        "question":        original.get("question", ""),
        "answer":          original.get("answer", ""),
    }


def _extract_metric(metric: Dict[str, Any]) -> Dict[str, Any]:
    meta = metric.get("metadata") or {}
    tu   = meta.get("token_usage") or {}
    ce   = meta.get("cost_estimate") or {}
    return {
        "metric_name":     metric.get("metric_name", ""),
        "score":           metric.get("score"),
        "reason":          metric.get("reason", ""),
        "rubric":          meta.get("rubric", ""),
        "elapsed_seconds": meta.get("elapsed_seconds"),
        "input_tokens":    tu.get("input_tokens"),
        "output_tokens":   tu.get("output_tokens"),
        "total_tokens":    tu.get("total_tokens"),
        "cost_usd":        ce.get("cost_usd"),
    }


def flatten_evaluation(
    evaluation_path: Path,
    out_dir: Path,
) -> Dict[str, Any]:
    if not evaluation_path.exists():
        raise FileNotFoundError(f"evaluation.json not found: {evaluation_path}")

    with evaluation_path.open("r", encoding="utf-8") as f:
        data: List[Dict[str, Any]] = json.load(f)

    long_rows: List[Dict[str, Any]] = []
    metrics_seen: List[str] = []
    wide: Dict[int, Dict[str, Any]] = {}

    for idx, item in enumerate(data):
        original = item.get("original") or {}
        orig_fields = _extract_original(idx, original)
        wide.setdefault(idx, dict(orig_fields))

        for metric in item.get("metrics") or []:
            m = _extract_metric(metric)
            long_rows.append({**orig_fields, **m})

            name = m["metric_name"]
            if name and name not in metrics_seen:
                metrics_seen.append(name)
            if name:
                wide[idx][f"{name}__score"]  = m["score"]
                wide[idx][f"{name}__reason"] = m["reason"]

    out_dir.mkdir(parents=True, exist_ok=True)

    # combined (long) CSV
    combined_path = out_dir / "combined.csv"
    long_fieldnames = ORIGINAL_COLS + METRIC_COLS
    with combined_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=long_fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in long_rows:
            w.writerow({k: row.get(k, "") for k in long_fieldnames})

    # pivot (wide) CSV
    pivot_path = out_dir / "pivot.csv"
    metric_score_cols = []
    for name in sorted(metrics_seen):
        metric_score_cols.append(f"{name}__score")
        metric_score_cols.append(f"{name}__reason")
    pivot_fieldnames = ORIGINAL_COLS + metric_score_cols

    with pivot_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=pivot_fieldnames, extrasaction="ignore")
        w.writeheader()
        for idx in sorted(wide.keys()):
            row = wide[idx]
            w.writerow({k: row.get(k, "") for k in pivot_fieldnames})

    return {
        "samples":       len(data),
        "metrics":       sorted(metrics_seen),
        "long_rows":     len(long_rows),
        "combined_path": str(combined_path),
        "pivot_path":    str(pivot_path),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description="Flatten evaluation.json to combined (long) and pivot (wide) CSVs."
    )
    p.add_argument("--evaluation", type=str, required=True, help="Path to evaluation.json")
    p.add_argument("--out-dir",    type=str, required=True, help="Output directory for CSVs")
    args = p.parse_args()

    eval_path = Path(args.evaluation).expanduser().resolve()
    out_dir   = Path(args.out_dir).expanduser().resolve()

    summary = flatten_evaluation(eval_path, out_dir)

    print(f"Evaluation : {eval_path}")
    print(f"Samples    : {summary['samples']}")
    print(f"Metrics    : {', '.join(summary['metrics'])}")
    print(f"Long rows  : {summary['long_rows']}")
    print(f"Combined   : {summary['combined_path']}")
    print(f"Pivot      : {summary['pivot_path']}")


if __name__ == "__main__":
    main()
