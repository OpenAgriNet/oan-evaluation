"""Per-turn mini glossary, built by the agent's own `get_mini_glossary_for_text` inside its container.

The agent's translator receives exactly this list (English term -> approved Gujarati) for each English answer, so
the terminology judge and `glossary_adherence` grade the reply against the same standard.
"""
from __future__ import annotations

import json
import re
import subprocess

# Runs inside the agent container; mirrors app/services/translation.py (_prepare_translation_inputs).
_IN_CONTAINER = """
import json, sys
from agents.tools.terms import get_mini_glossary_for_text
rows = json.load(sys.stdin)
print(json.dumps({r["id"]: get_mini_glossary_for_text(r["text"], threshold=0.90, max_terms=40, target_lang="gu") for r in rows}, ensure_ascii=False))
"""


def attach_glossary(turns: list[dict], container: str) -> int:
    """Adds turn['glossary'] = [[en, gu], ...]. Returns how many turns got at least one term."""
    rows = [{"id": t["trace_id"], "text": t["answer_en"]} for t in turns if t.get("answer_en")]
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-c", _IN_CONTAINER],
        input=json.dumps(rows, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    mini = json.loads(proc.stdout.strip().splitlines()[-1])
    attached = 0
    for turn in turns:
        pairs = [line.split(" -> ", 1) for line in (mini.get(turn["trace_id"]) or "").splitlines() if " -> " in line]
        turn["glossary"] = pairs
        attached += bool(pairs)
    return attached


GU_SUFFIXES = ("વું", "માં", "નું", "ની", "ના", "નો", "ને", "ું", "ી", "ો", "ા", "ે", "ં")


def _variants(gu: str) -> list[str]:
    return [v.strip() for v in re.split(r"[/,;]| અથવા ", gu) if v.strip()]


def _stem(word: str) -> str:
    for suffix in GU_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 2:
            return word[: -len(suffix)]
    return word


def _used(variant: str, answer: str) -> bool:
    """Exact variant, or every word of it present up to an inflection suffix (આવવું matches આવતી)."""
    return variant in answer or all(_stem(w) in answer for w in variant.split())


def glossary_adherence(turn: dict, session: list[dict]) -> tuple[float, str] | None:
    """Share of glossary terms whose approved Gujarati (any listed variant, any inflection) appears in the reply."""
    pairs = turn.get("glossary") or []
    answer = turn.get("answer_gu") or ""
    if not pairs or not answer:
        return None
    missed = [f"{en} -> {gu}" for en, gu in pairs if not any(_used(v, answer) for v in _variants(gu))]
    score = round(1 - len(missed) / len(pairs), 2)
    if not missed:
        return score, f"All {len(pairs)} glossary terms use the approved Gujarati."
    return score, f"{len(pairs) - len(missed)}/{len(pairs)} glossary terms used. Not found: {'; '.join(missed[:5])}."
