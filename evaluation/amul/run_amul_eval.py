"""Score an Amul Langfuse dataset run and (optionally) write the scores back to its traces.

    python -m evaluation.amul.run_amul_eval --run seed-gu-capture-2 --env-file inference/.env
    python -m evaluation.amul.run_amul_eval --run seed-gu-capture-2 --env-file inference/.env --judges grounding --post
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evaluation.amul.checks import CHECKS
from evaluation.amul.judge import JUDGE_REQUIRES, JUDGES, Judge, eval_farmer_context
from evaluation.amul.langfuse_io import Langfuse, load_env_file, load_run

CHECKS_VERSION = "amul-checks-v3"


def _is_low(name: str, score: float) -> bool:
    if name == "latency_s":
        return False
    if name == "glossary_adherence":
        return score < 0.75
    if name in CHECKS:
        return score < (4 if name == "language_mixing" else 1)
    return score <= 3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default="amul-seed-questions")
    parser.add_argument("--run", required=True, help="Langfuse dataset run name")
    parser.add_argument("--env-file", help="File with LANGFUSE_* / LiteLLM keys (values already in the environment win)")
    parser.add_argument("--checks", default=",".join(CHECKS), help="Comma-separated code checks, or 'none'. Options: " + ", ".join(CHECKS))
    parser.add_argument("--judges", default="", help="Comma-separated Gemma judges. Options: " + ", ".join(JUDGES))
    parser.add_argument("--farmer-context", help="JSON file with the farmer profile; defaults to the eval stub's fixture farmer")
    parser.add_argument(
        "--prod-reference",
        action="store_true",
        help="Attach successful knowledge-base tool results from other Amul Agent Dev conversations to turns whose own lookups failed",
    )
    parser.add_argument("--union", default="banas", help="Farmer's milk union, used to rank union-scheme references")
    parser.add_argument(
        "--glossary-container",
        help="Agent container (e.g. amul_app_eval) used to build each answer's mini glossary with the agent's own code; "
        "feeds the terminology judge and glossary_adherence",
    )
    parser.add_argument("--limit", type=int, help="Only judge the first N turns (trial runs)")
    parser.add_argument("--workers", type=int, default=4, help="Parallel judge calls")
    parser.add_argument("--cache", help="Save the extracted turns to this JSON file")
    parser.add_argument("--from-cache", help="Read turns from this JSON file instead of Langfuse")
    parser.add_argument("--post", action="store_true", help="Write scores to Langfuse (default: dry run)")
    args = parser.parse_args()

    load_env_file(args.env_file)
    lf = Langfuse() if (args.post or args.prod_reference or not args.from_cache) else None
    if args.from_cache:
        turns = json.loads(Path(args.from_cache).read_text(encoding="utf-8"))
    else:
        turns = load_run(lf, args.dataset, args.run)
    if args.prod_reference:
        from evaluation.amul.prod_reference import attach_references, load_library

        library = load_library(lf)
        attached = attach_references(turns, library, union=args.union)
        sizes = ", ".join(f"{tool} {len(entries)}" for tool, entries in library.items())
        print(f"reference library: {sizes}; turns with a reference: {attached}")
    if args.glossary_container:
        from evaluation.amul.glossary import attach_glossary

        print(f"turns with glossary terms: {attach_glossary(turns, args.glossary_container)}")
    if args.cache:
        Path(args.cache).parent.mkdir(parents=True, exist_ok=True)
        Path(args.cache).write_text(json.dumps(turns, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(turns)} turns in {len({t['session_id'] for t in turns})} sessions")

    sessions: dict[str, list[dict]] = defaultdict(list)
    for turn in turns:
        sessions[turn["session_id"]].append(turn)

    checks = [] if args.checks == "none" else [n.strip() for n in args.checks.split(",") if n.strip()]
    judges = [n.strip() for n in args.judges.split(",") if n.strip()]
    unknown = (set(checks) - set(CHECKS)) | (set(judges) - set(JUDGES))
    if unknown:
        raise SystemExit(f"Unknown checks/judges: {sorted(unknown)}")

    # (turn, metric name, score, reason, version)
    results: list[tuple[dict, str, float, str, str]] = []
    for turn in turns:
        for name in checks:
            outcome = CHECKS[name](turn, sessions[turn["session_id"]])
            if outcome is not None:
                results.append((turn, name, outcome[0], outcome[1], CHECKS_VERSION))

    if judges:
        context = Path(args.farmer_context).read_text(encoding="utf-8") if args.farmer_context else eval_farmer_context()
        judge = Judge(context)
        targets = turns[: args.limit] if args.limit else turns

        def score_turn(turn: dict) -> list[tuple[dict, str, float, str, str]]:
            rows = []
            for name in judges:
                if JUDGE_REQUIRES.get(name) and not turn.get(JUDGE_REQUIRES[name]):
                    continue
                version = f"{judge.model}/{judge.prompt_version(name)}"
                try:
                    for metric, (score, evidence) in judge.run(name, turn, sessions[turn["session_id"]]).items():
                        if score is not None:
                            rows.append((turn, metric, score, evidence, version))
                except Exception as exc:  # one bad turn should not stop the run
                    print(f"  !! {name} failed on {turn['session_id']} #{turn['turn_index']}: {exc}")
            return rows

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for rows in pool.map(score_turn, targets):
                results.extend(rows)

    totals: dict[str, list[float]] = defaultdict(list)
    for turn, name, score, reason, _ in results:
        totals[name].append(score)
        if _is_low(name, score):
            print(f"  {name:22} {score:>4}  {turn['session_id']} #{turn['turn_index']}: {reason}")

    if args.post:
        for turn, name, score, reason, version in results:
            lf.post_score(
                trace_id=turn["trace_id"],
                name=name,
                value=score,
                comment=reason,
                metadata={"version": version, "dataset_run": args.run},
            )

    print("\nmetric                  scored  mean")
    for name in checks + [m for j in judges for m in JUDGES[j]]:
        values = totals.get(name) or []
        mean = f"{sum(values) / len(values):.2f}" if values else "-"
        print(f"{name:22} {len(values):>7}  {mean}")
    print(f"\n{'posted ' + str(len(results)) + ' scores' if args.post else 'dry run: nothing written to Langfuse'}")


if __name__ == "__main__":
    main()
