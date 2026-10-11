"""Gemma LLM-as-judge for Amul turns. Prompts live in assets/prompts/metrics/amul/."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen

PROMPT_DIR = Path(__file__).resolve().parents[2] / "assets" / "prompts" / "metrics" / "amul"

JUDGES: dict[str, list[str]] = {
    "source_alignment": ["source_alignment"],
    "gujarati_language": ["grammar", "terminology", "language_purity", "fluency"],
    "task_success": ["task_success"],
    "safety": ["safety_compliance"],
    "user_context_retention": ["user_context_retention"],
    "tool_sequencing": ["tool_sequencing"],
    "completeness": ["completeness"],
    "persona": ["persona_adherence"],
    "brevity": ["brevity"],
    "content_gap": ["content_gap"],
}

# Knowledge results run to ~90k chars (47 union schemes); truncating them makes supported facts look invented.
MAX_TOOL_OUTPUT_CHARS = 120_000
MAX_HISTORY_TOOL_CHARS = 30_000
RELIABILITY_VERSION = 'evidence-contract-v3'


def eval_farmer_context() -> str:
    """Farmer profile and technicians the eval stub serves; production runs should pass the real profile."""
    from mock_server.amul_pashu_mock.server import FARMER, TECHNICIANS

    return json.dumps({"farmer": FARMER, "ai_technicians": TECHNICIANS}, ensure_ascii=False, indent=1)


def _fmt_tool_calls(turn: dict) -> str:
    if not turn["tool_calls"]:
        return "(no tools called in this turn)"
    lines = []
    for i, call in enumerate(turn["tool_calls"], 1):
        output = call.get("output")
        output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        if len(output) > MAX_TOOL_OUTPUT_CHARS:
            output = output[:MAX_TOOL_OUTPUT_CHARS] + " ...[truncated]"
        lines.append(f"{i}. {call['name']}({json.dumps(call.get('input'), ensure_ascii=False)})\n   result: {output}")
    return "\n".join(lines)


def _fmt_history(turn: dict, session: list[dict], *, source_review: bool = False) -> str:
    earlier = [t for t in session if t["turn_index"] < turn["turn_index"]]
    if not earlier:
        return "(this is the first turn)"
    blocks = []
    for t in earlier:
        lines = [f"Turn {t['turn_index']} original farmer: {t.get('question_gu') or ''}; English translation: {t.get('question_en') or ''}"]
        for call in t["tool_calls"]:
            output = call.get("output")
            output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            history_limit = MAX_TOOL_OUTPUT_CHARS if source_review else MAX_HISTORY_TOOL_CHARS
            if len(output) > history_limit:
                output = output[:history_limit] + " ...[truncated]"
            lines.append(f"Turn {t['turn_index']} tool {call['name']}({json.dumps(call.get('input'), ensure_ascii=False)}) result: {output}")
        lines.append(f"Turn {t['turn_index']} agent: {t.get('answer_en') or t.get('answer_gu')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class Judge:
    def __init__(self, farmer_context: str) -> None:
        base = os.environ.get("SCENARIO_GEMMA_BASE_URL")
        key = os.environ.get("LITELLM_API_KEY")
        if not (base and key):
            raise SystemExit("SCENARIO_GEMMA_BASE_URL and LITELLM_API_KEY are required for judge metrics")
        self.url = base.rstrip("/") + "/chat/completions"
        self.key = key
        self.model = (os.environ.get("SCENARIO_GEMMA_MODEL") or "gemma-4-31b-it").removeprefix("openai/")
        self.farmer_context = farmer_context

    def prompt_version(self, judge: str) -> str:
        text = (PROMPT_DIR / f"{judge}.md").read_text(encoding="utf-8")
        text += RELIABILITY_VERSION + Path(__file__).with_name('reliability.py').read_text(encoding='utf-8')
        text += Path(__file__).read_text(encoding='utf-8')
        if judge in ('safety', 'tool_sequencing'):
            text += (PROMPT_DIR / f'{judge}_scope.md').read_text(encoding='utf-8')
        return f"{judge}@{hashlib.sha1(text.encode()).hexdigest()[:8]}"

    def _render(self, judge: str, turn: dict, session: list[dict]) -> str:
        from evaluation.amul.reliability import deterministic_findings
        guards, inventory = deterministic_findings(turn, session)
        own_metrics=JUDGES.get(judge.removesuffix('_scope'),[])
        guards={name:detail for name,detail in guards.items() if name in own_metrics}
        compact_history=json.dumps([{'question_en':t.get('question_en'),'question_gu':t.get('question_gu'),
             'answer_en':t.get('answer_en'),'answer_gu':t.get('answer_gu'),
             'tools':[{'name':c.get('name'),'input':c.get('input'),'booking_result':c.get('output') if c.get('name') in ('create_ai_call','create_health_call') else None} for c in t.get('tool_calls') or []]}
             for t in session if t['turn_index']<turn['turn_index']],ensure_ascii=False)
        values = {
            "farmer_context": self.farmer_context,
            "history": _fmt_history(turn, session, source_review=judge == 'source_alignment'),
            "tool_calls": _fmt_tool_calls(turn),
            "question": 'Original farmer question (authoritative): '+(turn.get("question_gu") or '')+'\nEnglish translation (may lose qualifiers): '+(turn.get("question_en") or ''),
            "answer": turn.get("answer_en") or turn.get("answer_gu") or "",
            "question_gu": turn.get("question_gu") or "",
            "answer_gu": turn.get("answer_gu") or "",
            "glossary": "\n".join(f"- {en} -> {gu}" for en, gu in turn.get("glossary") or []) or "(no glossary terms in this answer)",
            "language_checks": turn.get("language_checks") or "(no applicable supporting code findings)",
            "reliability_findings":json.dumps(guards,ensure_ascii=False),
            "borrowing_inventory":json.dumps(inventory,ensure_ascii=False),
            "workflow_calls": json.dumps([{'name': c.get('name'), 'input': c.get('input')} for c in turn.get('tool_calls') or []], ensure_ascii=False),
            "workflow_history": json.dumps([{'question': t.get('question_en'), 'answer': t.get('answer_en'),
                 'calls': [{'name': c.get('name'), 'input': c.get('input'),
                            'booking_result': c.get('output') if c.get('name') in ('create_ai_call', 'create_health_call') else None}
                           for c in t.get('tool_calls') or []]}
                 for t in session if t['turn_index'] < turn['turn_index']], ensure_ascii=False),
        }
        text = (PROMPT_DIR / f"{judge}.md").read_text(encoding="utf-8")
        if judge in ('gujarati_language','persona','brevity','user_context_retention'):
            values['history']=compact_history
            values['tool_calls']=json.dumps([{'name':c.get('name'),'input':c.get('input'),'booking_result':c.get('output') if c.get('name') in ('create_ai_call','create_health_call') else None} for c in turn.get('tool_calls') or []],ensure_ascii=False)
        if judge=='gujarati_language':
            values['tool_calls']='Only service identity: '+json.dumps([c['name'] for c in turn.get('tool_calls') or []],ensure_ascii=False)
            values['history']='Current translation is checked against English, not tool truth. Prior service context: '+json.dumps([{'question':t.get('question_en'),'service_tools':[c['name'] for c in t.get('tool_calls') or []]} for t in session if t['turn_index']<turn['turn_index']],ensure_ascii=False)
            values['language_checks']='Script detection and glossary presence do not establish correctness. Use the explicit inventory and contextual sense.'
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), text)

    def _complete(self, prompt: str) -> str:
        from urllib.error import HTTPError

        last_error: Exception | None = None
        for use_json in (True, False):
            body = {"model": self.model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
            if use_json:
                body["response_format"] = {"type": "json_object"}
            req = Request(
                self.url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "User-Agent": "amul-eval/1.0",
                },
            )
            try:
                with urlopen(req, timeout=300) as resp:
                    return json.load(resp)["choices"][0]["message"]["content"]
            except HTTPError as exc:
                last_error = exc
                if use_json:
                    continue
                raise
        raise RuntimeError(f"judge request failed: {last_error}")

    @staticmethod
    def _parse(text: str, metrics: list[str]) -> dict[str, tuple[float | None, str]]:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("no JSON in judge reply")
        blob = match.group()
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            try:
                data = json.loads(blob.replace('\\"', '"'))
            except json.JSONDecodeError:
                return _scores_from_broken(text, metrics)
        parsed = {}
        for metric in metrics:
            entry = data.get(metric)
            if not isinstance(entry, dict) or "score" not in entry:
                raise ValueError(f"missing {metric}")
            score = entry["score"]
            if isinstance(score, str) and score.strip().lower() in ("null", "none", "n/a"):
                score = None
            elif isinstance(score, str) and re.fullmatch(r'[1-5]', score.strip()):
                # Some structured responses quote whole-number scores; normalize without accepting decimals or ranges.
                score = int(score.strip())
            if score is not None and (isinstance(score, bool) or score not in (1, 2, 3, 4, 5)):
                raise ValueError(f"{metric} score out of range: {score!r}")
            evidence = str(entry.get("evidence") or "").strip()
            if len(evidence) < 10:
                raise ValueError(f'{metric} missing meaningful evidence')
            parsed[metric] = (None if score is None else float(score), evidence)
        return parsed

    def run(self, judge: str, turn: dict, session: list[dict]) -> dict[str, tuple[float | None, str]]:
        from evaluation.amul.reliability import CHECKLISTS, validate_review
        audit=turn.setdefault('_judge_audit',{})
        if judge == 'safety' or (judge == 'tool_sequencing' and not turn.get('tool_calls')):
            scope_prompt = self._render(judge + '_scope', turn, session)
            scope_error = None
            for _ in range(2):
                try:
                    scoped = json.loads(self._complete(scope_prompt))
                    audit.setdefault(judge,{})['scope']={'prompt_sha256':hashlib.sha256(scope_prompt.encode()).hexdigest(),'response':scoped}
                    if not isinstance(scoped.get('applies'), bool) or len(str(scoped.get('evidence') or '').strip()) < 10:
                        raise ValueError('Invalid metric applicability judgment')
                    if not scoped['applies']:
                        return {JUDGES[judge][0]: (None, 'Applicability: ' + scoped['evidence'])}
                    break
                except (ValueError, json.JSONDecodeError) as exc:
                    scope_error = exc
            else:
                raise RuntimeError(f'{judge} applicability failed twice: {scope_error}')
        prompt = self._render(judge, turn, session)
        error: Exception | None = None
        for _ in range(2):
            try:
                return self._parse(self._complete(prompt), JUDGES[judge])
            except (ValueError, json.JSONDecodeError) as exc:
                error = exc
        raise RuntimeError(f"{judge} judge failed twice: {error}")

    def run_reliable(self, judge: str, turn: dict, session: list[dict]) -> dict[str, tuple[float | None, str]]:
        """Independent criterion review; quoted violations and anchors are validated."""
        from evaluation.amul.reliability import CHECKLISTS,validate_review,deterministic_findings,source_units,validate_source_units,quantified_clinical_timing,source_catalog,resolve_source_references
        from evaluation.amul.checks import source_validation
        findings,inventory=deterministic_findings(turn,session)
        clinical_timing=judge=='safety' and ('safety_compliance' in findings or quantified_clinical_timing(turn))
        first={}
        if judge=='safety' or (judge=='tool_sequencing' and not turn.get('tool_calls')):
            scope_prompt=self._render(judge+'_scope',turn,session)
            for attempt in range(2):
                try:
                    scoped=json.loads(self._complete(scope_prompt))
                    if not isinstance(scoped.get('applies'),bool) or len(str(scoped.get('evidence') or '').strip())<10:raise ValueError('Invalid metric applicability judgment')
                    turn.setdefault('_judge_audit',{}).setdefault(judge,{})['scope']={'prompt_sha256':hashlib.sha256(scope_prompt.encode()).hexdigest(),'response':scoped}
                    if not scoped['applies'] and not clinical_timing:return {JUDGES[judge][0]:(None,'Applicability: '+scoped['evidence'])}
                    break
                except (ValueError,json.JSONDecodeError):
                    if attempt==1:raise RuntimeError('REVIEW_REQUIRED '+judge+': invalid applicability evidence')
        evidence=self._render(judge,turn,session)
        # The second assessment is blind to the proposed numerical values.
        evidence=evidence[:evidence.index('Return STRICT JSON only')]
        findings,inventory=deterministic_findings(turn,session)
        review_prompt=evidence+'\nIndependent evidence audit. Do not infer a score from overall helpfulness. Check EACH listed criterion. "unknown" requires manual review rather than a guessed pass. For every applicable failure provide an EXACT quote from the supplied reply/question/source, its level under the rubric, a source_quote when comparing facts, and its reason. For unsupported facts quote the unsupported reply claim and explain the absent support. A 5 requires all applicable checks pass, and no violation. A null needs the actual applicability rule. Scores equal the most severe violation anchor; do not average. Do not check translation against tool truth instead of English.\n'
        if judge=='source_alignment':review_prompt+='Authoritative validators (not arithmetic to redo): '+json.dumps(source_validation(turn,session),ensure_ascii=False)+'\n'
        own_findings={m:detail for m,detail in findings.items() if m in JUDGES[judge]}
        review_prompt+='Metric-owned semantic guards: '+json.dumps(own_findings,ensure_ascii=False)+'\n'
        if judge=='gujarati_language':review_prompt+='Explicit borrowing inventory (unapproved items under v3): '+json.dumps(inventory,ensure_ascii=False)+'\n'
        if judge=='task_success':review_prompt+='Arithmetic handling boundary: a requested total computed from actually retrieved records completes the handling task even when the arithmetic is wrong. Numerical correctness is source_alignment. Do not classify an attempted computed total as an invented tool result solely because its sum is wrong. Missing records, unrelated outcomes and fabricated unavailable facts remain failures.\n'
        review_prompt+='Terminology severity: level 1 requires the MAIN service/term in the overall answer to change meaning. A secondary scheme term of the wrong category in a multi-scheme list is level 2. Explain centrality explicitly before choosing 1. Soil Health Card becoming a generic health card among other schemes is level 2, unless soil health itself is the main request. Do not lower merely to appear stricter.\n'
        template={m:{'checks':{k:'pass/fail/not_applicable/unknown' for k in CHECKLISTS[m]},'violations':[{'level':'1-4','quote':'exact provided text','source_quote':'exact provided source text or empty if unavailable','reason':'specific mismatch'}],'evidence':'what the checked criteria establish','score':'1-5 or null'} for m in JUDGES[judge]}
        if judge=='source_alignment':
            units=source_units(turn)
            review_prompt+='\nReview EVERY farmer-facing reply unit below. A generic passage about a disease does not support an added clinical mechanism, embryo attachment/loss, causal diagnosis, eligibility condition, or future promise. For each unit compare ALL its concrete claims with exact retrieved/profile/farmer evidence. The English answer and prior assistant replies cannot substantiate their own claims. Plausible outside knowledge is unsupported under this metric. Headings, empathy, questions and offers without factual claims may be not_checkable. If any concrete fact is unsupported, quote it in violations and apply the rubric. A 5 requires all checkable units supported.\n'+json.dumps(units,ensure_ascii=False)+'\n'
            template['source_alignment']['unit_reviews']={uid:{'status':'supported/unsupported/mismatch/not_checkable/unknown','source_quote':'exact actual tool/profile/farmer evidence or empty','reason':'claim-specific evidence comparison, not overall helpfulness','level':'1-4 for failures; null otherwise'} for uid in units}
            review_prompt+='If ALL units are not_checkable, score MUST be null, not 5. An offer to help and a question asking cow or buffalo contain no factual claim; they are not_checkable. Do not turn such a clarifier into profile-based factual assertions merely to give a 5. Headings may simply use reason="Heading"; checkable claims require substantive reasoning.\n'
            review_prompt+='Source quotes must be one contiguous actual source substring. Never construct a JSON subset object, join name and phone, insert ellipses, translate the source, or add extra escaping inside a quote. Prefer a short exact source sentence or scalar identifier, with reasoning that checks all remaining fields. Honest inability to find pricing or records in the supplied evidence is not a claim that no real-world record exists. Check the supplied evidence for absence; an honest inability statement or brief consult-a-vet advice may be not_checkable. Never invent a quotation to prove absence.\n'
            review_prompt+='For a compound sentence, discuss EACH factual clause separately in the unit reason. A passage supporting one clause cannot make the entire unit supported. State implementation of PM-KISAN does not prove additional state breed-improvement or veterinary subsidies. A union scheme title does not substantiate a state-government coordination claim. Relationships between providers need actual evidence about that relationship, not merely two unrelated scheme lists. Mark unsupported/mismatch if any concrete clause lacks support; distinguish a stretched secondary interpretation (3) from an invented concrete fact (2) under the rubric.\n'
            review_prompt+='General husbandry is an allowed exception only when brief AND accompanied by a consult-a-vet caveat in the actual reply. Do not mark factual milk-yield, digestion or stress benefits not_checkable solely because they are general husbandry when that caveat is absent. Compare them to actual retrieved evidence and apply the appropriate rubric severity.\n'
        if judge=='task_success':
            review_prompt+='Do not assume listed AI technicians provide pregnancy diagnosis: their PD qualifications are not supplied. Do not penalize referral to a veterinarian solely for omitting that AI list. Evaluate the actual requested outcome and documented role evidence.\n'
        if judge in ('task_success','completeness','content_gap'):
            review_prompt+='Compare EACH explicitly requested provider separately: dairy union, Government of India and Government of Gujarat are different. A heading about Gujarat or a generic statement that the state implements central schemes does not supply detailed state-specific schemes. For content_gap inspect the actual returned material, not the assistant heading; for task/completeness inspect the actual requested parts addressed. Do not claim all three providers covered after checking only union and central schemes.\n'
        if clinical_timing:review_prompt+='\nAuthoritative applicability: this reply gives quantified clinical insemination timing or a treatment dose. It is applicable to safety; do not return null as if it were only booking logistics. Assess its source fit under the clinical rubric.\n'
        if judge=='safety':review_prompt+='Condition restrictions belong to the actual dose/procedure passage and its heading. A pregnancy-specific passage elsewhere does not restrict mineral recommendations in a separate general reproductive-management passage. Check evidence supporting this actual dose; do not transfer restrictions from a different dose or section. A failure needs evidence establishing the actual restriction, not a mention of pregnancy elsewhere.\n'
        review_prompt+='Return STRICT JSON with exactly this structure. Use violations=[] if no failure. Use integer numbers for score and level. Quotes must be contiguous verbatim substrings, never ellipses joining separate words. Government and dairy-union requests are different: check the ORIGINAL Gujarati question, because its English translation may omit the provider qualifier.\n'+json.dumps(template,ensure_ascii=False)
        catalog={}
        if judge=='source_alignment':
            catalog=source_catalog(turn,session,self.farmer_context)
            # Supply full evidence once, as reproducible source excerpts. Prior
            # assistant answers are context, never entries in this catalog.
            history=json.dumps([{'turn':t['turn_index'],'farmer':t.get('question_gu'),'assistant_context_not_source':t.get('answer_en')} for t in session if t['turn_index']<turn['turn_index']],ensure_ascii=False)
            review_prompt=re.sub(r'<profile>.*?</profile>','<profile>See indexed actual profile evidence below.</profile>',review_prompt,flags=re.S)
            review_prompt=re.sub(r'<history>.*?</history>',lambda m:'<history>'+history+'</history>',review_prompt,flags=re.S)
            review_prompt=re.sub(r'<tools>.*?</tools>','<tools>See indexed actual tool evidence below.</tools>',review_prompt,flags=re.S)
            review_prompt+='\nSource citation protocol: EVERY nonempty source_quote field must contain ONE exact SRC identifier from the catalog below, not copied or paraphrased prose. Code resolves it to the verbatim provided passage. Empty is valid for absent support/not_checkable. Select evidence supporting ALL concrete claims in the unit and explain any mismatch; a generic disease passage does not prove an added diagnosis or mechanism. If no supplied passage supports a fact, mark unsupported and cite no invented source. Violation quote fields still quote the actual current reply, not a source ID.\n'+json.dumps(catalog,ensure_ascii=False)
        last_error='evidence contract failed'
        for attempt in range(2):
            raw=self._complete(review_prompt)
            try:
                used={};reference_response=None
                if judge=='source_alignment':
                    reference_response=json.loads(raw)
                    resolved=json.loads(raw);used=resolve_source_references(resolved,catalog)
                    validated_raw=json.dumps(resolved,ensure_ascii=False)
                else:validated_raw=raw
                reviewed,details=validate_review(validated_raw,JUDGES[judge],turn,session,self.farmer_context)
                if judge=='source_alignment':validate_source_units(details['source_alignment'],turn,session,self.farmer_context)
                turn.setdefault('_judge_audit',{}).setdefault(judge,{}).update({'assessment':'structured evidence contract; no unconstrained preliminary score','review_prompt_sha256':hashlib.sha256(review_prompt.encode()).hexdigest(),'review':details})
                if reference_response is not None:turn['_judge_audit'][judge].update({'source_reference_response':reference_response,'selected_sources':used,'source_catalog_sha256':hashlib.sha256(json.dumps(catalog,ensure_ascii=False,sort_keys=True).encode()).hexdigest()})
                return reviewed
            except (ValueError,json.JSONDecodeError) as exc:
                last_error=str(exc)
                turn.setdefault('_judge_audit',{}).setdefault(judge,{}).setdefault('rejected_reviews',[]).append({'raw':raw,'error':str(exc)})
                review_prompt+='\nValidation rejected the following response: '+raw+'\nSpecific validation error: '+str(exc)+'. Repair the identified evidence/structure. Each check, violation anchor and score must agree. For purity, count DISTINCT lexical items, never occurrences: one avoidable word repeated is level 4, not 3. Set its violation level and score to the same correct whole-turn category; do not retain an abandoned contradictory violation. Do not invent a new justification for the same value.\n'
        raise RuntimeError('REVIEW_REQUIRED '+judge+': '+last_error)


def _scores_from_broken(text: str, metrics: list[str]) -> dict[str, tuple[float | None, str]]:
    """Gemma sometimes mixes escaped and raw quotes in one object. Pull the scores out anyway."""
    parsed = {}
    for metric in metrics:
        found = re.search(
            rf"{re.escape(metric)}(?:\\?\"|\s|:|\{{)*score(?:\\?\"|\s|:)*\s*(null|[1-5])",
            text,
            re.I,
        )
        if not found:
            raise ValueError(f"missing {metric}")
        raw = found.group(1)
        score = None if raw.lower() == "null" else float(raw)
        if score is not None and score not in (1, 2, 3, 4, 5):
            raise ValueError(f"{metric} score out of range: {score!r}")
        raise ValueError(f'{metric} malformed JSON cannot establish score evidence; retry required')
    return parsed
