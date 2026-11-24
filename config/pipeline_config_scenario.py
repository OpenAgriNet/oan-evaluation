from __future__ import annotations

import os

from config.pipeline_config import DataTransformConfig, ModelEvaluationConfig, PipelineConfig

# ── vLLM endpoint for the "vllm" judge profile ───────────────────────
# No hardcoded default on purpose: this box's vLLM containers include shared/
# production instances (e.g. gemma4-text) that must never be hit without the
# user explicitly confirming that specific use, right then — see oan-brain's
# knowledge/environments.md and feedback_vllm_docker_care.md. Set both env
# vars yourself, each time, pointing at whatever endpoint you've just
# confirmed is OK to use.
VLLM_BASE_URL = os.environ["SCENARIO_JUDGE_VLLM_BASE_URL"]
VLLM_MODEL = os.environ["SCENARIO_JUDGE_VLLM_MODEL"]


# ── Judge LLM profiles ──────────────────────────────────────────────
LLM_PROFILES = {
    "gpt-5.4-mini": {
        "model": "gpt-5.1",
        "retries": 3,
        "model_settings": {
            #"temperature": 0.0,
             "timeout": 60},
        "input_token_cost_per_1m": 1.25,
        "output_token_cost_per_1m": 10,
    },
    "gpt-4.1-mini": {
        "model": "gpt-5-mini",
        "retries": 3,
        "model_settings": {"temperature": 0.0, "timeout": 120},
        "input_token_cost_per_1m": 0.25,
        "output_token_cost_per_1m": 2.5,
    },
    "vllm": {
        "model": VLLM_MODEL,
        "base_url": VLLM_BASE_URL,
        "api_key": "dummy",
        "retries": 3,
        "model_settings": {"temperature": 0.0, "timeout": 120},
        "input_token_cost_per_1m": 0,
        "output_token_cost_per_1m": 0,
    },
       "vllm_qwen": {
        # Not referenced by any active metric below — env vars are optional
        # (no default) rather than required, so importing this config doesn't
        # force you to configure a judge profile nothing currently uses.
        "model": os.environ.get("SCENARIO_JUDGE_QWEN_MODEL", ""),
        "base_url": os.environ.get("SCENARIO_JUDGE_QWEN_BASE_URL", ""),
        "api_key": "dummy",
        "retries": 3,
        "model_settings": {"temperature": 0.0, "timeout": 120},
        "input_token_cost_per_1m": 0,
        "output_token_cost_per_1m": 0,
    },
}

# ── Pipeline config for scenario-based (any model) evaluation ────────
# Not tied to one model — model_name is set below only as the default;
# override per run with `--model-name <name>` (see inference/README.md).
# Input:  data/models/<model_name>/data.json
# Output: data/models/<model_name>/evaluation.json
#
# bv_specific prompts are picked up automatically from
# assets/prompts/metrics/bv_specific/<metric_prompt_file> when present.
# `category` from each row flows into those templates via build_context.
#
# Set num_samples to an integer (e.g. 20) for a quick smoke-test run.
CONFIG = PipelineConfig(
    model_name="default",  # always override with --model-name <name> in practice
    num_samples=None,
    max_concurrent=50,
    checkpoint_interval_minutes=2,
    fail_open=True,
    model_evaluation=ModelEvaluationConfig(
        llm=LLM_PROFILES,
        default_llm_profile="vllm",
        metric_classes=[
            # ── Response Usefulness ──────────────────────────────────
            ("metrics.llm_metrics.AccuracyCompletenessMetric",       "vllm"),
            #("metrics.llm_metrics.ActionabilityMetric",              "vllm"),
            #("metrics.llm_metrics.ConversationClosureMetric",        "vllm"),
            ("metrics.llm_metrics.SourceDataComprehensivenessMetric","vllm"),
            # ── Factual Grounding ────────────────────────────────────
            ("metrics.llm_metrics.NoFabricationMetric",              "vllm"),
            ("metrics.llm_metrics.CitationAccuracyMetric",           "vllm"),
            #("metrics.llm_metrics.CitationComprehensivenessMetric",  "vllm"),
            #("metrics.llm_metrics.SafetyComplianceMetric",           "vllm"),
            # ── Linguistic Quality ───────────────────────────────────
            ("metrics.llm_metrics.TranslationAccuracyMetric",        "vllm"),
            #("metrics.llm_metrics.GrammarFluencyMetric",             "vllm"),
            #("metrics.llm_metrics.LanguagePurityMetric",             "vllm"),
            #("metrics.llm_metrics.BrevityMetric",                    "vllm"),
            #("metrics.llm_metrics.VoiceReadyTextMetric",             "vllm"),
            # ── Tool/Workflow Quality ────────────────────────────────
            ("metrics.llm_metrics.ToolCallQualityMetric",            "vllm"),
            #"metrics.llm_metrics.AgristackWorkflowMetric",
            #"metrics.llm_metrics.ToolSequencingMetric",
            #"metrics.llm_metrics.SearchQualityMetric",
            #"metrics.llm_metrics.OutputHygieneMetric",
            # ── Performance (no LLM cost) ────────────────────────────
            #"metrics.llm_metrics.ElapsedSecondsMetric",
            #"metrics.llm_metrics.WordCountMetric",
            #"metrics.llm_metrics.ErrorMetric",
        ],
    ),
    data_transform=DataTransformConfig(
        selected_output_keys=[
            "question",
            "answer",
            "category",
            "agristack_required",
            "output_language",
            "agent_turns",
            "tool_calls_raw",
            "retrieved_sources",
            "TTFT_answer",
            "latency_answer",
        ],
    ),
)
