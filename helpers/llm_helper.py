from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


async def llm_call(
    *,
    prompt: str,
    llm: Dict[str, Any],
    system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Common LLM helper used by metric plugins.

    The `llm` dict is passed through as Agent kwargs.
    If the profile contains `base_url`, an OpenAIChatModel is constructed
    pointing at that endpoint (vLLM / any OpenAI-compatible server).
    """
    if not isinstance(llm, dict):
        raise ValueError("llm config must be a dict of Agent kwargs")

    agent_kwargs = dict(llm)
    agent_kwargs.pop("input_token_cost_per_1m", None)
    agent_kwargs.pop("output_token_cost_per_1m", None)
    agent_kwargs.setdefault("output_type", str)

    base_url = agent_kwargs.pop("base_url", None)
    if base_url is not None:
        model_name = agent_kwargs.pop("model")
        api_key = agent_kwargs.pop("api_key", "dummy")
        provider = OpenAIProvider(base_url=base_url, api_key=api_key)
        agent_kwargs["model"] = OpenAIChatModel(model_name, provider=provider)

    if system_prompt is not None and "system_prompt" not in agent_kwargs:
        agent_kwargs["system_prompt"] = system_prompt

    agent = Agent(**agent_kwargs)
    result = await agent.run(prompt)
    usage = result.usage() if callable(result.usage) else result.usage
    return {
        "text": result.output,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "details": dict(usage.details),
        },
    }
