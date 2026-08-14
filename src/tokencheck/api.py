"""Anthropic observability endpoints, over stdlib urllib.

Endpoints, headers and response shapes here are ported from CodexBar
(https://github.com/steipete/CodexBar, MIT, © 2026 Peter Steinberger):

* ``GET https://api.anthropic.com/api/oauth/usage``   — subscription rate limits
* ``GET https://api.anthropic.com/api/oauth/profile`` — account identity
* ``GET https://api.anthropic.com/v1/organizations/usage_report/messages``
* ``GET https://api.anthropic.com/v1/organizations/cost_report``
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

from .util import as_float as _as_float
from .util import as_int as _as_int
from .util import parse_iso as _parse_iso

API_BASE = "https://api.anthropic.com"
OAUTH_USAGE_PATH = "/api/oauth/usage"
OAUTH_PROFILE_PATH = "/api/oauth/profile"
COST_REPORT_URL = f"{API_BASE}/v1/organizations/cost_report"
MESSAGES_USAGE_URL = f"{API_BASE}/v1/organizations/usage_report/messages"

OAUTH_BETA_HEADER = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"
FALLBACK_CLAUDE_VERSION = "2.1.0"
MAX_DAILY_BUCKETS = 31

_TIMEOUT = 30


class APIError(Exception):
    """An Anthropic API call failed."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _claude_code_user_agent() -> str:
    """The OAuth usage endpoint expects a Claude Code user agent."""
    version = None
    binary = shutil.which("claude")
    if binary:
        try:
            result = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                token = result.stdout.strip().split()
                version = token[0] if token else None
            # `claude --version` prints e.g. "2.1.229 (Claude Code)".
        except (OSError, subprocess.SubprocessError):
            version = None
    return f"claude-code/{version or FALLBACK_CLAUDE_VERSION}"


OAUTH_REAUTH_HINT = "The credential is expired or revoked — run `claude` to re-authenticate."

#: An API key carries no local expiry, so a 401 is the only signal that it has
#: expired or been revoked. The message has to cover that as well as the
#: wrong-kind-of-key case, since the server reports both identically.
ADMIN_KEY_HINT = (
    "The key was rejected. It is expired, revoked, or not an Admin API key.\n"
    "  Note: Anthropic's Admin API is unavailable for individual accounts — an\n"
    "  Admin key (`sk-ant-admin…`) can only be provisioned by an admin of a\n"
    "  Console *organization* (Console -> Settings -> Organization).\n"
    "  This report also covers API usage only. Claude Pro/Max subscription usage\n"
    "  is not billed per token and never appears here — use `tokencheck limits`\n"
    "  and `tokencheck local` for that."
)


def _get_json(
    url: str,
    headers: dict[str, str],
    *,
    label: str,
    auth_hint: str = OAUTH_REAUTH_HINT,
) -> Any:
    """Fetch and decode JSON."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace").strip().replace("\n", " ")[:400]
        except Exception:  # noqa: BLE001 - error body is best-effort context only
            detail = ""
        raise _http_error(label, error.code, detail, error.headers, auth_hint) from error
    except urllib.error.URLError as error:
        raise APIError(f"{label}: network error — {error.reason}") from error

    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise APIError(f"{label}: response was not valid JSON") from error


def _http_error(label: str, status: int, detail: str, headers: Any, auth_hint: str) -> APIError:
    retry_after: float | None = None
    if status == 401:
        return APIError(f"{label}: unauthorized (HTTP 401). {auth_hint}", status=status)
    if status == 403:
        # The server's own message is the most specific thing available, so it
        # leads; the generic guidance follows it.
        server_says = f"\n  {detail}" if detail else ""
        return APIError(
            f"{label}: forbidden (HTTP 403). This credential lacks access to that endpoint."
            f"{server_says}\n  {auth_hint}",
            status=status,
        )
    if status == 429:
        raw = headers.get("Retry-After") if headers else None
        try:
            retry_after = float(raw) if raw else None
        except (TypeError, ValueError):
            retry_after = None
        hint = f" Retry in ~{int(retry_after)}s." if retry_after else ""
        return APIError(
            f"{label}: rate limited by Anthropic (HTTP 429).{hint} Wait a few minutes and retry.",
            status=status,
            retry_after=retry_after,
        )
    suffix = f" — {detail}" if detail else ""
    return APIError(f"{label}: HTTP {status}{suffix}", status=status)


# --------------------------------------------------------------------------
# OAuth (subscription rate limits)
# --------------------------------------------------------------------------


def fetch_oauth_usage(access_token: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": OAUTH_BETA_HEADER,
        "User-Agent": _claude_code_user_agent(),
    }
    payload = _get_json(API_BASE + OAUTH_USAGE_PATH, headers, label="oauth usage")
    if not isinstance(payload, dict):
        raise APIError("oauth usage: unexpected response shape")
    return payload


def fetch_oauth_profile(access_token: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _claude_code_user_agent(),
    }
    payload = _get_json(API_BASE + OAUTH_PROFILE_PATH, headers, label="oauth profile")
    if not isinstance(payload, dict):
        raise APIError("oauth profile: unexpected response shape")
    return payload


# Flat window keys, in the order they should be displayed. The API has been
# migrating these into a `limits[]` array; both shapes are parsed below.
_WINDOW_LABELS: list[tuple[str, str]] = [
    ("five_hour", "5-hour session"),
    ("seven_day", "7-day (all models)"),
    ("seven_day_opus", "7-day Opus"),
    ("seven_day_sonnet", "7-day Sonnet"),
    ("seven_day_oauth_apps", "7-day OAuth apps"),
    ("seven_day_routines", "7-day routines"),
    ("seven_day_claude_routines", "7-day routines"),
    ("claude_routines", "7-day routines"),
    ("seven_day_cowork", "7-day cowork"),
]


def parse_limit_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize both response shapes into one list of limit windows."""
    windows: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    for key, label in _WINDOW_LABELS:
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        utilization = _as_float(window.get("utilization"))
        if utilization is None:
            continue
        if label in seen_labels:
            continue
        seen_labels.add(label)
        windows.append(
            {
                "label": label,
                "key": key,
                "utilization": utilization,
                "resets_at": window.get("resets_at"),
            }
        )

    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("is_active") is False:
            continue
        utilization = _as_float(entry.get("percent"))
        if utilization is None:
            continue
        model = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        group = entry.get("group") or entry.get("kind") or "limit"
        label = f"{group} {model}".strip() if model else str(group)
        if label in seen_labels:
            continue
        seen_labels.add(label)
        windows.append(
            {
                "label": label,
                "key": entry.get("kind") or group,
                "utilization": utilization,
                "resets_at": entry.get("resets_at"),
            }
        )

    return windows


def parse_extra_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pay-as-you-go credits on top of the subscription, when enabled."""
    extra = payload.get("extra_usage")
    if not isinstance(extra, dict) or not extra.get("is_enabled"):
        return None
    return {
        "used": _as_float(extra.get("used_credits")),
        "limit": _as_float(extra.get("monthly_limit")),
        "utilization": _as_float(extra.get("utilization")),
        "currency": extra.get("currency") or "USD",
    }


# --------------------------------------------------------------------------
# Admin API (org-wide tokens and cost)
# --------------------------------------------------------------------------


def _admin_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Accept": "application/json",
        "User-Agent": "TokenCheck/0.1",
    }


def _rfc3339(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _daily_range(days: int, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    today = now.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - dt.timedelta(days=days - 1), today + dt.timedelta(days=1)


def _paged_admin_get(base_url: str, api_key: str, params: list[tuple[str, str]], label: str) -> list[dict[str, Any]]:
    """Follow `next_page` cursors until the report is exhausted."""
    headers = _admin_headers(api_key)
    buckets: list[dict[str, Any]] = []
    page: str | None = None

    for _ in range(20):  # generous cap; a 31-day daily report fits in one page
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


def fetch_admin_usage(
    api_key: str,
    *,
    period: "Period | None" = None,
    days: int | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Fetch the org token report and cost report over `period`, merged into buckets.

    `days` remains accepted so older call sites keep working; `period` wins when
    both are given.
    """
    from .period import from_args

    window = period or from_args(None, days)
    now = now or dt.datetime.now(dt.timezone.utc)
    start, end = window.range(now)

    message_buckets = _paged_admin_get(
        MESSAGES_USAGE_URL,
        api_key,
        [
            ("starting_at", _rfc3339(start)),
            ("ending_at", _rfc3339(end)),
            ("bucket_width", window.bucket_width),
            ("limit", str(window.bucket_limit())),
            ("group_by[]", "model"),
        ],
        "usage_report/messages",
    )

    # `cost_report` only buckets by day. For a sub-day period that means the
    # cost figure covers the calendar day so far, not the exact window — the
    # report flags that rather than quietly mismatching the token counts.
    cost_start = start.replace(hour=0, minute=0, second=0, microsecond=0) if window.is_hourly else start
    cost_buckets = _paged_admin_get(
        COST_REPORT_URL,
        api_key,
        [
            ("starting_at", _rfc3339(cost_start)),
            ("ending_at", _rfc3339(end)),
            ("bucket_width", "1d"),
            ("limit", str(window.days)),
            ("group_by[]", "description"),
        ],
        "cost_report",
    )

    report = _merge_admin_buckets(message_buckets, cost_buckets, now=now)
    report["period"] = window.label
    report["period_description"] = window.describe()
    report["cost_is_daily_only"] = window.is_hourly
    return report


def _merge_admin_buckets(
    message_buckets: list[dict[str, Any]],
    cost_buckets: list[dict[str, Any]],
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    daily: dict[str, dict[str, Any]] = {}

    def bucket_for(item: dict[str, Any]) -> dict[str, Any] | None:
        starting_at = item.get("starting_at")
        if not isinstance(starting_at, str):
            return None
        entry = daily.get(starting_at)
        if entry is None:
            entry = {
                "day": starting_at[:10],
                "starting_at": starting_at,
                "ending_at": item.get("ending_at"),
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "models": defaultdict(_zero_model),
                "cost_items": defaultdict(float),
            }
            daily[starting_at] = entry
        return entry

    for item in message_buckets:
        entry = bucket_for(item)
        if entry is None:
            continue
        for result in item.get("results") or []:
            if not isinstance(result, dict):
                continue
            cache_creation = result.get("cache_creation") or {}
            counts = {
                "input_tokens": _as_int(result.get("uncached_input_tokens")),
                "cache_creation_input_tokens": (
                    _as_int(cache_creation.get("ephemeral_1h_input_tokens"))
                    + _as_int(cache_creation.get("ephemeral_5m_input_tokens"))
                ),
                "cache_read_input_tokens": _as_int(result.get("cache_read_input_tokens")),
                "output_tokens": _as_int(result.get("output_tokens")),
            }
            counts["total_tokens"] = sum(counts.values())
            model = result.get("model") or "unknown"
            for field, value in counts.items():
                entry[field] += value
                entry["models"][model][field] += value

    for item in cost_buckets:
        entry = bucket_for(item)
        if entry is None:
            continue
        for result in item.get("results") or []:
            if not isinstance(result, dict):
                continue
            # The Usage & Cost API returns `amount` as a decimal string in the
            # currency's lowest unit (cents for USD).
            amount = (_as_float(result.get("amount")) or 0.0) / 100.0
            name = result.get("description") or result.get("cost_type") or "Claude API"
            entry["cost_usd"] += amount
            entry["cost_items"][name] += amount

    horizon = now.astimezone(dt.timezone.utc)
    days_out = []
    for key in sorted(daily):
        entry = daily[key]
        started = _parse_iso(entry["starting_at"])
        if started is not None and started > horizon:
            continue
        entry["models"] = [
            {"name": name, **values}
            for name, values in sorted(
                entry["models"].items(), key=lambda kv: (-kv[1]["total_tokens"], kv[0])
            )
        ]
        entry["cost_items"] = [
            {"name": name, "cost_usd": value}
            for name, value in sorted(entry["cost_items"].items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        days_out.append(entry)

    totals = {
        "input_tokens": sum(d["input_tokens"] for d in days_out),
        "cache_creation_input_tokens": sum(d["cache_creation_input_tokens"] for d in days_out),
        "cache_read_input_tokens": sum(d["cache_read_input_tokens"] for d in days_out),
        "output_tokens": sum(d["output_tokens"] for d in days_out),
        "total_tokens": sum(d["total_tokens"] for d in days_out),
        "cost_usd": sum(d["cost_usd"] for d in days_out),
    }

    models: dict[str, dict[str, int]] = defaultdict(_zero_model)
    for day in days_out:
        for model in day["models"]:
            for field, value in model.items():
                if field != "name":
                    models[model["name"]][field] += value

    return {
        "range": {
            "starting_at": days_out[0]["starting_at"] if days_out else None,
            "ending_at": days_out[-1]["ending_at"] if days_out else None,
        },
        "totals": totals,
        "models": [
            {"name": name, **values}
            for name, values in sorted(models.items(), key=lambda kv: (-kv[1]["total_tokens"], kv[0]))
        ],
        "daily": days_out,
        "fetched_at": _rfc3339(now),
    }


def _zero_model() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


