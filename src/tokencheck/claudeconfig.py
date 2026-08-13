"""Plan, account and counter telemetry that Claude Code caches on disk.

``~/.claude.json`` holds facts no endpoint returns as conveniently: the
subscription tier and rate-limit tier the account is actually on, whether extra
usage is enabled, the date of the first Claude Code token, and running
tool/skill/plugin invocation counters. ``~/.claude/stats-cache.json`` holds
Claude Code's own periodically-computed activity rollup.

Everything here is read-only and best-effort: a missing or malformed file yields
empty results rather than an error, because these are conveniences layered on
top of the live API, never a substitute for it.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .auth import claude_config_dir
from .util import as_float, as_int

# Short glosses so each field explains itself in the output.
PLAN_NOTES: dict[str, str] = {
    "email": "account the credential belongs to",
    "organization_name": "billing organization",
    "organization_type": "subscription product (e.g. claude_max)",
    "rate_limit_tier": "quota tier the limit windows are sized from",
    "organization_role": "your role in the organization",
    "extra_usage_enabled": "pay-as-you-go top-up beyond the subscription",
    "billing_type": "how the subscription is paid",
    "first_token_date": "first Claude Code API call on this account",
    "subscription_started": "when the current subscription began",
    "account_created": "when the Anthropic account was created",
}

COUNTER_NOTES: dict[str, str] = {
    "tools": "built-in tool invocations recorded by Claude Code",
    "skills": "skill loads",
    "plugins": "plugin activations",
}


def config_file() -> Path:
    """``~/.claude.json`` — sits beside the config dir, not inside it."""
    directory = claude_config_dir()
    return directory.parent / f"{directory.name}.json"


def stats_file() -> Path:
    return claude_config_dir() / "stats-cache.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _epoch_ms(value: Any) -> str | None:
    millis = as_float(value)
    if millis is None or millis <= 0:
        return None
    return dt.datetime.fromtimestamp(millis / 1000.0, dt.timezone.utc).isoformat()


def read_plan(path: Path | None = None) -> dict[str, Any]:
    """Account and plan identity, with the fields that shape the rate limits."""
    config = _load(path or config_file())
    account = config.get("oauthAccount")
    account = account if isinstance(account, dict) else {}
    return {
        "email": account.get("emailAddress"),
        "display_name": account.get("displayName"),
        "organization_name": account.get("organizationName"),
        "organization_uuid": account.get("organizationUuid"),
        "organization_type": account.get("organizationType"),
        "organization_role": account.get("organizationRole"),
        "rate_limit_tier": account.get("organizationRateLimitTier") or account.get("userRateLimitTier"),
        "billing_type": account.get("billingType"),
        "extra_usage_enabled": account.get("hasExtraUsageEnabled"),
        "account_created": account.get("accountCreatedAt"),
        "subscription_started": account.get("subscriptionCreatedAt"),
        "first_token_date": config.get("claudeCodeFirstTokenDate"),
        "num_startups": as_int(config.get("numStartups")) or None,
        "install_method": config.get("installMethod"),
        "notes": PLAN_NOTES,
        "source": str(path or config_file()),
    }


def read_cached_utilization(path: Path | None = None) -> dict[str, Any] | None:
    """Claude Code's cached copy of the rate-limit endpoint.

    Useful only as an offline fallback and as a cross-check — it can be hours
    stale, so the age is reported alongside it and `limits` still queries live.
    """
    config = _load(path or config_file())
    cached = config.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None
    utilization = cached.get("utilization")
    if not isinstance(utilization, dict):
        return None

    windows = []
    for key, value in utilization.items():
        if not isinstance(value, dict):
            continue
        percent = as_float(value.get("utilization"))
        if percent is None:
            continue
        windows.append({"key": key, "utilization": percent, "resets_at": value.get("resets_at")})
    windows.sort(key=lambda w: (-w["utilization"], w["key"]))

    fetched_at = _epoch_ms(cached.get("fetchedAtMs"))
    age_seconds = None
    if fetched_at:
        age_seconds = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(fetched_at)).total_seconds()
    return {"fetched_at": fetched_at, "age_seconds": age_seconds, "windows": windows}


def read_counters(path: Path | None = None) -> dict[str, Any]:
    """Lifetime tool / skill / plugin usage counters."""
    config = _load(path or config_file())

    def rows(key: str) -> list[dict[str, Any]]:
        block = config.get(key)
        if not isinstance(block, dict):
            return []
        out = []
        for name, value in block.items():
            if not isinstance(value, dict):
                continue
            out.append(
                {
                    "name": name,
                    "count": as_int(value.get("usageCount")),
                    "last_used": _epoch_ms(value.get("lastUsedAt")),
                }
            )
        out.sort(key=lambda item: (-item["count"], item["name"]))
        return out

    return {
        "tools": rows("toolUsage"),
        "skills": rows("skillUsage"),
        "plugins": rows("pluginUsage"),
        "notes": COUNTER_NOTES,
    }


def read_stats(path: Path | None = None) -> dict[str, Any] | None:
    """Claude Code's own activity rollup — corroboration only, often stale."""
    stats = _load(path or stats_file())
    if not stats:
        return None
    return {
        "last_computed": stats.get("lastComputedDate"),
        "total_sessions": as_int(stats.get("totalSessions")),
        "total_messages": as_int(stats.get("totalMessages")),
        "first_session": stats.get("firstSessionDate"),
        "models": sorted(
            (
                {
                    "name": name,
                    "input_tokens": as_int(value.get("inputTokens")),
                    "output_tokens": as_int(value.get("outputTokens")),
                    "cache_read_input_tokens": as_int(value.get("cacheReadInputTokens")),
                    "cache_creation_input_tokens": as_int(value.get("cacheCreationInputTokens")),
                }
                for name, value in (stats.get("modelUsage") or {}).items()
                if isinstance(value, dict)
            ),
            key=lambda item: -item["output_tokens"],
        ),
        "stale_note": "Claude Code recomputes this occasionally; treat it as corroboration, not a source",
    }
