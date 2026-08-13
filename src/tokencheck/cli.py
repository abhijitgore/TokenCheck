"""TokenCheck command line interface."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any

from . import (
    __version__,
    api,
    auth,
    claudeconfig,
    gemini_api,
    openai_api,
    otlp,
    period,
    render,
    transcripts,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokencheck",
        description="Report token usage, rate-limit utilization and reset times for Claude, OpenAI and Gemini.",
    )
    parser.add_argument("--version", action="version", version=f"tokencheck {__version__}")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")

    sub = parser.add_subparsers(dest="command")

    limits = sub.add_parser(
        "limits",
        help="subscription rate-limit windows, utilization and reset times (default)",
    )
    limits.add_argument(
        "--provider",
        "-p",
        choices=("claude", "openai", "gemini", "all"),
        default="claude",
        help="which provider to report (default: claude; `all` reports every one you have credentials for)",
    )
    limits.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    limits.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    usage = sub.add_parser("usage", help="org-wide token counts and cost (needs an Admin API key)")
    usage.add_argument(
        "--provider",
        "-p",
        choices=("claude", "openai"),
        default="claude",
        help="which provider's usage report to fetch (Gemini publishes no token-count API)",
    )
    usage.add_argument("--daily", action="store_true", help="include the per-bucket breakdown")
    usage.add_argument("--admin-key", help="admin key for the chosen provider (else the env var)")
    usage.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    usage.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    whoami = sub.add_parser("whoami", help="which Claude account the credential belongs to")
    whoami.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    whoami.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    auth_cmd = sub.add_parser("auth", help="show which credential sources are available")
    auth_cmd.add_argument("--admin-key", help=argparse.SUPPRESS)
    auth_cmd.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    auth_cmd.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    local = sub.add_parser(
        "local", help="token consumption from local Claude Code session transcripts"
    )
    local.add_argument(
        "--by",
        choices=transcripts.GROUPINGS,
        help="add a breakdown by this dimension (day, model, project, session, tool, skill, effort, version)",
    )
    local.add_argument("--project", help="only count sessions whose path matches this substring")
    local.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    local.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    plan = sub.add_parser("plan", help="subscription tier, account, quota tier and usage counters")
    plan.add_argument("--offline", action="store_true", help="skip the live API call, use cached values")
    plan.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    plan.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    collect = sub.add_parser("collect", help="receive the OpenTelemetry stream Claude Code emits")
    collect.add_argument("--print-env", action="store_true", help="print the exports Claude Code needs, then exit")
    collect.add_argument("--port", type=int, default=otlp.DEFAULT_PORT, help=f"(default {otlp.DEFAULT_PORT})")
    collect.add_argument("--host", default=otlp.DEFAULT_HOST, help=argparse.SUPPRESS)
    collect.add_argument("--verbose", action="store_true", help="log every request received")
    collect.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    collect.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    telemetry = sub.add_parser("telemetry", help="show the captured OpenTelemetry metrics and events")
    telemetry.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    telemetry.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    every = sub.add_parser("all", help="plan, limits, local usage and telemetry in one view")
    every.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    every.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    for command in (local, plan, telemetry, every):
        command.add_argument(
            "--no-notes", action="store_true", help="drop the explanatory gloss beside each figure"
        )

    # Every command that reports over a span of time takes the same window flag.
    for command in (usage, local, telemetry, every):
        command.add_argument(
            "--period",
            metavar="PERIOD",
            help=(
                f"reporting window: {', '.join(period.CHOICES)}, or any `Nh`/`Nd` "
                f"(default: {period.DEFAULT})"
            ),
        )
        # Superseded by --period; kept so existing invocations keep working.
        command.add_argument("--days", type=int, help=argparse.SUPPRESS)

    return parser


def _emit(payload: Any, text: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(text)


_EXPIRED_CLAUDE = (
    "Claude OAuth token has expired. Run `claude` to re-authenticate.\n"
    "(TokenCheck deliberately never refreshes the token itself — refresh "
    "tokens rotate, and doing so would invalidate Claude Code's session.)"
)


def _claude_limits(include_raw: bool) -> dict[str, Any]:
    credential = auth.find_oauth_credential()
    if credential.is_expired:
        raise auth.AuthError(_EXPIRED_CLAUDE)

    payload = api.fetch_oauth_usage(credential.access_token)
    report: dict[str, Any] = {
        "provider": "claude",
        "title": "Claude subscription limits",
        "windows": api.parse_limit_windows(payload),
        "extra_usage": api.parse_extra_usage(payload),
        "subscription_type": credential.subscription_type,
        "credential_source": credential.source,
    }
    if include_raw:
        report["raw"] = payload
    return report


def _openai_limits(include_raw: bool) -> dict[str, Any]:
    credential = auth.find_codex_credential()
    if credential.is_expired:
        raise auth.AuthError(
            "Codex access token has expired. Run `codex login` to refresh it.\n"
            "(TokenCheck never redeems Codex's refresh token — that would rotate it "
            "out from under the Codex CLI's own session.)"
        )

    payload = openai_api.fetch_codex_usage(credential.access_token, credential.account_id)
    report = openai_api.codex_limits_report(payload, credential_source=credential.source)
    if include_raw:
        report["raw"] = payload
    return report


def _gemini_limits(include_raw: bool) -> dict[str, Any]:
    credential = auth.find_gemini_credential()
    if credential.is_expired:
        raise auth.AuthError(
            "Gemini access token has expired. Sign in again with the Gemini CLI "
            "(`gemini`) or Antigravity to refresh ~/.gemini/oauth_creds.json."
        )

    quota = gemini_api.fetch_quota(credential.access_token)
    tier = gemini_api.fetch_tier(credential.access_token)
    report = gemini_api.limits_report(quota, tier, credential_source=credential.source)
    if include_raw:
        report["raw"] = {"quota": quota, "tier": tier}
    return report


_LIMIT_PROVIDERS = {
    "claude": _claude_limits,
    "openai": _openai_limits,
    "gemini": _gemini_limits,
}


def _cmd_limits(args: argparse.Namespace, style: render.Style) -> int:
    provider = getattr(args, "provider", "claude")
    include_raw = bool(args.json)

    if provider != "all":
        report = _LIMIT_PROVIDERS[provider](include_raw)
        _emit(report, render.render_limits(report, style), as_json=args.json)
        return EXIT_OK

    # `all` is a survey, so one provider being unconfigured or down must not
    # suppress the others — each failure is reported in place.
    reports: list[dict[str, Any]] = []
    sections: list[str] = []
    failures = 0
    for name, fetch in _LIMIT_PROVIDERS.items():
        try:
            report = fetch(include_raw)
        except (auth.AuthError, api.APIError) as error:
            failures += 1
            reports.append({"provider": name, "error": str(error)})
            sections.append(render.render_provider_error(name, str(error), style))
            continue
        reports.append(report)
        sections.append(render.render_limits(report, style))

    _emit(reports, "\n\n".join(sections), as_json=args.json)
    return EXIT_OK if failures < len(_LIMIT_PROVIDERS) else EXIT_AUTH


def _cmd_usage(args: argparse.Namespace, style: render.Style) -> int:
    window = period.from_args(getattr(args, "period", None), getattr(args, "days", None))
    provider = getattr(args, "provider", "claude")

    if provider == "openai":
        key = auth.find_openai_admin_key(getattr(args, "admin_key", None))
        report = openai_api.fetch_org_usage(key, period=window)
    else:
        key = auth.find_admin_key(getattr(args, "admin_key", None))
        report = api.fetch_admin_usage(key, period=window)

    _emit(report, render.render_usage(report, style, show_daily=args.daily), as_json=args.json)
    return EXIT_OK


def _cmd_whoami(args: argparse.Namespace, style: render.Style) -> int:
    credential = auth.find_oauth_credential()
    payload = api.fetch_oauth_profile(credential.access_token)
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    organization = payload.get("organization") if isinstance(payload.get("organization"), dict) else {}
    profile = {
        "email": account.get("email_address") or account.get("email") or payload.get("email_address"),
        "account_uuid": account.get("uuid"),
        "organization_name": organization.get("name"),
        "organization_uuid": organization.get("uuid") or payload.get("organization_uuid"),
        "subscription_type": credential.subscription_type,
        "credential_source": credential.source,
    }
    if args.json:
        profile["raw"] = payload
    _emit(profile, render.render_whoami(profile, style), as_json=args.json)
    return EXIT_OK


def _cmd_auth(args: argparse.Namespace, style: render.Style) -> int:
    rows = auth.describe_sources(getattr(args, "admin_key", None))
    _emit(rows, render.render_auth(rows, style), as_json=args.json)
    return EXIT_OK


def _window(args: argparse.Namespace) -> period.Period:
    return period.from_args(getattr(args, "period", None), getattr(args, "days", None))


def _cmd_local(args: argparse.Namespace, style: render.Style) -> int:
    window = _window(args)
    records = transcripts.collect_usage(
        since=window.start(), project=getattr(args, "project", None)
    )
    report = transcripts.summarize(records, group_by=getattr(args, "by", None))
    report["period"] = window.label
    report["period_description"] = window.describe()
    notes = not getattr(args, "no_notes", False)
    _emit(report, render.render_local(report, style, notes=notes), as_json=args.json)
    return EXIT_OK


def _plan_report(offline: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "plan": claudeconfig.read_plan(),
        "counters": claudeconfig.read_counters(),
        "cached_utilization": claudeconfig.read_cached_utilization(),
        "stats": claudeconfig.read_stats(),
        "windows": [],
    }
    if offline:
        cached = report["cached_utilization"] or {}
        report["windows"] = [
            {"label": window["key"], "utilization": window["utilization"], "resets_at": window.get("resets_at")}
            for window in cached.get("windows", [])
        ]
        return report

    credential = auth.find_oauth_credential()
    if not credential.is_expired:
        payload = api.fetch_oauth_usage(credential.access_token)
        report["windows"] = api.parse_limit_windows(payload)
        report["extra_usage"] = api.parse_extra_usage(payload)
        report["plan"]["subscription_type"] = credential.subscription_type
    return report


def _cmd_plan(args: argparse.Namespace, style: render.Style) -> int:
    report = _plan_report(getattr(args, "offline", False))
    notes = not getattr(args, "no_notes", False)
    _emit(report, render.render_plan(report, style, notes=notes), as_json=args.json)
    return EXIT_OK


def _cmd_collect(args: argparse.Namespace, style: render.Style) -> int:
    exports = otlp.env_exports(args.port, args.host)
    if args.print_env:
        if args.json:
            print(json.dumps({"exports": exports, "store": str(otlp.data_dir())}, indent=2))
        else:
            print("\n".join(exports))
            print()
            print(
                style.dim(
                    "  Paste these into the shell you run `claude` from, then start "
                    "`tokencheck collect` in another shell."
                )
            )
        return EXIT_OK

    try:
        server = otlp.serve(args.host, args.port, verbose=args.verbose)
    except OSError as error:
        print(f"Cannot listen on {args.host}:{args.port} — {error}", file=sys.stderr)
        return EXIT_ERROR

    print(style.bold(f"Receiving Claude Code telemetry on http://{args.host}:{args.port}"))
    print(style.dim(f"  writing to {otlp.data_dir()}"))
    print(style.dim("  set these in the shell you run `claude` from:"))
    print()
    for line in exports:
        print(f"      {line}")
    print()
    print(style.dim("  Ctrl-C to stop; then `tokencheck telemetry` to read it back."))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print(style.dim("  stopped."))
    finally:
        server.server_close()
    return EXIT_OK


def _cmd_telemetry(args: argparse.Namespace, style: render.Style) -> int:
    window = _window(args)
    report = otlp.summarize(since=window.start())
    report["period"] = window.label
    report["period_description"] = window.describe()
    notes = not getattr(args, "no_notes", False)
    _emit(report, render.render_telemetry(report, style, notes=notes), as_json=args.json)
    return EXIT_OK


def _cmd_all(args: argparse.Namespace, style: render.Style) -> int:
    notes = not getattr(args, "no_notes", False)
    plan_report: dict[str, Any]
    try:
        plan_report = _plan_report(offline=False)
    except (auth.AuthError, api.APIError) as error:
        plan_report = _plan_report(offline=True)
        plan_report["live_error"] = str(error)

    window = _window(args)
    records = transcripts.collect_usage(since=window.start())
    local_report = transcripts.summarize(records)
    telemetry_report = otlp.summarize(since=window.start())
    for section in (local_report, telemetry_report):
        section["period"] = window.label
        section["period_description"] = window.describe()

    sections = [
        render.render_plan(plan_report, style, notes=notes),
        render.render_local(local_report, style, notes=notes),
        render.render_telemetry(telemetry_report, style, notes=notes),
    ]
    combined = {"plan": plan_report, "local": local_report, "telemetry": telemetry_report}
    _emit(combined, "\n\n".join(sections), as_json=args.json)
    return EXIT_OK


_COMMANDS = {
    "limits": _cmd_limits,
    "usage": _cmd_usage,
    "whoami": _cmd_whoami,
    "auth": _cmd_auth,
    "local": _cmd_local,
    "plan": _cmd_plan,
    "collect": _cmd_collect,
    "telemetry": _cmd_telemetry,
    "all": _cmd_all,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "limits"
    if args.command is None:
        # Bare `tokencheck` runs `limits`; re-parse so that subcommand's
        # defaults are present on the namespace.
        args = parser.parse_args([*argv, "limits"])

    style = render.Style(enabled=not args.no_color and render._color_enabled())

    try:
        return _COMMANDS[command](args, style)
    except period.PeriodError as error:
        print(str(error), file=sys.stderr)
        return EXIT_ERROR
    except auth.AuthError as error:
        print(str(error), file=sys.stderr)
        return EXIT_AUTH
    except api.APIError as error:
        print(str(error), file=sys.stderr)
        return EXIT_AUTH if error.status in (401, 403) else EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
