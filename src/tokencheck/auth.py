"""Credential discovery for TokenCheck.

Two independent auth methods are supported:

1. **OAuth (Claude subscription)** — the access token Claude Code stores after
   `claude login`. Grants the per-account rate-limit endpoint
   ``GET /api/oauth/usage``.
2. **Admin API key** — an ``sk-ant-admin…`` key from the Anthropic Console.
   Grants the org-wide Usage & Cost API.

Nothing here ever writes: TokenCheck reads the Claude Code credential and leaves
it exactly as found. In particular it does *not* perform an OAuth refresh —
refresh tokens rotate, so redeeming Claude Code's refresh token would invalidate
the session Claude Code itself is holding.
"""

from __future__ import annotations

import base64
import functools
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KEYCHAIN_SERVICE = "Claude Code-credentials"

OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
ADMIN_KEY_ENVS = ("ANTHROPIC_ADMIN_KEY", "ANTHROPIC_ADMIN_API_KEY")
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"


class AuthError(Exception):
    """No usable credential could be found."""


@dataclass(frozen=True)
class OAuthCredential:
    access_token: str
    source: str
    expires_at: float | None = None
    scopes: tuple[str, ...] = ()
    subscription_type: str | None = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    @property
    def expires_in_seconds(self) -> float | None:
        if self.expires_at is None:
            return None
        return self.expires_at - time.time()


def claude_config_dir() -> Path:
    """Claude Code treats ``CLAUDE_CONFIG_DIR`` as one literal directory."""
    raw = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude"


def _parse_credentials_blob(blob: str, source: str) -> OAuthCredential | None:
    try:
        payload = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    # Claude Code nests the OAuth material under `claudeAiOauth`; tolerate a
    # flat shape too, since the layout has moved between versions.
    section = payload.get("claudeAiOauth")
    if not isinstance(section, dict):
        section = payload

    token = section.get("accessToken") or section.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None

    expires_at = section.get("expiresAt", section.get("expires_at"))
    expires_seconds: float | None = None
    if isinstance(expires_at, (int, float)):
        # Stored in milliseconds since epoch.
        expires_seconds = float(expires_at) / 1000.0 if expires_at > 1e11 else float(expires_at)

    raw_scopes = section.get("scopes") or section.get("scope") or ()
    if isinstance(raw_scopes, str):
        raw_scopes = raw_scopes.split()
    scopes = tuple(s for s in raw_scopes if isinstance(s, str))

    subscription = section.get("subscriptionType") or section.get("subscription_type")

    return OAuthCredential(
        access_token=token.strip(),
        source=source,
        expires_at=expires_seconds,
        scopes=scopes,
        subscription_type=subscription if isinstance(subscription, str) else None,
    )


def _from_env() -> OAuthCredential | None:
    token = os.environ.get(OAUTH_TOKEN_ENV, "").strip()
    if not token:
        return None
    return OAuthCredential(access_token=token, source=f"${OAUTH_TOKEN_ENV}")


#: Prefixes that distinguish an org admin key from an ordinary API key. The
#: usage endpoints reject the latter with a bare 401, which reads like a bad
#: credential rather than the wrong *kind* of credential — so name the mismatch.
KEY_PREFIXES = {
    "anthropic": {"admin": "sk-ant-admin", "regular": "sk-ant-api"},
    "openai": {"admin": "sk-admin", "regular": "sk-proj"},
}

#: OpenAI grants usage access either through an admin key or through a project
#: key carrying an explicit scope — its own 403 says so — so the warning names
#: both routes rather than insisting on an admin key.
_EXTRA_ROUTE = {
    "openai": " Alternatively, grant this key the `api.usage.read` scope.",
}


def key_kind_warning(key: str, vendor: str) -> str | None:
    """Warn when a key is clearly not the admin kind. None when it looks right."""
    prefixes = KEY_PREFIXES.get(vendor)
    if not prefixes or not key:
        return None
    if key.startswith(prefixes["admin"]):
        return None
    if key.startswith(prefixes["regular"]):
        return (
            f"This looks like a regular API key (`{prefixes['regular']}…`), not an "
            f"admin key (`{prefixes['admin']}…`). The usage endpoints will reject it."
            + _EXTRA_ROUTE.get(vendor, "")
        )
    return None


#: Keychain services TokenCheck looks in for admin keys, so they survive a
#: reboot without living in a shell rc file (which is often under version
#: control) or in the environment of every process you launch.
ANTHROPIC_ADMIN_KEYCHAIN_SERVICE = "anthropic_admin_key"
OPENAI_ADMIN_KEYCHAIN_SERVICE = "openai_admin_key"


#: Long enough for a slow Keychain, short enough that a blocking GUI prompt is
#: reported rather than hanging the command.
KEYCHAIN_TIMEOUT = 8


class KeychainBlocked(AuthError):
    """The item exists but macOS is gating it behind an approval dialog."""


def expires_in_phrase(seconds: float | None) -> str:
    """"in 43m" / "in 2d 3h" / "" — how long a credential has left."""
    if seconds is None:
        return ""
    if seconds <= 0:
        return "expired"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"in {max(1, minutes)}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"in {hours}h {minutes}m" if minutes else f"in {hours}h"
    days, hours = divmod(hours, 24)
    return f"in {days}d {hours}h" if hours else f"in {days}d"


def rotate_command(service: str, placeholder: str) -> str:
    """The command that replaces a stored key, ACL included.

    `-U` updates an existing item's *value* but keeps its original access
    control, so adding `-A` to an update is silently ineffective — an item first
    created without `-A` keeps prompting forever. Deleting first is what makes
    the new ACL take effect.
    """
    return (
        f"security delete-generic-password -s {service} 2>/dev/null; "
        f"security add-generic-password -a \"$USER\" -s {service} -w '{placeholder}' -A"
    )


@functools.lru_cache(maxsize=None)
def keychain_secret(service: str) -> str | None:
    """Read a generic-password item from the macOS login Keychain.

    Cached for the life of the process: several commands ask for the same key
    more than once, and each miss can cost a Keychain round-trip. A key rotated
    mid-run is therefore not picked up — fine for a short-lived CLI.

    A missing item and a non-macOS host both mean "no secret" and return None.
    A *blocked* read is different and raises: the item is there, but its access
    control only trusts the process that created it, so macOS puts up an
    approval dialog that a non-interactive run will never answer. Reporting
    that as "not set" would send you looking for a key you already stored.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise KeychainBlocked(
            f"Keychain item `{service}` exists but is waiting on an approval dialog.\n"
            "  Its access control only trusts the process that created it. Note that\n"
            "  `-U` updates the value but keeps the old ACL, so the item must be\n"
            "  deleted and re-added for `-A` to take effect:\n"
            f"    {rotate_command(service, 'KEY')}"
        ) from error
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    secret = result.stdout.strip()
    return secret or None


def _from_keychain() -> OAuthCredential | None:
    """Read the Claude Code credential from the macOS login Keychain.

    May surface a Keychain access prompt the first time. A denial, a missing
    item, and a non-macOS host all look the same here: no credential.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_credentials_blob(result.stdout.strip(), source="macOS Keychain")


def _from_file(path: Path) -> OAuthCredential | None:
    try:
        blob = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_credentials_blob(blob, source=str(path))


def credential_files() -> list[Path]:
    paths = [claude_config_dir() / ".credentials.json"]
    default = Path.home() / ".claude" / ".credentials.json"
    if default not in paths:
        paths.append(default)
    return paths


def find_oauth_credential() -> OAuthCredential:
    """Resolve an OAuth access token, in precedence order."""
    credential = _from_env() or _from_keychain()
    if credential is not None:
        return credential

    for path in credential_files():
        credential = _from_file(path)
        if credential is not None:
            return credential

    searched = ", ".join([f"${OAUTH_TOKEN_ENV}", "macOS Keychain", *map(str, credential_files())])
    raise AuthError(
        "No Claude OAuth credential found.\n"
        f"  Searched: {searched}\n"
        "  Run `claude` and sign in, or export "
        f"{OAUTH_TOKEN_ENV} (see `claude setup-token`)."
    )


def find_admin_key(explicit: str | None = None) -> str:
    """Resolve an Anthropic Admin API key: flag, then env, then Keychain."""
    if explicit and explicit.strip():
        return explicit.strip()
    for name in ADMIN_KEY_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    secret = keychain_secret(ANTHROPIC_ADMIN_KEYCHAIN_SERVICE)
    if secret:
        return secret
    raise AuthError(
        "No Anthropic Admin API key found.\n"
        f"  Searched: --admin-key, {', '.join('$' + n for n in ADMIN_KEY_ENVS)}, "
        f"macOS Keychain ({ANTHROPIC_ADMIN_KEYCHAIN_SERVICE})\n"
        "  The Usage & Cost API needs an admin key (starts with `sk-ant-admin`), "
        "not a regular API key.\n"
        "  Mint one at https://console.anthropic.com/settings/admin-keys "
        "(org owner/admin only), then store it so it survives a reboot:\n"
        f"    security add-generic-password -a \"$USER\" -s {ANTHROPIC_ADMIN_KEYCHAIN_SERVICE} -w 'sk-ant-admin...' -U"
    )


# --------------------------------------------------------------------------
# OpenAI — Codex CLI OAuth, and the organization admin key
# --------------------------------------------------------------------------

CODEX_HOME_ENV = "CODEX_HOME"
OPENAI_ADMIN_KEY_ENVS = ("OPENAI_ADMIN_KEY", "OPENAI_API_KEY")


@dataclass(frozen=True)
class CodexCredential:
    access_token: str
    source: str
    account_id: str | None = None
    auth_mode: str | None = None
    expires_at: float | None = None
    id_token: str | None = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    @property
    def expires_in_seconds(self) -> float | None:
        return None if self.expires_at is None else self.expires_at - time.time()

    def identity_claims(self) -> dict[str, Any]:
        """Account details carried in the id_token.

        These are a snapshot from the last token refresh, so anything the live
        API also reports (plan, email) should prefer the API. What only the
        claims have is org membership and role.

        Returns only the fields TokenCheck renders — never the raw token, and
        never the session identifiers (`sid`, `jti`) the claims also carry.
        """
        if not self.id_token:
            return {}
        claims = decode_jwt_claims(self.id_token)
        auth_claim = claims.get("https://api.openai.com/auth")
        auth_claim = auth_claim if isinstance(auth_claim, dict) else {}

        organizations = []
        for org in auth_claim.get("organizations") or []:
            if isinstance(org, dict):
                organizations.append(
                    {"id": org.get("id"), "title": org.get("title"), "role": org.get("role")}
                )

        return {
            "email": claims.get("email"),
            "user_id": auth_claim.get("chatgpt_user_id"),
            "account_id": auth_claim.get("chatgpt_account_id"),
            "plan_type": auth_claim.get("chatgpt_plan_type"),
            "organizations": organizations,
        }


def codex_home() -> Path:
    raw = os.environ.get(CODEX_HOME_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def codex_auth_file() -> Path:
    return codex_home() / "auth.json"


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read a JWT payload without verifying it.

    The signature is the issuer's to check; all we want is the descriptive
    claims — expiry, email, org membership — so we can report them without a
    network round-trip. Never trust these for authorization.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _jwt_expiry(token: str) -> float | None:
    expiry = decode_jwt_claims(token).get("exp")
    return float(expiry) if isinstance(expiry, (int, float)) else None


def _read_codex_credential() -> CodexCredential | None:
    path = codex_auth_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    tokens = payload.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else payload
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None

    id_token = tokens.get("id_token")
    return CodexCredential(
        access_token=token.strip(),
        source=str(path),
        account_id=tokens.get("account_id"),
        auth_mode=payload.get("auth_mode"),
        expires_at=_jwt_expiry(token.strip()),
        id_token=id_token if isinstance(id_token, str) else None,
    )


def find_codex_credential() -> CodexCredential:
    credential = _read_codex_credential()
    if credential is not None:
        return credential
    raise AuthError(
        "No Codex credential found.\n"
        f"  Searched: {codex_auth_file()}\n"
        "  Install the Codex CLI and run `codex login` (ChatGPT sign-in)."
    )


def find_openai_admin_key(explicit: str | None = None) -> str:
    """Resolve an OpenAI admin key: flag, then env, then Keychain."""
    if explicit and explicit.strip():
        return explicit.strip()
    for name in OPENAI_ADMIN_KEY_ENVS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    secret = keychain_secret(OPENAI_ADMIN_KEYCHAIN_SERVICE)
    if secret:
        return secret
    raise AuthError(
        "No OpenAI admin key found.\n"
        f"  Searched: --admin-key, {', '.join('$' + n for n in OPENAI_ADMIN_KEY_ENVS)}, "
        f"macOS Keychain ({OPENAI_ADMIN_KEYCHAIN_SERVICE})\n"
        "  The Usage API needs an admin key (starts with `sk-admin`), not a project key.\n"
        "  Mint one at https://platform.openai.com/settings/organization/admin-keys, "
        "then store it so it survives a reboot:\n"
        f"    security add-generic-password -a \"$USER\" -s {OPENAI_ADMIN_KEYCHAIN_SERVICE} -w 'sk-admin...' -U"
    )


# --------------------------------------------------------------------------
# Gemini — the OAuth credential the Gemini CLI writes
# --------------------------------------------------------------------------

GEMINI_HOME_ENV = "GEMINI_CONFIG_DIR"


@dataclass(frozen=True)
class GeminiCredential:
    access_token: str
    source: str
    expires_at: float | None = None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    @property
    def expires_in_seconds(self) -> float | None:
        return None if self.expires_at is None else self.expires_at - time.time()


def gemini_config_dir() -> Path:
    raw = os.environ.get(GEMINI_HOME_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".gemini"


def gemini_credential_file() -> Path:
    return gemini_config_dir() / "oauth_creds.json"


def _read_gemini_credential() -> GeminiCredential | None:
    path = gemini_credential_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None

    # `expiry_date` is milliseconds since epoch, and may carry a fractional part.
    raw_expiry = payload.get("expiry_date")
    expires_at = float(raw_expiry) / 1000.0 if isinstance(raw_expiry, (int, float)) else None

    return GeminiCredential(access_token=token.strip(), source=str(path), expires_at=expires_at)


def find_gemini_credential() -> GeminiCredential:
    credential = _read_gemini_credential()
    if credential is not None:
        return credential
    raise AuthError(
        "No Gemini credential found.\n"
        f"  Searched: {gemini_credential_file()}\n"
        "  Sign in with the Gemini CLI (`gemini`) or Antigravity to create it."
    )


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


#: Below this, a credential is close enough to expiry to say so up front.
EXPIRY_WARNING_SECONDS = 15 * 60


def _expiry_detail(credential: Any, reauth: str) -> str:
    """"valid (expires in 43m)" / "expires in 4m — <reauth>" / "expired — <reauth>"."""
    if credential is None:
        return "not found"
    if credential.is_expired:
        return f"expired — {reauth}"
    remaining = getattr(credential, "expires_in_seconds", None)
    if remaining is None:
        return "valid"
    phrase = expires_in_phrase(remaining)
    if remaining <= EXPIRY_WARNING_SECONDS:
        return f"expires {phrase} — {reauth}"
    return f"valid (expires {phrase})"


def _credential_row(
    provider: str, source: str, credential: Any, reauth: str, missing: str = "not found"
) -> dict[str, object]:
    """One inventory row. `available` means found; `valid` means usable *now*.

    The two differ for an expired token — the file is right there, but a command
    relying on it will fail, so callers that are advising the user (like
    `setup`) must key off `valid`, not `available`.
    """
    return {
        "provider": provider,
        "method": "oauth",
        "source": source,
        "available": credential is not None,
        "valid": credential is not None and not credential.is_expired,
        "detail": _expiry_detail(credential, reauth) if credential is not None else missing,
    }


def describe_sources(
    explicit_admin_key: str | None = None,
    explicit_openai_key: str | None = None,
) -> list[dict[str, object]]:
    """Report which credential sources are present, for every provider.

    Never returns secrets — only whether each source resolved, and whether the
    credential it holds is still in date.
    """
    rows: list[dict[str, object]] = []

    env_token = os.environ.get(OAUTH_TOKEN_ENV, "").strip()
    rows.append(
        {
            "provider": "claude",
            "method": "oauth",
            "source": f"${OAUTH_TOKEN_ENV}",
            "available": bool(env_token),
            "valid": bool(env_token),
            "detail": "set" if env_token else "not set",
        }
    )

    keychain = _from_keychain()
    rows.append(
        _credential_row(
            "claude",
            f"macOS Keychain ({KEYCHAIN_SERVICE})",
            keychain,
            "run `claude` to re-authenticate",
            missing="not found (or access denied)",
        )
    )

    for path in credential_files():
        credential = _from_file(path)
        rows.append(
            {
                "provider": "claude",
                "method": "oauth",
                "source": str(path),
                "available": credential is not None,
                "valid": credential is not None,
                "detail": "readable" if credential is not None else "not found",
            }
        )

    try:
        key = find_admin_key(explicit_admin_key)
        admin_available = True
        admin_detail = key_kind_warning(key, "anthropic") or "set"
    except KeychainBlocked:
        admin_available, admin_detail = False, "stored, but Keychain approval pending"
    except AuthError:
        admin_available, admin_detail = False, "not set"
    rows.append(
        {
            "provider": "claude",
            "method": "admin",
            "source": "--admin-key / $"
            + " / $".join(ADMIN_KEY_ENVS)
            + f" / Keychain ({ANTHROPIC_ADMIN_KEYCHAIN_SERVICE})",
            "available": admin_available,
            "valid": admin_available,
            "detail": admin_detail,
        }
    )

    codex = _read_codex_credential()
    rows.append(_credential_row("openai", str(codex_auth_file()), codex, "run `codex login`"))

    try:
        key = find_openai_admin_key(explicit_openai_key)
        openai_available = True
        openai_detail = key_kind_warning(key, "openai") or "set"
    except KeychainBlocked:
        openai_available, openai_detail = False, "stored, but Keychain approval pending"
    except AuthError:
        openai_available, openai_detail = False, "not set"
    rows.append(
        {
            "provider": "openai",
            "method": "admin",
            "source": "--admin-key / $"
            + " / $".join(OPENAI_ADMIN_KEY_ENVS)
            + f" / Keychain ({OPENAI_ADMIN_KEYCHAIN_SERVICE})",
            "available": openai_available,
            "valid": openai_available,
            "detail": openai_detail,
        }
    )

    gemini = _read_gemini_credential()
    rows.append(
        _credential_row(
            "gemini", str(gemini_credential_file()), gemini, "run `gemini` (tokens last ~1 hour)"
        )
    )

    return rows
