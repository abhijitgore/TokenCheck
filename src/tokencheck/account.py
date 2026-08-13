"""Individual subscription, or organization account?

TokenCheck is built for individual Claude subscriptions: it reads the OAuth
rate-limit windows behind a personal plan and the transcripts on this machine.
Team and Enterprise accounts are a different reporting problem — their data
lives in the Admin and Analytics APIs, aggregated across members — so the tool
says so and stops rather than half-working.

The reverse case matters too: Anthropic's Admin API is *unavailable* for
individual accounts, so `usage` cannot work on one no matter what key is stored.

Both checks **fail open**. If the signals are missing or unfamiliar the account
is reported `unknown` and nothing is blocked — a misread must never make the
tool unusable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claudeconfig

#: Set to any non-empty value to bypass the organization gate, in case the
#: signals below ever misclassify a working account.
OVERRIDE_ENV = "TOKENCHECK_SKIP_ACCOUNT_CHECK"

INDIVIDUAL = "individual"
ORGANIZATION = "organization"
UNKNOWN = "unknown"

#: `organizationType` as Claude Code records it. Every individual plan still
#: gets an "organization" — a personal one — so the type string, not the mere
#: presence of an org, is what distinguishes them.
_INDIVIDUAL_TYPES = {"claude_max", "claude_pro", "claude_free"}
_ORGANIZATION_MARKERS = ("team", "enterprise", "business")


@dataclass(frozen=True)
class Account:
    kind: str
    organization_type: str | None = None
    organization_name: str | None = None
    seat_tier: str | None = None
    billing_type: str | None = None
    reason: str = ""

    @property
    def is_organization(self) -> bool:
        return self.kind == ORGANIZATION

    @property
    def is_individual(self) -> bool:
        return self.kind == INDIVIDUAL


def _oauth_account(path: Path | None = None) -> dict[str, Any]:
    config = claudeconfig._load(path or claudeconfig.config_file())
    account = config.get("oauthAccount")
    return account if isinstance(account, dict) else {}


def classify(path: Path | None = None) -> Account:
    """Decide from what Claude Code recorded at login."""
    account = _oauth_account(path)
    org_type = account.get("organizationType")
    org_type = org_type.strip().lower() if isinstance(org_type, str) else None
    seat_tier = account.get("seatTier")
    billing = account.get("billingType")
    billing = billing.strip().lower() if isinstance(billing, str) else None
    name = account.get("organizationName")

    common = {
        "organization_type": org_type,
        "organization_name": name if isinstance(name, str) else None,
        "seat_tier": seat_tier if isinstance(seat_tier, str) else None,
        "billing_type": billing,
    }

    if org_type and any(marker in org_type for marker in _ORGANIZATION_MARKERS):
        return Account(ORGANIZATION, **common, reason=f"organization type is `{org_type}`")

    # A seat tier is a per-member concept: individual plans leave it null.
    if isinstance(seat_tier, str) and seat_tier.strip():
        return Account(ORGANIZATION, **common, reason=f"account holds a `{seat_tier}` seat")

    if billing in {"invoice", "enterprise"}:
        return Account(ORGANIZATION, **common, reason=f"billing type is `{billing}`")

    if org_type in _INDIVIDUAL_TYPES:
        return Account(INDIVIDUAL, **common, reason=f"subscription is `{org_type}`")

    return Account(
        UNKNOWN,
        **common,
        reason="no recognisable account-type signal in ~/.claude.json",
    )


def override_active(environment: dict[str, str] | None = None) -> bool:
    env = environment if environment is not None else os.environ
    return bool(env.get(OVERRIDE_ENV, "").strip())


def organization_notice(account: Account) -> str:
    """Why TokenCheck stops on an organization account, and where to go."""
    where = f" ({account.organization_name})" if account.organization_name else ""
    return (
        f"TokenCheck targets individual Claude subscriptions.\n\n"
        f"  This account is an organization{where} — {account.reason}.\n"
        "  Organization-wide usage is reported by Anthropic's Admin and Analytics\n"
        "  APIs, which aggregate across members; this tool reads one personal\n"
        "  subscription and the transcripts on this machine.\n\n"
        f"  If that is wrong, set {OVERRIDE_ENV}=1 to run anyway."
    )


def individual_usage_notice(account: Account) -> str:
    """Why `usage` cannot work on an individual account, and what to run instead."""
    plan = f" (yours is `{account.organization_type}`)" if account.organization_type else ""
    return (
        "`tokencheck usage` needs Anthropic's Admin API, which is unavailable for\n"
        f"individual accounts{plan}.\n\n"
        "  That report covers API billing. Subscription usage is not billed per\n"
        "  token and would not appear in it even with an admin key. For this\n"
        "  account the equivalent data is:\n\n"
        "    tokencheck limits    live rate-limit windows and reset times\n"
        "    tokencheck local     per-response token totals, with cost estimates"
    )
