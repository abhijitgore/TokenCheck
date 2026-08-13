"""A minimal OTLP/HTTP receiver for the telemetry Claude Code *emits*.

Transcripts record what the API charged for. They say nothing about what
happened around those calls: which tool permissions you accepted or rejected,
which API requests errored and how often they retried, how many lines of code
were written, how many commits and PRs came out of it, how long you were
actively working. Claude Code emits all of that over OpenTelemetry when
``CLAUDE_CODE_ENABLE_TELEMETRY=1`` — but only if something is listening.

This module is that listener: an ``http.server`` accepting OTLP/HTTP requests on
localhost and appending them to a newline-delimited JSON log.

**http/json only.** The OTLP default is protobuf over gRPC, which is not
reachable from the standard library. Claude Code speaks ``http/json`` when told
to, so :func:`env_exports` sets ``OTEL_EXPORTER_OTLP_PROTOCOL=http/json``. A
protobuf request is rejected with a clear message rather than being silently
mis-parsed.

Nothing here enables ``OTEL_LOG_USER_PROMPTS``, ``OTEL_LOG_ASSISTANT_RESPONSES``
or ``OTEL_LOG_RAW_API_BODIES`` — TokenCheck captures counters, not conversation
content.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from .util import as_float, as_int, parse_iso

DEFAULT_PORT = 4318
DEFAULT_HOST = "127.0.0.1"
MAX_BODY_BYTES = 32 * 1024 * 1024

SIGNAL_PATHS = {
    "/v1/metrics": "metrics",
    "/v1/logs": "logs",
    "/v1/traces": "traces",
}

# What each Claude Code metric actually counts, shown beside the numbers.
METRIC_NOTES: dict[str, str] = {
    "claude_code.session.count": "CLI sessions started",
    "claude_code.token.usage": "tokens consumed, split by type",
    "claude_code.cost.usage": "cost Claude Code itself attributes to the session (USD)",
    "claude_code.lines_of_code.count": "lines added/removed by edit tools",
    "claude_code.commit.count": "git commits created",
    "claude_code.pull_request.count": "pull requests created",
    "claude_code.code_edit_tool.decision": "edit-permission prompts, accepted vs rejected",
    "claude_code.active_time.total": "seconds actively working (not wall clock)",
}

# Keyed without the `claude_code.` prefix: the documentation writes event names
# fully qualified, but the `event.name` attribute Claude Code actually emits is
# bare (`api_request`, not `claude_code.api_request`). :func:`event_name`
# normalizes both spellings onto these keys.
EVENT_NOTES: dict[str, str] = {
    "user_prompt": "a prompt was submitted (length only, no text)",
    "assistant_response": "a response was produced",
    "api_request": "one API call, with per-call tokens and cost",
    "api_error": "an API call failed — status, attempt number, duration",
    "api_refusal": "a safety classifier declined the request",
    "tool_result": "a tool ran — success, duration, error type",
    "tool_decision": "a permission prompt was accepted or rejected",
    "permission_mode_changed": "permission mode switched",
    "auth": "a login/credential event",
    "mcp_server_connection": "an MCP server connected or failed to",
    "internal_error": "Claude Code hit an internal error",
    "plugin_installed": "a plugin was installed",
    "plugin_loaded": "a plugin was loaded",
}

EVENT_PREFIX = "claude_code."


def event_name(raw: Any) -> str:
    """Normalize an event name to its bare form."""
    name = str(raw or "event")
    return name[len(EVENT_PREFIX) :] if name.startswith(EVENT_PREFIX) else name


def data_dir() -> Path:
    """Where captured telemetry is stored (override: ``$TOKENCHECK_DATA_DIR``)."""
    raw = os.environ.get("TOKENCHECK_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "tokencheck"


def store_path(moment: dt.datetime | None = None) -> Path:
    """One file per UTC day, so the log rotates without a size check."""
    moment = moment or dt.datetime.now(dt.timezone.utc)
    return data_dir() / f"otlp-{moment.astimezone(dt.timezone.utc):%Y-%m-%d}.ndjson"


def store_files() -> list[Path]:
    directory = data_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("otlp-*.ndjson"))


def env_exports(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> list[str]:
    """The exports Claude Code needs in order to emit to this receiver."""
    endpoint = f"http://{host}:{port}"
    return [
        "export CLAUDE_CODE_ENABLE_TELEMETRY=1",
        "export OTEL_METRICS_EXPORTER=otlp",
        "export OTEL_LOGS_EXPORTER=otlp",
        "export OTEL_EXPORTER_OTLP_PROTOCOL=http/json",
        f"export OTEL_EXPORTER_OTLP_ENDPOINT={endpoint}",
        # Delta means each export carries only the increment since the last one,
        # so appending batches and summing them is correct. Under `cumulative`
        # every export repeats the running total; TokenCheck detects and handles
        # that too (see `summarize`), but pinning delta keeps it simple.
        "export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta",
        # Default is 60s for metrics; shorten so a short session still reports.
        "export OTEL_METRIC_EXPORT_INTERVAL=10000",
        "export OTEL_LOGS_EXPORT_INTERVAL=5000",
    ]


# OTLP aggregation temporality enum.
TEMPORALITY_DELTA = 1
TEMPORALITY_CUMULATIVE = 2


# --------------------------------------------------------------------------
# Receiver
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TokenCheck-OTLP"

    # Set by serve().
    on_payload = staticmethod(lambda signal, payload: None)
    verbose = False

    def do_POST(self) -> None:  # noqa: N802 - http.server's required spelling
        signal = SIGNAL_PATHS.get(self.path.split("?", 1)[0])
        if signal is None:
            self._reply(404, {"error": f"unknown OTLP path {self.path}"})
            return

        length = as_int(self.headers.get("Content-Length"))
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(400, {"error": "missing or oversized body"})
            return
        body = self.rfile.read(length)

        if "gzip" in (self.headers.get("Content-Encoding") or "").lower():
            try:
                body = gzip.decompress(body)
            except OSError:
                self._reply(400, {"error": "malformed gzip body"})
                return

        content_type = (self.headers.get("Content-Type") or "").lower()
        if "protobuf" in content_type:
            self._reply(
                415,
                {
                    "error": "TokenCheck speaks OTLP http/json only; "
                    "set OTEL_EXPORTER_OTLP_PROTOCOL=http/json"
                },
            )
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._reply(400, {"error": "body was not valid JSON"})
            return

        try:
            self.on_payload(signal, payload)
        except OSError as error:
            self._reply(500, {"error": f"could not persist: {error}"})
            return

        # An OTLP client expects a (possibly empty) partial-success envelope.
        self._reply(200, {"partialSuccess": {}})

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.verbose:
            super().log_message(fmt, *args)


def append(signal: str, payload: Any, *, now: dt.datetime | None = None) -> Path:
    """Append one received payload to today's NDJSON file."""
    now = now or dt.datetime.now(dt.timezone.utc)
    path = store_path(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"received_at": now.isoformat(), "signal": signal, "payload": payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    verbose: bool = False,
    on_receive=None,
) -> ThreadingHTTPServer:
    """Build the receiver. Caller runs ``serve_forever()``.

    Bound to loopback by design: this accepts unauthenticated writes, and the
    only intended client is Claude Code on the same machine.
    """

    def handle(signal: str, payload: Any) -> None:
        path = append(signal, payload)
        if on_receive is not None:
            on_receive(signal, payload, path)

    handler = type("_BoundHandler", (_Handler,), {"on_payload": staticmethod(handle), "verbose": verbose})
    return ThreadingHTTPServer((host, port), handler)


# --------------------------------------------------------------------------
# Parsing — OTLP/JSON is deeply nested and tags every scalar with its type
# --------------------------------------------------------------------------


def _attr_value(value: Any) -> Any:
    """Unwrap ``{"stringValue": "x"}`` and friends into a plain Python value."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "boolValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        return as_int(value["intValue"])
    if "doubleValue" in value:
        return as_float(value["doubleValue"])
    if "arrayValue" in value:
        values = (value["arrayValue"] or {}).get("values") or []
        return [_attr_value(item) for item in values]
    return None


def attributes(items: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        if isinstance(item, dict) and "key" in item:
            out[str(item["key"])] = _attr_value(item.get("value"))
    return out


def _point_value(point: dict[str, Any]) -> float:
    if "asInt" in point:
        return float(as_int(point["asInt"]))
    if "asDouble" in point:
        return as_float(point["asDouble"]) or 0.0
    # Histograms carry a sum instead of a single value.
    return as_float(point.get("sum")) or 0.0


def flatten_metrics(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield one flat dict per metric data point."""
    for resource in payload.get("resourceMetrics") or []:
        resource_attrs = attributes((resource.get("resource") or {}).get("attributes"))
        for scope in resource.get("scopeMetrics") or []:
            for metric in scope.get("metrics") or []:
                name = metric.get("name")
                body = metric.get("sum") or metric.get("gauge") or metric.get("histogram") or {}
                temporality = as_int(body.get("aggregationTemporality")) or TEMPORALITY_DELTA
                for point in body.get("dataPoints") or []:
                    if not isinstance(point, dict):
                        continue
                    yield {
                        "name": name,
                        "unit": metric.get("unit"),
                        "value": _point_value(point),
                        "attributes": {**resource_attrs, **attributes(point.get("attributes"))},
                        "time_unix_nano": point.get("timeUnixNano"),
                        "start_time_unix_nano": point.get("startTimeUnixNano"),
                        "temporality": temporality,
                    }


def flatten_logs(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield one flat dict per log record (Claude Code's events)."""
    for resource in payload.get("resourceLogs") or []:
        resource_attrs = attributes((resource.get("resource") or {}).get("attributes"))
        for scope in resource.get("scopeLogs") or []:
            for record in scope.get("logRecords") or []:
                if not isinstance(record, dict):
                    continue
                attrs = {**resource_attrs, **attributes(record.get("attributes"))}
                raw = attrs.get("event.name") or _attr_value(record.get("body")) or "event"
                yield {
                    "name": event_name(raw),
                    "severity": record.get("severityText"),
                    "attributes": attrs,
                    "time_unix_nano": record.get("timeUnixNano"),
                }


def _iter_stored(paths: list[Path] | None = None) -> Iterator[dict[str, Any]]:
    for path in paths if paths is not None else store_files():
        try:
            handle = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record


def _series_key(point: dict[str, Any]) -> tuple:
    """Identity of one metric time series: name + attributes + stream start."""
    attrs = tuple(sorted((k, str(v)) for k, v in point["attributes"].items()))
    return (point["name"], attrs, str(point.get("start_time_unix_nano")))


def summarize(paths: list[Path] | None = None, *, since: dt.datetime | None = None) -> dict[str, Any]:
    """Aggregate everything captured so far into a report.

    Delta points are summed; cumulative points are *not*. Under cumulative
    temporality every export repeats the running total for a series, so summing
    the batches would multiply the figure by the number of exports — the same
    double-counting trap the transcript parser avoids. For those, the largest
    value seen per series is the total.
    """
    metrics: dict[str, dict[str, Any]] = {}
    cumulative: dict[tuple, float] = {}
    saw_cumulative = False
    events: dict[str, int] = defaultdict(int)
    tokens_by_type: dict[str, float] = defaultdict(float)
    tokens_by_model: dict[str, float] = defaultdict(float)
    decisions: dict[str, int] = defaultdict(int)
    lines: dict[str, float] = defaultdict(float)
    errors: list[dict[str, Any]] = []
    sessions: set[str] = set()
    cost = 0.0
    received: list[str] = []

    for record in _iter_stored(paths):
        stamp = record.get("received_at")
        if since is not None:
            moment = parse_iso(stamp)
            if moment is None or moment < since:
                continue
        if isinstance(stamp, str):
            received.append(stamp)
        payload = record.get("payload") or {}
        signal = record.get("signal")

        if signal == "metrics":
            for point in flatten_metrics(payload):
                name = str(point["name"])
                bucket = metrics.setdefault(
                    name,
                    {"name": name, "unit": point.get("unit"), "value": 0.0, "points": 0,
                     "note": METRIC_NOTES.get(name, "")},
                )
                bucket["points"] += 1

                value = point["value"]
                if point.get("temporality") == TEMPORALITY_CUMULATIVE:
                    saw_cumulative = True
                    key = _series_key(point)
                    previous = cumulative.get(key, 0.0)
                    if value <= previous:
                        # Already counted this series at this height (or the
                        # counter reset); contribute only the increment.
                        continue
                    cumulative[key] = value
                    value -= previous

                bucket["value"] += value
                attrs = point["attributes"]
                if attrs.get("session.id"):
                    sessions.add(str(attrs["session.id"]))
                if name == "claude_code.token.usage":
                    tokens_by_type[str(attrs.get("type") or "unknown")] += value
                    tokens_by_model[str(attrs.get("model") or "unknown")] += value
                elif name == "claude_code.cost.usage":
                    cost += value
                elif name == "claude_code.lines_of_code.count":
                    lines[str(attrs.get("type") or "unknown")] += value
                elif name == "claude_code.code_edit_tool.decision":
                    decisions[str(attrs.get("decision") or "unknown")] += int(value)

        elif signal == "logs":
            for event in flatten_logs(payload):
                events[event["name"]] += 1
                attrs = event["attributes"]
                if attrs.get("session.id"):
                    sessions.add(str(attrs["session.id"]))
                if event["name"] == "api_error":
                    errors.append(
                        {
                            "model": attrs.get("model"),
                            "status_code": attrs.get("status_code"),
                            "attempt": attrs.get("attempt"),
                            "duration_ms": attrs.get("duration_ms"),
                            "error": attrs.get("error"),
                        }
                    )
                elif event["name"] == "tool_decision":
                    decisions[str(attrs.get("decision") or "unknown")] += 1

    return {
        "captured": bool(received),
        "range": {"first": min(received) if received else None, "last": max(received) if received else None},
        "metrics": sorted(metrics.values(), key=lambda m: m["name"]),
        "events": [
            {"name": name, "count": count, "note": EVENT_NOTES.get(name, "")}
            for name, count in sorted(events.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "tokens_by_type": dict(tokens_by_type),
        "tokens_by_model": dict(tokens_by_model),
        "lines_of_code": dict(lines),
        "decisions": dict(decisions),
        "api_errors": errors[:20],
        "api_error_count": len(errors),
        "sessions": len(sessions),
        "cost_usd": cost,
        "cumulative_temporality": saw_cumulative,
        "store": str(data_dir()),
    }
