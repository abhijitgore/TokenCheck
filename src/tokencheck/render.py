"""Terminal rendering: utilization bars, humanized reset times, tables."""

from __future__ import annotations

import datetime as dt
import os
import sys
from typing import Any, Iterable, Sequence

BAR_WIDTH = 24


def _color_enabled(stream: Any = sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TOKENCHECK_FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


class Style:
    """ANSI styling, all of it gated on `enabled`.

    Everything decorative lives behind these helpers, so `--no-color` output is
    the same text with the escapes removed — never a different layout.
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def accent(self, text: str) -> str:
        """The one accent colour — section headings and rules."""
        return self._wrap("36", text)

    def heading(self, text: str) -> str:
        return self._wrap("1;36", text)

    def for_utilization(self, percent: float, text: str) -> str:
        if percent >= 90:
            return self.red(text)
        if percent >= 70:
            return self.yellow(text)
        return self.green(text)

    def status(self, ok: bool) -> str:
        return self.green("✓") if ok else self.dim("·")


def section(title: str, style: Style, subtitle: str | None = None, width: int = 64) -> list[str]:
    """A titled section: accented heading over a rule the title's width."""
    lines = [style.heading(title), style.accent("─" * min(max(len(title), 24), width))]
    if subtitle:
        lines.append(style.dim(f"  {subtitle}"))
    return lines


#: Eighths of a block, so a bar resolves finer than its cell count.
_PARTIALS = " ▏▎▍▌▋▊▉"


def bar(percent: float, width: int = BAR_WIDTH) -> str:
    """A utilization bar with sub-cell resolution.

    Rounding to whole cells makes 1% and 4% look identical; the partial-block
    glyphs give each of the low percentages a visibly different bar, which is
    the range subscription usage actually sits in most of the time.
    """
    clamped = max(0.0, min(percent, 100.0))
    eighths = int(round(clamped / 100 * width * 8))
    filled, remainder = divmod(eighths, 8)
    out = "█" * filled
    if remainder and filled < width:
        out += _PARTIALS[remainder]
    return out + "░" * (width - _visible_len(out))


def parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def humanize_reset(value: Any, *, now: dt.datetime | None = None) -> str:
    """"in 2h 14m (Aug 12, 6:30 PM)" — or "" when there is no reset time."""
    moment = parse_iso(value)
    if moment is None:
        return ""
    now = now or dt.datetime.now(dt.timezone.utc)
    local = moment.astimezone()
    stamp = local.strftime("%b %-d, %-I:%M %p")
    delta = moment - now
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return f"due now ({stamp})"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return f"in {' '.join(parts) or '<1m'} ({stamp})"


def thousands(value: int | float) -> str:
    return f"{value:,.0f}"


def compact_tokens(value: int) -> str:
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(value)


def table(rows: Sequence[Sequence[str]], headers: Sequence[str], style: Style, right: Iterable[int] = ()) -> str:
    right_set = set(right)
    columns = list(zip(*([headers, *rows]))) if rows else [(h,) for h in headers]
    widths = [max(_visible_len(cell) for cell in column) for column in columns]

    def line(cells: Sequence[str], dim: bool = False) -> str:
        out = []
        for index, cell in enumerate(cells):
            pad = " " * (widths[index] - _visible_len(cell))
            out.append(pad + cell if index in right_set else cell + pad)
        text = "  ".join(out).rstrip()
        return style.dim(text) if dim else text

    parts = [style.bold(line(headers))]
    parts.append(style.dim("─" * min(sum(widths) + 2 * (len(widths) - 1), 100)))
    parts.extend(line(row) for row in rows)
    return "\n".join(parts)


def _visible_len(text: str) -> int:
    out, in_escape = 0, False
    for char in text:
        if in_escape:
            in_escape = char != "m"
            continue
        if char == "\033":
            in_escape = True
            continue
        out += 1
    return out


# --------------------------------------------------------------------------
# Command renderers
# --------------------------------------------------------------------------


def render_limits(report: dict[str, Any], style: Style) -> str:
    """Render one provider's rate-limit windows.

    Shared by Claude, ChatGPT/Codex and Gemini — each supplies its own `title`
    and a list of `windows` normalized to percent-used plus a reset time.
    """
    header = report.get("title") or "Claude subscription limits"
    plan = report.get("subscription_type")
    if plan:
        header += f"  ({plan})"
    lines = section(header, style, report.get("account"))
    lines.append("")

    windows = report.get("windows") or []
    if not windows:
        lines.append("  No rate-limit windows reported for this account.")
        for entry in report.get("ineligible_tiers") or []:
            reason = entry.get("reason") or "ineligible"
            lines.append(style.dim(f"  {entry.get('tier')}: {reason}"))
        lines.append("")
        lines.append(style.dim(f"  source: {report.get('credential_source', 'unknown')}"))
        return "\n".join(lines)

    label_width = max(len(w["label"]) for w in windows)
    for window in windows:
        percent = float(window["utilization"])
        painted = style.for_utilization(percent, f"{bar(percent)} {percent:5.1f}%")
        reset = humanize_reset(window.get("resets_at"))
        suffix = style.dim(f"  resets {reset}") if reset else ""
        lines.append(f"  {window['label']:<{label_width}}  {painted}{suffix}")

    extra = report.get("extra_usage")
    if extra:
        lines.extend(_render_extra_usage(extra, style))

    for entry in report.get("ineligible_tiers") or []:
        reason = entry.get("reason") or "ineligible"
        lines.append("")
        lines.append(style.dim(f"  {entry.get('tier')}: {reason}"))

    lines.append("")
    note = report.get("note")
    if note:
        lines.append(style.dim(f"  {note}"))

    # Warn while the credential still works, so a refresh can happen before the
    # next run fails rather than after.
    warning = report.get("expiry_warning")
    if warning:
        lines.append(style.yellow(f"  {warning}"))

    lines.append(style.dim(f"  source: {report.get('credential_source', 'unknown')}"))
    return "\n".join(lines)


def _render_extra_usage(extra: dict[str, Any], style: Style) -> list[str]:
    lines = ["", style.bold("Extra usage (pay-as-you-go)")]
    used, limit = extra.get("used"), extra.get("limit")
    currency = extra.get("currency", "USD")
    percent = extra.get("utilization")
    if percent is None and used is not None and limit:
        percent = used / limit * 100

    detail = ""
    if used is not None and limit is not None:
        detail = f"  {used:,.2f} / {limit:,.2f} {currency}"

    if percent is not None:
        painted = style.for_utilization(percent, f"{bar(percent)} {percent:5.1f}%")
        lines.append(f"  monthly credits  {painted}{style.dim(detail)}")
    elif detail:
        lines.append(f"  monthly credits {detail}")
    elif extra.get("unlimited"):
        lines.append("  credits          unlimited")
    elif extra.get("balance") is not None:
        lines.append(f"  credits          balance {extra['balance']}")
    else:
        lines.pop()  # nothing worth printing under the heading
        lines.pop()
    return lines


def render_usage(report: dict[str, Any], style: Style, *, show_daily: bool) -> str:
    lines: list[str] = []
    totals = report.get("totals") or {}
    window = report.get("range") or {}
    start = (window.get("starting_at") or "")[:10]
    end = (window.get("ending_at") or "")[:10]
    span = f"{start} → {end}" if start and end else "no data in range"

    title = report.get("title") or "Claude API org usage"
    described = report.get("period_description")
    subtitle = f"{described}   {span}" if described else span
    lines.extend(section(title, style, subtitle))
    lines.append("")
    lines.append(
        "  "
        + "   ".join(
            [
                f"input {thousands(totals.get('input_tokens', 0))}",
                f"cache-write {thousands(totals.get('cache_creation_input_tokens', 0))}",
                f"cache-read {thousands(totals.get('cache_read_input_tokens', 0))}",
                f"output {thousands(totals.get('output_tokens', 0))}",
            ]
        )
    )
    cost_note = " (day so far)" if report.get("cost_is_daily_only") else ""
    lines.append(
        "  "
        + style.bold(f"total {thousands(totals.get('total_tokens', 0))} tokens")
        + style.dim(f"   cost ${totals.get('cost_usd', 0.0):,.2f}{cost_note}")
    )
    if report.get("requests"):
        lines.append(style.dim(f"  {thousands(report['requests'])} model requests"))

    models = report.get("models") or []
    if models:
        lines.append("")
        lines.append(style.bold("By model"))
        rows = [
            [
                model["name"],
                compact_tokens(model["input_tokens"]),
                compact_tokens(model["cache_creation_input_tokens"]),
                compact_tokens(model["cache_read_input_tokens"]),
                compact_tokens(model["output_tokens"]),
                compact_tokens(model["total_tokens"]),
            ]
            for model in models
        ]
        lines.append(
            _indent(
                table(
                    rows,
                    ["MODEL", "INPUT", "CACHE-W", "CACHE-R", "OUTPUT", "TOTAL"],
                    style,
                    right=(1, 2, 3, 4, 5),
                )
            )
        )

    daily = report.get("daily") or []
    if show_daily and daily:
        hourly = report.get("cost_is_daily_only") or _looks_hourly(daily)
        lines.append("")
        lines.append(style.bold("By hour" if hourly else "By day"))
        # Cost is only ever reported per day, so an hourly table would carry a
        # column of zeros that contradicts the total above it. Omit it instead.
        rows = [
            [
                _bucket_label(day, hourly),
                compact_tokens(day["input_tokens"]),
                compact_tokens(day["cache_creation_input_tokens"]),
                compact_tokens(day["cache_read_input_tokens"]),
                compact_tokens(day["output_tokens"]),
                compact_tokens(day["total_tokens"]),
                *([] if hourly else [f"${day['cost_usd']:,.2f}"]),
            ]
            for day in daily
        ]
        headers = ["HOUR" if hourly else "DAY", "INPUT", "CACHE-W", "CACHE-R", "OUTPUT", "TOTAL"]
        if not hourly:
            headers.append("COST")
        lines.append(
            _indent(table(rows, headers, style, right=range(1, len(headers))))
        )
    elif not daily:
        lines.append("")
        lines.append(style.dim("  No usage recorded in this window."))

    return "\n".join(lines)


def render_auth(rows: list[dict[str, Any]], style: Style) -> str:
    table_rows = [
        [
            style.green("✓") if row["available"] else style.dim("·"),
            str(row.get("provider", "claude")),
            str(row["method"]),
            str(row["source"]),
            style.dim(str(row["detail"])),
        ]
        for row in rows
    ]
    return (
        style.bold("Credential sources")
        + "\n\n"
        + _indent(table(table_rows, ["", "PROVIDER", "METHOD", "SOURCE", "STATUS"], style))
    )


def _looks_hourly(buckets: list[dict[str, Any]]) -> bool:
    """True when two buckets share a calendar day — so the day column would repeat."""
    days = [b.get("day") for b in buckets if b.get("day")]
    return len(days) > len(set(days))


def _bucket_label(bucket: dict[str, Any], hourly: bool) -> str:
    if not hourly:
        return str(bucket.get("day", ""))
    moment = parse_iso(bucket.get("starting_at"))
    if moment is None:
        return str(bucket.get("day", ""))
    return moment.astimezone().strftime("%b %-d %H:%M")


PROVIDER_NAMES = {"claude": "Claude", "openai": "ChatGPT / Codex", "gemini": "Gemini"}


def render_provider_error(provider: str, message: str, style: Style, suffix: str = "") -> str:
    """Stand-in section for a provider that could not be reached under `--provider all`.

    Titled to match the command that was asked for, so an unreachable provider
    reads as the same section it would have been, not a differently-named one.
    """
    title = PROVIDER_NAMES.get(provider, provider)
    lines = section(f"{title} {suffix}".strip(), style)
    lines.append("")
    for index, line in enumerate(message.splitlines()):
        # First line is the problem; the rest is guidance.
        lines.append(style.yellow(f"  {line}") if index == 0 else style.dim(f"  {line}"))
    return "\n".join(lines)


#: Rendered in this order; absent fields are skipped, so one layout serves
#: three providers that each expose a different subset.
_WHOAMI_FIELDS = (
    ("name", "name"),
    ("email", "email"),
    ("plan", "subscription_type"),
    ("organization", "organization_name"),
    ("org role", "organization_role"),
    ("org uuid", "organization_uuid"),
    ("project", "project_id"),
    ("account uuid", "account_uuid"),
    ("user id", "user_id"),
    ("credential", "credential_source"),
)


def render_whoami(profile: dict[str, Any], style: Style) -> str:
    lines = section(profile.get("title") or "Claude account", style)
    lines.append("")

    present = [(label, profile.get(key)) for label, key in _WHOAMI_FIELDS if profile.get(key)]
    width = max((len(label) for label, _ in present), default=0)
    for label, value in present:
        lines.append(f"  {style.dim(label.ljust(width))}   {value}")

    extra = [org for org in profile.get("organizations") or [] if org.get("id")]
    if len(extra) > 1:
        lines.append("")
        lines.append(style.dim(f"  {len(extra)} organizations:"))
        for org in extra:
            role = f" ({org['role']})" if org.get("role") else ""
            lines.append(f"    {org.get('title') or org['id']}{style.dim(role)}")

    return "\n".join(lines)


def render_setup(report: dict[str, Any], style: Style) -> str:
    """What this machine has, what it is missing, and the fix for each gap."""
    steps = report.get("steps") or []
    # Optional steps are not gaps: an Anthropic Admin key cannot be provisioned
    # at all on an individual account, so counting it as missing would nag
    # forever about something the user cannot fix.
    core = [s for s in steps if not s.get("optional")]
    done = [s for s in core if s["done"]]
    todo = [s for s in steps if not s["done"] and not s.get("optional")]
    optional_todo = [s for s in steps if not s["done"] and s.get("optional")]

    lines = section(
        "TokenCheck setup", style, f"{len(done)} of {len(core)} credentials configured"
    )
    lines.append("")

    width = max((len(s["name"]) for s in steps), default=0)
    for step in steps:
        # Expired or blocked is distinct from missing: the credential exists,
        # so the fix is renewing or unlocking it, not creating a new one.
        needs_attention = step.get("expired") or step.get("blocked")
        if step.get("optional") and not step["done"]:
            mark = style.dim("–")
        elif needs_attention:
            mark = style.yellow("!")
        else:
            mark = style.status(step["done"])
        detail = step.get("status_detail") or (
            "signed in but expired" if step.get("expired") else step["unlocks"]
        )
        lines.append(f"  {mark} {step['name'].ljust(width)}   {style.dim(detail)}")

    if todo:
        lines.append("")
        lines.extend(section("To finish", style))
        for step in todo:
            lines.append("")
            suffix = ""
            if step.get("status_detail"):
                suffix = f" — {step['status_detail']}"
            elif step.get("expired"):
                suffix = " (expired)"
            lines.append(f"  {step['name']}{suffix}")
            if step.get("blocked"):
                lines.append(
                    style.dim("      Dismiss the macOS prompt, then re-store with -A so any")
                )
                lines.append(style.dim("      app can read it without prompting:"))
            lines.append(f"      {style.bold(step['fix'])}")
            if step.get("note"):
                lines.append(f"      {style.dim(step['note'])}")
    elif not optional_todo:
        lines.append("")
        lines.append(style.green("  Everything is configured."))
    else:
        lines.append("")
        lines.append(style.green("  Everything required is configured."))

    if optional_todo:
        lines.append("")
        lines.extend(section("Optional", style))
        for step in optional_todo:
            lines.append("")
            lines.append(f"  {step['name']}")
            if step.get("note"):
                lines.append(f"      {style.dim(step['note'])}")

    telemetry = report.get("telemetry") or {}
    lines.append("")
    lines.extend(section("Optional: capture emitted telemetry", style))
    if telemetry.get("capturing"):
        lines.append("")
        lines.append(f"  {style.status(True)} already capturing to {telemetry.get('store')}")
        lines.append(style.dim("      read it back with `tokencheck telemetry`"))
    else:
        lines.append("")
        lines.append(style.dim("  Claude Code emits OpenTelemetry only when something is listening."))
        lines.append(style.dim("  Run `tokencheck collect` in one shell, then in the shell you run"))
        lines.append(style.dim("  `claude` from:"))
        lines.append("")
        for export in telemetry.get("exports") or []:
            lines.append(f"      {style.dim(export)}")

    return "\n".join(lines)


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# --------------------------------------------------------------------------
# Local telemetry renderers
#
# Each figure is printed with a short gloss beside it. The vocabulary here
# ("cache write", "sidechain", "effort") is Anthropic's, not general knowledge,
# and a number nobody can interpret is not telemetry — it is noise.
# --------------------------------------------------------------------------

_LOCAL_ROWS = (
    ("input", "input_tokens"),
    ("cache write 1h", "cache_creation_1h_tokens"),
    ("cache write 5m", "cache_creation_5m_tokens"),
    ("cache read", "cache_read_input_tokens"),
    ("output", "output_tokens"),
    ("total", "total_tokens"),
)


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _explain(style: Style, note: str) -> str:
    return style.dim(f"  {note}") if note else ""


def render_local(report: dict[str, Any], style: Style, *, notes: bool = True) -> str:
    totals = report.get("totals") or {}
    notes_map = report.get("field_notes") or {}
    window = report.get("range") or {}
    first, last = (window.get("first") or "")[:10], (window.get("last") or "")[:10]
    span = f"{first} → {last}" if first and last else "no usage recorded"

    lines = [style.bold(f"Claude local usage  ({span})")]
    lines.append(
        style.dim(
            f"  {totals.get('responses', 0):,} API responses · "
            f"{totals.get('sessions', 0):,} sessions · "
            f"{totals.get('projects', 0):,} projects · "
            "from Claude Code session transcripts"
        )
    )
    lines.append("")

    if not totals.get("responses"):
        lines.append("  No transcripts found under ~/.claude/projects.")
        return "\n".join(lines)

    label_width = max(len(label) for label, _ in _LOCAL_ROWS)
    value_width = max(len(thousands(totals.get(key, 0))) for _, key in _LOCAL_ROWS)
    for label, key in _LOCAL_ROWS:
        value = thousands(totals.get(key, 0))
        row = f"  {label:<{label_width}}  {value:>{value_width}}"
        if label == "total":
            row = f"  {style.bold(label.ljust(label_width))}  {style.bold(value.rjust(value_width))}"
        lines.append(row + (_explain(style, notes_map.get(key, "")) if notes else ""))

    cost = totals.get("cost_usd")
    cost_row = f"  {'est. cost':<{label_width}}  {_money(cost):>{value_width}}"
    lines.append(cost_row + (_explain(style, report.get("cost_note", "")) if notes else ""))
    if totals.get("unpriced_responses"):
        lines.append(
            style.dim(
                f"  {totals['unpriced_responses']:,} responses used a model with no known price "
                "and are excluded from the estimate"
            )
        )

    for label, key in (("web searches", "web_search_requests"), ("web fetches", "web_fetch_requests")):
        if totals.get(key):
            lines.append(
                f"  {label:<{label_width}}  {thousands(totals[key]):>{value_width}}"
                + (_explain(style, notes_map.get(key, "")) if notes else "")
            )

    sidechain = report.get("sidechain")
    if sidechain:
        share = sidechain["total_tokens"] / max(totals.get("total_tokens", 1), 1) * 100
        lines.append("")
        lines.append(
            style.dim(
                f"  of which subagents/sidechains: {sidechain['responses']:,} responses, "
                f"{compact_tokens(sidechain['total_tokens'])} tokens ({share:.1f}%), "
                f"{_money(sidechain['cost_usd'])} — work delegated away from the main thread"
            )
        )

    if report.get("models"):
        lines.append("")
        lines.append(style.bold("By model"))
        lines.append(_indent(_usage_table(report["models"], style, "MODEL")))

    if report.get("groups"):
        grouping = str(report.get("group_by", ""))
        lines.append("")
        lines.append(style.bold(f"By {grouping}"))
        if notes and report.get("group_note"):
            lines.append(style.dim(f"  {report['group_note']}"))
        lines.append(_indent(_usage_table(report["groups"], style, grouping.upper())))

    return "\n".join(lines)


def _usage_table(rows: list[dict[str, Any]], style: Style, heading: str) -> str:
    table_rows = [
        [
            str(row["name"]),
            thousands(row["responses"]),
            compact_tokens(row["input_tokens"]),
            compact_tokens(row["cache_creation_1h_tokens"] + row["cache_creation_5m_tokens"]),
            compact_tokens(row["cache_read_input_tokens"]),
            compact_tokens(row["output_tokens"]),
            compact_tokens(row["total_tokens"]),
            _money(None if row.get("unpriced_responses") and not row["cost_usd"] else row["cost_usd"]),
        ]
        for row in rows
    ]
    return table(
        table_rows,
        [heading, "RESP", "INPUT", "CACHE-W", "CACHE-R", "OUTPUT", "TOTAL", "EST $"],
        style,
        right=(1, 2, 3, 4, 5, 6, 7),
    )


def render_plan(report: dict[str, Any], style: Style, *, notes: bool = True) -> str:
    plan = report.get("plan") or {}
    notes_map = plan.get("notes") or {}
    lines = [style.bold("Claude plan and account")]
    lines.append("")

    fields = (
        ("email", "email"),
        ("organization", "organization_name"),
        ("plan", "organization_type"),
        ("rate-limit tier", "rate_limit_tier"),
        ("role", "organization_role"),
        ("billing", "billing_type"),
        ("extra usage", "extra_usage_enabled"),
        ("first token", "first_token_date"),
        ("subscribed", "subscription_started"),
    )
    shown = [(label, key) for label, key in fields if plan.get(key) is not None]
    if shown:
        width = max(len(label) for label, _ in shown)
        for label, key in shown:
            value = plan[key]
            if isinstance(value, bool):
                value = "enabled" if value else "disabled"
            elif key.endswith(("_date", "_started")) and isinstance(value, str):
                value = value[:10]
            lines.append(
                f"  {label:<{width}}  {value}" + (_explain(style, notes_map.get(key, "")) if notes else "")
            )

    windows = report.get("windows") or []
    if windows:
        lines.append("")
        lines.append(style.bold("Live rate-limit windows"))
        if notes:
            lines.append(style.dim("  share of the quota consumed in each rolling window"))
        label_width = max(len(w["label"]) for w in windows)
        for window in windows:
            percent = float(window["utilization"])
            painted = style.for_utilization(percent, f"{bar(percent)} {percent:5.1f}%")
            reset = humanize_reset(window.get("resets_at"))
            suffix = style.dim(f"  resets {reset}") if reset else ""
            lines.append(f"  {window['label']:<{label_width}}  {painted}{suffix}")

    extra = report.get("extra_usage")
    if extra:
        used, limit = extra.get("used"), extra.get("limit")
        currency = extra.get("currency", "USD")
        percent = extra.get("utilization")
        if percent is None and used is not None and limit:
            percent = used / limit * 100
        lines.append("")
        lines.append(style.bold("Extra usage (pay-as-you-go)"))
        if notes:
            lines.append(style.dim("  billed on top of the subscription once a window is exhausted"))
        detail = f"  {used:,.2f} / {limit:,.2f} {currency}" if used is not None and limit is not None else ""
        if percent is not None:
            painted = style.for_utilization(percent, f"{bar(percent)} {percent:5.1f}%")
            lines.append(f"  monthly credits  {painted}{style.dim(detail)}")
        elif detail:
            lines.append(f"  monthly credits {detail}")

    cached = report.get("cached_utilization")
    if cached and cached.get("age_seconds") is not None:
        hours = cached["age_seconds"] / 3600
        lines.append("")
        lines.append(
            style.dim(
                f"  Claude Code's own cached copy of these windows is {hours:.1f}h old "
                "— shown above are live values"
            )
        )

    counters = report.get("counters") or {}
    counter_notes = counters.get("notes") or {}
    for key, heading in (("tools", "Most-used tools"), ("skills", "Most-used skills")):
        rows = counters.get(key) or []
        if not rows:
            continue
        lines.append("")
        lines.append(style.bold(heading))
        if notes and counter_notes.get(key):
            lines.append(style.dim(f"  {counter_notes[key]}"))
        top = rows[:8]
        width = max(len(str(row["name"])) for row in top)
        for row in top:
            lines.append(f"  {str(row['name']):<{width}}  {row['count']:>6,}")

    stats = report.get("stats")
    if stats:
        lines.append("")
        lines.append(
            style.dim(
                f"  Claude Code's activity rollup: {stats['total_sessions']:,} sessions, "
                f"{stats['total_messages']:,} messages (last computed {stats['last_computed']}) "
                "— corroboration only, often stale"
            )
        )

    return "\n".join(lines)


def render_telemetry(report: dict[str, Any], style: Style, *, notes: bool = True) -> str:
    lines = [style.bold("Claude Code emitted telemetry (OpenTelemetry)")]

    if not report.get("captured"):
        lines.append("")
        lines.append("  Nothing captured yet.")
        lines.append("")
        lines.append(style.dim("  Claude Code only emits telemetry when it is told to. Run:"))
        lines.append("")
        lines.append("      tokencheck collect --print-env    # then eval those exports")
        lines.append("      tokencheck collect                # leave this running")
        lines.append("")
        lines.append(
            style.dim(
                "  Then start Claude Code in a shell carrying those exports. This channel "
                "is the only source of tool decisions, API errors, lines-of-code, commits "
                "and active time — none of which appear in transcripts."
            )
        )
        return "\n".join(lines)

    window = report.get("range") or {}
    first, last = (window.get("first") or "")[:16], (window.get("last") or "")[:16]
    lines.append(style.dim(f"  {first} → {last} · {report.get('sessions', 0)} sessions · {report.get('store')}"))
    lines.append("")

    metrics = report.get("metrics") or []
    if metrics:
        lines.append(style.bold("Metrics"))
        width = max(len(m["name"]) for m in metrics)
        for metric in metrics:
            value = metric["value"]
            shown = f"{value:,.2f}" if value % 1 else f"{int(value):,}"
            unit = f" {metric['unit']}" if metric.get("unit") and metric["unit"] != "none" else ""
            row = f"  {metric['name']:<{width}}  {shown:>12}{unit}"
            lines.append(row + (_explain(style, metric.get("note", "")) if notes else ""))

    for title, key, note in (
        ("Tokens by type", "tokens_by_type", "input / output / cacheRead / cacheCreation as Claude Code counts them"),
        ("Tokens by model", "tokens_by_model", "which model consumed them"),
        ("Lines of code", "lines_of_code", "added vs removed by edit tools"),
        ("Permission decisions", "decisions", "how often you accepted or rejected a tool action"),
    ):
        block = report.get(key) or {}
        if not block:
            continue
        lines.append("")
        lines.append(style.bold(title))
        if notes and note:
            lines.append(style.dim(f"  {note}"))
        width = max(len(str(name)) for name in block)
        for name, value in sorted(block.items(), key=lambda kv: -kv[1]):
            shown = f"{value:,.2f}" if value % 1 else f"{int(value):,}"
            lines.append(f"  {str(name):<{width}}  {shown:>14}")

    if report.get("cost_usd"):
        lines.append("")
        lines.append(
            f"  {style.bold('cost reported by Claude Code')}  ${report['cost_usd']:,.4f}"
            + (_explain(style, "Claude Code's own cost attribution, not TokenCheck's estimate") if notes else "")
        )

    events = report.get("events") or []
    if events:
        lines.append("")
        lines.append(style.bold("Events"))
        rows = [[e["name"], f"{e['count']:,}", style.dim(e.get("note", ""))] for e in events]
        lines.append(_indent(table(rows, ["EVENT", "COUNT", "MEANING"], style, right=(1,))))

    if report.get("api_error_count"):
        lines.append("")
        lines.append(style.bold(f"API errors ({report['api_error_count']:,})"))
        rows = [
            [
                str(e.get("model") or "—"),
                str(e.get("status_code") or "—"),
                str(e.get("attempt") or "—"),
                str(e.get("error") or "")[:60],
            ]
            for e in report.get("api_errors") or []
        ]
        lines.append(_indent(table(rows, ["MODEL", "STATUS", "ATTEMPT", "ERROR"], style, right=(1, 2))))

    return "\n".join(lines)
