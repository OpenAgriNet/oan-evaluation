"""
Configurable rule evaluation and handler dispatch for beckn_search_proxy.

Add new behaviour:
  1. Implement a handler function and register it in HANDLERS.
  2. Add a condition type in eval_condition if not expressible with path + and/or.
  3. Append a rule to config.json (when / then).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

# vistaar_mandi_mock lives next to this package directory (mock_server/…)
_MOCK_SERVER_ROOT = Path(__file__).resolve().parent.parent
_MANDI_PKG = _MOCK_SERVER_ROOT / "vistaar_mandi_mock"
if str(_MANDI_PKG) not in sys.path:
    sys.path.insert(0, str(_MANDI_PKG))

from mandi_response import (  # noqa: E402
    PROXY_FALLTHROUGH,
    city_tables,
    load_registry,
    nearest_city,
    parse_mandi_stop,
    try_build_mandi_mock,
)

# New handlers live beside this file in the beckn_search_proxy package
_PROXY_PKG = Path(__file__).resolve().parent
if str(_PROXY_PKG) not in sys.path:
    sys.path.insert(0, str(_PROXY_PKG))

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def substitute_env(obj: Any) -> Any:
    """Replace ${VAR} or ${VAR:default} in strings using os.environ."""

    def sub_str(s: str) -> str:
        def repl(m: re.Match[str]) -> str:
            key, default = m.group(1), m.group(2)
            v = os.environ.get(key)
            if v is not None and v != "":
                return v
            return default if default is not None else ""

        return _ENV_PATTERN.sub(repl, s)

    if isinstance(obj, str):
        return sub_str(obj)
    if isinstance(obj, list):
        return [substitute_env(x) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute_env(v) for k, v in obj.items() if not str(k).startswith("_")}
    return obj


def resolve_path(config_dir: Path, p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return (config_dir / path).resolve()


def get_path(obj: Any, dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if isinstance(cur, list) and part.isdigit():  # e.g. message.order.items.0.id
            cur = cur[int(part)] if int(part) < len(cur) else None
            continue
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def eval_mandi_nearest_city_in(
    body: dict,
    spec: dict[str, Any],
    config_dir: Path,
) -> bool:
    reg_path = resolve_path(config_dir, spec["registry"])
    allowed = frozenset(spec.get("city_keys") or [])
    if not allowed:
        return False
    try:
        lat, lon, _ = parse_mandi_stop(body)
    except (KeyError, TypeError, ValueError):
        return False
    reg = load_registry(reg_path)
    cities, _tol = city_tables(reg)
    # Match any mandi price-discovery request that parses. Gating on a distance
    # tolerance and a city allowlist sent every out-of-window location to the
    # live BPP (401/404 in eval); mock_catalog now serves the nearest city for
    # any coordinate, so the rule must not reject first.
    return nearest_city(lat, lon, cities, float("inf")) is not None


def eval_phone_in_registry(body: dict, spec: dict[str, Any], config_dir: Path) -> bool:
    """True if the phone in message.order.fulfillments[0].customer.contact.phone matches a farmer key."""
    import json as _json

    reg_path = resolve_path(config_dir, spec["registry"])
    try:
        with open(reg_path, "r", encoding="utf-8") as f:
            reg = _json.load(f)
    except Exception:
        return False

    farmers = reg.get("farmers", {})
    if not farmers:
        return False

    try:
        phone_raw = (
            body["message"]["order"]["fulfillments"][0]["customer"]["contact"]["phone"]
        )
    except (KeyError, IndexError, TypeError):
        return False

    digits = "".join(c for c in phone_raw if c.isdigit())
    phone_key = digits[-10:] if len(digits) >= 10 else digits
    # Any well-formed phone is served. Requiring a registry hit sent every other
    # number to the live BPP (401/404 in eval); the handlers synthesise a
    # deterministic farmer for unknown numbers instead.
    return len(phone_key) == 10 or phone_key in farmers


def eval_condition(node: dict[str, Any], body: dict, config_dir: Path) -> bool:
    if "and" in node:
        return all(eval_condition(c, body, config_dir) for c in node["and"])
    if "or" in node:
        return any(eval_condition(c, body, config_dir) for c in node["or"])
    if "not" in node:
        return not eval_condition(node["not"], body, config_dir)
    if "type" in node:
        t = node["type"]
        if t == "mandi_nearest_city_in":
            return eval_mandi_nearest_city_in(body, node, config_dir)
        if t == "phone_in_registry":
            return eval_phone_in_registry(body, node, config_dir)
        if t == "path_present":
            # True when the dotted path resolves to something non-empty. Used to
            # tell the two PMFBY /status flows apart: the scheme-status check
            # carries customer.person.tags (season/year/inquiry_type) while the
            # grievance OTP verify sends phone-only fulfillments.
            return bool(get_path(body, node["path"]))
        raise ValueError(f"unknown condition type: {t}")
    if "path" in node and "equals" in node:
        return get_path(body, node["path"]) == node["equals"]
    raise ValueError(f"invalid condition node keys: {list(node.keys())}")


def handle_mock_mandi(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    reg = resolve_path(config_dir, then_cfg["registry"])
    keys = frozenset(then_cfg.get("city_keys") or [])
    bpp_fb = os.environ.get("BPP_URI") or os.environ.get("DEFAULT_BPP_URI")
    out = try_build_mandi_mock(
        body,
        reg,
        allowed_city_keys=keys,
        bpp_uri_fallback=bpp_fb,
    )
    if out is PROXY_FALLTHROUGH:
        return None
    return out


def handle_mock_scheme_info(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_scheme_info import build_scheme_info_response  # noqa: PLC0415
    reg = resolve_path(config_dir, then_cfg["registry"])
    return build_scheme_info_response(body, reg)


def handle_forward_network(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from forward_network import forward  # noqa: PLC0415
    return forward(body, then_cfg["env_file"])


def handle_mock_shc(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_shc import build_shc_response  # noqa: PLC0415
    reg = resolve_path(config_dir, then_cfg["registry"])
    return build_shc_response(body, reg)


def handle_mock_pmkisan_init(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_pmkisan import build_pmkisan_init_response  # noqa: PLC0415
    reg = resolve_path(config_dir, then_cfg["registry"])
    return build_pmkisan_init_response(body, reg)


def handle_mock_pmkisan_status(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_pmkisan import build_pmkisan_status_response  # noqa: PLC0415
    reg = resolve_path(config_dir, then_cfg["registry"])
    return build_pmkisan_status_response(body, reg)


def handle_mock_pmfby_init(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_pmfby import build_pmfby_init_response  # noqa: PLC0415
    reg = resolve_path(config_dir, then_cfg["registry"])
    return build_pmfby_init_response(body, reg)


def handle_mock_pmfby_status(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_pmfby import build_pmfby_status_response  # noqa: PLC0415
    reg = resolve_path(config_dir, then_cfg["registry"])
    return build_pmfby_status_response(body, reg)


def handle_mock_pmfby_grievance(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_pmfby import build_pmfby_grievance_response  # noqa: PLC0415
    reg = resolve_path(config_dir, then_cfg["registry"])
    return build_pmfby_grievance_response(body, reg)


def handle_mock_sathi(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_sathi import build_sathi_response  # noqa: PLC0415
    reg = resolve_path(config_dir, then_cfg.get("registry", "../common_registry.json"))
    return build_sathi_response(body, reg)


def handle_mock_sathi_search(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_sathi_search import build_sathi_search_response  # noqa: PLC0415
    return build_sathi_search_response(body)


def handle_mock_pmkisan_grievance_submit(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_pmkisan_grievance import build_pmkisan_grievance_submit_response
    return build_pmkisan_grievance_submit_response(body, resolve_path(config_dir, then_cfg["registry"]))


def handle_mock_pmkisan_grievance_status(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_pmkisan_grievance import build_pmkisan_grievance_status_response
    return build_pmkisan_grievance_status_response(body, resolve_path(config_dir, then_cfg["registry"]))


def handle_mock_pmfby_grievance_status(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_pmfby import build_pmfby_grievance_status_response
    return build_pmfby_grievance_status_response(body, resolve_path(config_dir, then_cfg["registry"]))


def handle_current_status(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict:
    from mock_current_status import build_response
    return build_response(body, then_cfg, config_dir)


def handle_current_search(body: dict, then_cfg: dict[str, Any], config_dir: Path) -> dict | None:
    from mock_current_search import build_response
    return build_response(body, then_cfg, config_dir)


HANDLERS: dict[str, Any] = {
    "mock_current_status": handle_current_status,
    "mock_current_search": handle_current_search,
    "mock_mandi": handle_mock_mandi,
    "mock_scheme_info": handle_mock_scheme_info,
    "forward_network": handle_forward_network,
    "mock_shc": handle_mock_shc,
    "mock_pmkisan_init": handle_mock_pmkisan_init,
    "mock_pmkisan_status": handle_mock_pmkisan_status,
    "mock_pmfby_init": handle_mock_pmfby_init,
    "mock_pmfby_status": handle_mock_pmfby_status,
    "mock_pmkisan_grievance_submit": handle_mock_pmkisan_grievance_submit,
    "mock_pmkisan_grievance_status": handle_mock_pmkisan_grievance_status,
    "mock_pmfby_grievance_status": handle_mock_pmfby_grievance_status,
    "mock_pmfby_grievance": handle_mock_pmfby_grievance,
    "mock_sathi": handle_mock_sathi,
    "mock_sathi_search": handle_mock_sathi_search,
}


def apply_rules(
    body: dict,
    rules: list[dict[str, Any]],
    config_dir: Path,
) -> dict | None:
    """
    Return a JSON dict to respond with, or None to forward to upstream.
    First matching rule whose handler returns non-None wins.
    """
    for rule in rules:
        rule_id = rule.get("id", "?")
        when = rule.get("when")
        then = rule.get("then")
        if not when or not then:
            continue
        try:
            if not eval_condition(when, body, config_dir):
                continue
        except Exception as e:
            raise RuntimeError(f"rule {rule_id!r} when-clause error: {e}") from e

        handler_name = then.get("handler")
        if not handler_name:
            raise RuntimeError(f"rule {rule_id!r} missing then.handler")
        fn = HANDLERS.get(handler_name)
        if not fn:
            raise RuntimeError(f"rule {rule_id!r} unknown handler {handler_name!r}")

        try:
            result = fn(body, then, config_dir)
        except Exception as e:
            raise RuntimeError(f"rule {rule_id!r} handler {handler_name!r} error: {e}") from e

        if result is not None:
            return result

    return None
