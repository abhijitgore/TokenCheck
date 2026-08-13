"""Gemini: per-model request quota from the Cloud Code private API.

`POST cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` returns one
bucket per model with the fraction of quota still remaining, plus the reset
time. `:loadCodeAssist` names the account's tier. Both are authenticated with
the OAuth access token Gemini CLI stores in `~/.gemini/oauth_creds.json`.
Endpoints ported from CodexBar (MIT, © 2026 Peter Steinberger).

**What Gemini does not offer:** there is no counterpart to Anthropic's or
OpenAI's usage report — no public API returns token counts or spend per model.
Quota-remaining is the whole picture here, so `tokencheck usage` has no Gemini
mode and `--period` does not apply to it.
"""

from __future__ import annotations

import json
from typing import Any

from .api import APIError, _get_json  # noqa: F401 - APIError re-raised by callers
from .util import as_float, parse_iso

QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
LOAD_CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"

REAUTH_HINT = (
    "The Gemini credential is expired or revoked — sign in again with the Gemini "
    "CLI (`gemini`) or Antigravity to refresh ~/.gemini/oauth_creds.json."
)


def _post(url: str, access_token: str, body: dict[str, Any], *, label: str) -> dict[str, Any]:
    payload = _get_json(
        url,
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TokenCheck/0.1",
        },
        label=label,
        auth_hint=REAUTH_HINT,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
    )
    if not isinstance(payload, dict):
        raise APIError(f"{label}: unexpected response shape")
    return payload


def fetch_quota(access_token: str) -> dict[str, Any]:
    # The endpoint rejects any request body fields — it must be a bare object.
    return _post(QUOTA_URL, access_token, {}, label="gemini quota")


def fetch_tier(access_token: str) -> dict[str, Any] | None:
    """Best-effort tier lookup. Never fatal: some accounts get no tier at all."""
    try:
        return _post(
            LOAD_CODE_ASSIST_URL,
            access_token,
            {"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
            label="gemini tier",
        )
    except APIError:
        return None


def parse_quota_windows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn quota buckets into utilization windows.

    The API reports what is *left*; every other provider in TokenCheck reports
    what is *used*, so invert it here rather than special-casing the renderer.
    """
    windows: list[dict[str, Any]] = []
    for bucket in payload.get("buckets") or []:
        if not isinstance(bucket, dict):
            continue
        remaining = as_float(bucket.get("remainingFraction"))
        if remaining is None:
            continue
        model = bucket.get("modelId") or "unknown"
        token_type = bucket.get("tokenType")
        label = str(model)
        if token_type and str(token_type).upper() != "REQUESTS":
            label = f"{model} ({str(token_type).lower()})"
        windows.append(
            {
                "label": label,
                "key": str(model),
                "utilization": (1.0 - max(0.0, min(1.0, remaining))) * 100,
                "remaining_fraction": remaining,
                "token_type": token_type,
                "resets_at": bucket.get("resetTime"),
            }
        )

    windows.sort(key=lambda w: (-w["utilization"], w["label"]))
    return windows


def parse_tier(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the tier name. Absent for accounts with no eligible tier."""
    if not isinstance(payload, dict):
        return {}

    current = payload.get("currentTier")
    if isinstance(current, dict) and (current.get("name") or current.get("id")):
        return {
            "tier_id": current.get("id"),
            "tier_name": current.get("name"),
            "project_id": payload.get("cloudaicompanionProject"),
        }

    # No current tier: report the default allowed tier so the header is not blank,
    # and surface why a tier is unavailable when the API says so.
    allowed = [t for t in payload.get("allowedTiers") or [] if isinstance(t, dict)]
    default = next((t for t in allowed if t.get("isDefault")), allowed[0] if allowed else None)
    ineligible = [t for t in payload.get("ineligibleTiers") or [] if isinstance(t, dict)]
    return {
        "tier_id": (default or {}).get("id"),
        "tier_name": (default or {}).get("name"),
        "project_id": payload.get("cloudaicompanionProject"),
        "ineligible": [
            {"tier": t.get("tierName") or t.get("tierId"), "reason": t.get("reasonMessage")}
            for t in ineligible
        ],
    }


def limits_report(
    quota: dict[str, Any], tier: dict[str, Any] | None, *, credential_source: str
) -> dict[str, Any]:
    info = parse_tier(tier)
    windows = parse_quota_windows(quota)
    report = {
        "provider": "gemini",
        "title": "Gemini quota",
        "windows": windows,
        "extra_usage": None,
        "subscription_type": info.get("tier_name") or info.get("tier_id"),
        "account": info.get("project_id"),
        "credential_source": credential_source,
        "note": "quota remaining per model; Gemini publishes no token-count API",
    }
    if info.get("ineligible"):
        report["ineligible_tiers"] = info["ineligible"]
    return report


USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Google account identity for the signed-in Gemini user."""
    payload = _get_json(
        USERINFO_URL,
        {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "TokenCheck/0.1",
        },
        label="gemini userinfo",
        auth_hint=REAUTH_HINT,
    )
    return payload if isinstance(payload, dict) else {}


def identity(
    userinfo: dict[str, Any], tier: dict[str, Any] | None, *, credential_source: str
) -> dict[str, Any]:
    info = parse_tier(tier)
    return {
        "provider": "gemini",
        "title": "Gemini account",
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "account_uuid": userinfo.get("sub"),
        "organization_name": userinfo.get("hd"),
        "project_id": info.get("project_id"),
        "subscription_type": info.get("tier_name") or info.get("tier_id"),
        "credential_source": credential_source,
    }


def next_reset(windows: list[dict[str, Any]]) -> str | None:
    stamps = [w.get("resets_at") for w in windows if w.get("resets_at")]
    parsed = [(parse_iso(s), s) for s in stamps]
    valid = [(moment, raw) for moment, raw in parsed if moment is not None]
    return min(valid)[1] if valid else None
