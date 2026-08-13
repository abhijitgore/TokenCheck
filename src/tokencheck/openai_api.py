"""OpenAI: ChatGPT/Codex subscription limits, and the org Usage & Costs API.

Two independent surfaces, mirroring the Claude split in `api.py`:

* **Codex OAuth** — `GET chatgpt.com/backend-api/wham/usage` with the access
  token Codex CLI stores in `~/.codex/auth.json`. Returns the plan and the
  rolling rate-limit windows. Endpoint, headers and response shape are ported
  from CodexBar (MIT, © 2026 Peter Steinberger).
* **Admin key** — `GET api.openai.com/v1/organization/usage/completions` and
  `/v1/organization/costs`, which report token counts and spend over a window.

As with Claude, nothing here refreshes a token: `~/.codex/auth.json` holds a
refresh token that Codex CLI owns, and redeeming it would rotate the value out
from under Codex's own session.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from .api import APIError, _get_json
from .period import Period
from .util import as_float, as_int

CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ORG_USAGE_URL = "https://api.openai.com/v1/organization/usage/completions"
ORG_COSTS_URL = "https://api.openai.com/v1/organization/costs"

CODEX_REAUTH_HINT = "The Codex credential is expired or revoked — run `codex login`."
#: As on the Anthropic side, a 401 is the only signal an API key has expired or
#: been revoked — the server does not distinguish that from the wrong key kind.
ADMIN_KEY_HINT = (
    "The key was rejected. It is expired, revoked, or not an admin key.\n"
    "  Mint a fresh one at https://platform.openai.com/settings/organization/admin-keys "
    "(it must start with `sk-admin`, not `sk-proj`), then replace the stored copy:\n"
    "    security add-generic-password -a \"$USER\" -s openai_admin_key -w 'sk-admin...' -A -U"
)

#: `/v1/organization/costs` only buckets by day, unlike the usage endpoint.
COSTS_BUCKET_WIDTH = "1d"


# --------------------------------------------------------------------------
# Codex / ChatGPT subscription limits
# --------------------------------------------------------------------------


def fetch_codex_usage(access_token: str, account_id: str | None = None) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "TokenCheck/0.1",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    payload = _get_json(CHATGPT_USAGE_URL, headers, label="codex usage", auth_hint=CODEX_REAUTH_HINT)
    if not isinstance(payload, dict):
        raise APIError("codex usage: unexpected response shape")
    return payload


_WINDOW_LABELS = {
    "primary_window": "primary",
    "secondary_window": "secondary",
}


def parse_codex_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize Codex rate-limit windows into the shape `render_limits` wants."""
    windows: list[dict[str, Any]] = []
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = {}

    for key, name in _WINDOW_LABELS.items():
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        used = as_float(window.get("used_percent"))
        if used is None:
            continue
        windows.append(
            {
                "label": _window_label(name, window.get("limit_window_seconds")),
                "key": key,
                "utilization": used,
                "resets_at": _iso_from_epoch(window.get("reset_at")),
            }
        )

    for entry in payload.get("additional_rate_limits") or []:
        if not isinstance(entry, dict):
            continue
        window = entry.get("rate_limit")
        window = window if isinstance(window, dict) else entry
        used = as_float(window.get("used_percent"))
        if used is None:
            continue
        name = entry.get("limit_name") or entry.get("metered_feature") or "additional"
        windows.append(
            {
                "label": _window_label(str(name), window.get("limit_window_seconds")),
                "key": str(name),
                "utilization": used,
                "resets_at": _iso_from_epoch(window.get("reset_at")),
            }
        )

    return windows


def _window_label(name: str, limit_window_seconds: Any) -> str:
    """"primary (7-day)" — the window length is the only thing that names it."""
    seconds = as_int(limit_window_seconds)
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{name} ({seconds // 86400}-day)"
    if seconds >= 3600:
        return f"{name} ({seconds // 3600}-hour)"
    return name


def _iso_from_epoch(value: Any) -> str | None:
    """Codex reports reset times as unix seconds; the renderer wants ISO-8601."""
    seconds = as_float(value)
    if seconds is None or seconds <= 0:
        return None
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def parse_codex_credits(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Codex credit balance, shaped like Claude's `extra_usage` block."""
    credits = payload.get("credits")
    if not isinstance(credits, dict):
        return None
    if not credits.get("has_credits") and not credits.get("unlimited"):
        return None
    return {
        "used": None,
        "limit": None,
        "utilization": None,
        "currency": "credits",
        "balance": credits.get("balance"),
        "unlimited": bool(credits.get("unlimited")),
    }


def codex_identity(
    payload: dict[str, Any], claims: dict[str, Any], *, credential_source: str
) -> dict[str, Any]:
    """Account identity, preferring the live API over the id_token snapshot.

    The claims are written at token-refresh time and can be months stale — an
    account that changed plan since then would be misreported. What only the
    claims carry is organization membership and role, so the two are merged
    rather than one replacing the other.
    """
    organizations = claims.get("organizations") or []
    default_org = next(
        (org for org in organizations if org.get("title") or org.get("id")), None
    )
    return {
        "provider": "openai",
        "title": "ChatGPT / Codex account",
        "email": payload.get("email") or claims.get("email"),
        "account_uuid": payload.get("account_id") or claims.get("account_id"),
        "user_id": payload.get("user_id") or claims.get("user_id"),
        "organization_name": (default_org or {}).get("title"),
        "organization_uuid": (default_org or {}).get("id"),
        "organization_role": (default_org or {}).get("role"),
        "organizations": organizations,
        "subscription_type": payload.get("plan_type") or claims.get("plan_type"),
        "credential_source": credential_source,
    }


def codex_limits_report(payload: dict[str, Any], *, credential_source: str) -> dict[str, Any]:
    return {
        "provider": "openai",
        "title": "ChatGPT / Codex limits",
        "windows": parse_codex_windows(payload),
        "extra_usage": parse_codex_credits(payload),
        "subscription_type": payload.get("plan_type"),
        "account": payload.get("email"),
        "credential_source": credential_source,
    }


# --------------------------------------------------------------------------
# Organization Usage & Costs (admin key)
# --------------------------------------------------------------------------


def _admin_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "TokenCheck/0.1",
    }


def _paged_get(
    base_url: str, api_key: str, params: list[tuple[str, str]], label: str
) -> list[dict[str, Any]]:
    """Follow OpenAI's `next_page` cursor until the report is exhausted."""
    import urllib.parse

    headers = _admin_headers(api_key)
    buckets: list[dict[str, Any]] = []
    page: str | None = None

    for _ in range(20):
        query = list(params)
        if page:
            query.append(("page", page))
        url = f"{base_url}?{urllib.parse.urlencode(query, doseq=True)}"
        payload = _get_json(url, headers, label=label, auth_hint=ADMIN_KEY_HINT)
        if not isinstance(payload, dict):
            raise APIError(f"{label}: unexpected response shape")
        data = payload.get("data")
        if isinstance(data, list):
            buckets.extend(item for item in data if isinstance(item, dict))
        if not payload.get("has_more"):
            break
        page = payload.get("next_page")
        if not page:
            break

    return buckets


def fetch_org_usage(
    api_key: str, *, period: Period, now: dt.datetime | None = None
) -> dict[str, Any]:
    """Token counts and spend for the org over `period`."""
    now = now or dt.datetime.now(dt.timezone.utc)
    start, end = period.range(now)

    usage_buckets = _paged_get(
        ORG_USAGE_URL,
        api_key,
        [
            ("start_time", str(int(start.timestamp()))),
            ("end_time", str(int(end.timestamp()))),
            ("bucket_width", period.bucket_width),
            ("limit", str(period.bucket_limit())),
            ("group_by", "model"),
        ],
        "organization/usage",
    )

    # Costs are daily-only, so a sub-day period still gets whole-day buckets;
    # the report labels that difference rather than pretending otherwise.
    cost_start = start if not period.is_hourly else start.replace(hour=0, minute=0, second=0, microsecond=0)
    cost_buckets = _paged_get(
        ORG_COSTS_URL,
        api_key,
        [
            ("start_time", str(int(cost_start.timestamp()))),
            ("end_time", str(int(end.timestamp()))),
            ("bucket_width", COSTS_BUCKET_WIDTH),
            ("limit", str(max(1, period.days))),
            ("group_by", "line_item"),
        ],
        "organization/costs",
    )

    return merge_org_buckets(usage_buckets, cost_buckets, period=period, now=now)


def merge_org_buckets(
    usage_buckets: list[dict[str, Any]],
    cost_buckets: list[dict[str, Any]],
    *,
    period: Period,
    now: dt.datetime,
) -> dict[str, Any]:
    """Fold OpenAI's bucket lists into the same report shape the Claude side emits."""
    models: dict[str, dict[str, int]] = defaultdict(_zero_model)
    cost_items: dict[str, float] = defaultdict(float)
    per_bucket: list[dict[str, Any]] = []
    totals = _zero_model()
    total_cost = 0.0
    requests = 0

    for bucket in usage_buckets:
        start = as_int(bucket.get("start_time"))
        entry = {
            "starting_at": _iso_from_epoch(start),
            "ending_at": _iso_from_epoch(bucket.get("end_time")),
            "day": (_iso_from_epoch(start) or "")[:10],
            **_zero_model(),
            "cost_usd": 0.0,
        }
        for result in bucket.get("results") or []:
            if not isinstance(result, dict):
                continue
            counts = {
                "input_tokens": as_int(result.get("input_tokens")),
                "cache_read_input_tokens": as_int(result.get("input_cached_tokens")),
                "output_tokens": as_int(result.get("output_tokens")),
                # OpenAI reports no separate cache-write counter.
                "cache_creation_input_tokens": 0,
            }
            counts["total_tokens"] = sum(counts.values())
            requests += as_int(result.get("num_model_requests"))
            name = result.get("model") or "unknown"
            for field, value in counts.items():
                entry[field] += value
                totals[field] += value
                models[name][field] += value
        per_bucket.append(entry)

    by_day: dict[str, float] = {}
    for bucket in cost_buckets:
        day = (_iso_from_epoch(bucket.get("start_time")) or "")[:10]
        for result in bucket.get("results") or []:
            if not isinstance(result, dict):
                continue
            # OpenAI returns cost as {"value": float, "currency": "usd"} in whole
            # dollars — unlike Anthropic, which uses the currency's lowest unit.
            amount = as_float((result.get("amount") or {}).get("value")) or 0.0
            name = result.get("line_item") or "OpenAI API"
            cost_items[name] += amount
            total_cost += amount
            by_day[day] = by_day.get(day, 0.0) + amount

    for entry in per_bucket:
        if entry["day"] in by_day and period.bucket_width == COSTS_BUCKET_WIDTH:
            entry["cost_usd"] = by_day[entry["day"]]

    per_bucket.sort(key=lambda item: item["starting_at"] or "")
    totals["cost_usd"] = total_cost

    return {
        "provider": "openai",
        "title": "OpenAI org usage",
        "period": period.label,
        "period_description": period.describe(),
        "cost_is_daily_only": period.is_hourly,
        "requests": requests,
        "range": {
            "starting_at": per_bucket[0]["starting_at"] if per_bucket else None,
            "ending_at": per_bucket[-1]["ending_at"] if per_bucket else None,
        },
        "totals": totals,
        "models": [
            {"name": name, **values}
            for name, values in sorted(models.items(), key=lambda kv: (-kv[1]["total_tokens"], kv[0]))
        ],
        "cost_items": [
            {"name": name, "cost_usd": value}
            for name, value in sorted(cost_items.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "daily": per_bucket,
        "fetched_at": now.isoformat(),
    }


def _zero_model() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
