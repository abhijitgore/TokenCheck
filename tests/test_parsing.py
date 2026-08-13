"""Parser tests, using recorded-shape fixtures.

The Admin API path cannot be exercised live without an `sk-ant-admin…` key, so
its bucket-merging and unit conversion are pinned here instead.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokencheck import api, auth, claudeconfig, otlp, pricing, render, transcripts  # noqa: E402


class OAuthUsageParsing(unittest.TestCase):
    def test_parses_flat_windows(self):
        payload = {
            "five_hour": {"utilization": 1.0, "resets_at": "2026-08-13T04:30:00Z"},
            "seven_day": {"utilization": 14.0, "resets_at": "2026-08-13T22:00:00Z"},
            "seven_day_opus": {"utilization": 3.5, "resets_at": "2026-08-13T22:00:00Z"},
        }
        windows = api.parse_limit_windows(payload)
        self.assertEqual(
            [(w["label"], w["utilization"]) for w in windows],
            [("5-hour session", 1.0), ("7-day (all models)", 14.0), ("7-day Opus", 3.5)],
        )

    def test_parses_newer_limits_array_with_model_scope(self):
        payload = {
            "limits": [
                {
                    "kind": "weekly_scoped",
                    "group": "weekly",
                    "percent": 17.0,
                    "resets_at": "2026-08-13T22:00:00Z",
                    "scope": {"model": {"display_name": "Fable"}},
                    "is_active": True,
                },
                {"kind": "weekly_scoped", "percent": 99.0, "is_active": False},
            ]
        }
        windows = api.parse_limit_windows(payload)
        self.assertEqual(len(windows), 1, "inactive limits must be dropped")
        self.assertEqual(windows[0]["label"], "weekly Fable")
        self.assertEqual(windows[0]["utilization"], 17.0)

    def test_flat_and_array_shapes_do_not_duplicate(self):
        payload = {
            "five_hour": {"utilization": 2.0, "resets_at": None},
            "limits": [{"kind": "weekly", "group": "weekly", "percent": 5.0, "is_active": True}],
        }
        labels = [w["label"] for w in api.parse_limit_windows(payload)]
        self.assertEqual(labels, ["5-hour session", "weekly"])

    def test_extra_usage_only_when_enabled(self):
        self.assertIsNone(api.parse_extra_usage({"extra_usage": {"is_enabled": False}}))
        extra = api.parse_extra_usage(
            {"extra_usage": {"is_enabled": True, "used_credits": 12.5, "monthly_limit": 1000}}
        )
        self.assertEqual(extra["used"], 12.5)
        self.assertEqual(extra["limit"], 1000.0)


class AdminUsageMerging(unittest.TestCase):
    now = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)

    messages = [
        {
            "starting_at": "2026-08-11T00:00:00Z",
            "ending_at": "2026-08-12T00:00:00Z",
            "results": [
                {
                    "uncached_input_tokens": 1000,
                    "cache_creation": {
                        "ephemeral_1h_input_tokens": 200,
                        "ephemeral_5m_input_tokens": 300,
                    },
                    "cache_read_input_tokens": 5000,
                    "output_tokens": 400,
                    "model": "claude-opus-5",
                },
                {
                    "uncached_input_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 5,
                    "model": "claude-haiku-4-5",
                },
            ],
        },
        {
            "starting_at": "2026-08-12T00:00:00Z",
            "ending_at": "2026-08-13T00:00:00Z",
            "results": [
                {
                    "uncached_input_tokens": 7,
                    "cache_read_input_tokens": 1,
                    "output_tokens": 2,
                    "model": "claude-opus-5",
                }
            ],
        },
    ]

    costs = [
        {
            "starting_at": "2026-08-11T00:00:00Z",
            "ending_at": "2026-08-12T00:00:00Z",
            # `amount` is a decimal string in the currency's lowest unit.
            "results": [{"amount": "250", "currency": "USD", "description": "Claude Opus 5 input"}],
        }
    ]

    def merged(self):
        return api._merge_admin_buckets(self.messages, self.costs, now=self.now)

    def test_totals(self):
        totals = self.merged()["totals"]
        self.assertEqual(totals["input_tokens"], 1017)
        self.assertEqual(totals["cache_creation_input_tokens"], 500)
        self.assertEqual(totals["cache_read_input_tokens"], 5001)
        self.assertEqual(totals["output_tokens"], 407)
        self.assertEqual(totals["total_tokens"], 1017 + 500 + 5001 + 407)

    def test_cost_is_converted_from_cents(self):
        self.assertAlmostEqual(self.merged()["totals"]["cost_usd"], 2.50)

    def test_models_are_aggregated_across_days_and_sorted(self):
        models = self.merged()["models"]
        self.assertEqual([m["name"] for m in models], ["claude-opus-5", "claude-haiku-4-5"])
        self.assertEqual(models[0]["output_tokens"], 402)

    def test_future_buckets_are_dropped(self):
        future = [{"starting_at": "2026-09-01T00:00:00Z", "ending_at": "2026-09-02T00:00:00Z", "results": []}]
        days = api._merge_admin_buckets(self.messages + future, [], now=self.now)["daily"]
        self.assertEqual([d["day"] for d in days], ["2026-08-11", "2026-08-12"])

    def test_daily_range_is_inclusive_of_today(self):
        start, end = api._daily_range(7, self.now)
        self.assertEqual(start.isoformat(), "2026-08-06T00:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-08-13T00:00:00+00:00")

    def test_report_is_json_serializable(self):
        json.dumps(self.merged(), default=str)


class CredentialParsing(unittest.TestCase):
    def test_nested_claude_code_shape(self):
        blob = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "  token-value  ",
                    "expiresAt": 4102444800000,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        )
        credential = auth._parse_credentials_blob(blob, source="test")
        self.assertEqual(credential.access_token, "token-value")
        self.assertEqual(credential.subscription_type, "max")
        self.assertFalse(credential.is_expired)

    def test_expiry_in_the_past(self):
        blob = json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000000000000}})
        self.assertTrue(auth._parse_credentials_blob(blob, source="test").is_expired)

    def test_rejects_junk(self):
        self.assertIsNone(auth._parse_credentials_blob("not json", source="test"))
        self.assertIsNone(auth._parse_credentials_blob("{}", source="test"))


class Rendering(unittest.TestCase):
    def test_bar_endpoints(self):
        self.assertEqual(render.bar(0, 4), "░░░░")
        self.assertEqual(render.bar(100, 4), "████")
        self.assertEqual(render.bar(150, 4), "████", "over-100 clamps rather than overflowing")

    def test_humanize_reset(self):
        now = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)
        self.assertTrue(render.humanize_reset("2026-08-12T14:30:00Z", now=now).startswith("in 2h"))
        self.assertTrue(render.humanize_reset("2026-08-12T11:00:00Z", now=now).startswith("due now"))
        self.assertEqual(render.humanize_reset(None), "")

    def test_visible_len_ignores_ansi(self):
        self.assertEqual(render._visible_len("\033[32mok\033[0m"), 2)


def _assistant_row(message_id: str, *, model="claude-opus-5", output=100, one_hour=0,
                   five_minute=0, cache_read=0, inputs=10, **extra):
    """One transcript row in the shape Claude Code writes."""
    row = {
        "type": "assistant",
        "requestId": f"req_{message_id}",
        "sessionId": "sess-1",
        "timestamp": "2026-08-12T10:00:00.000Z",
        "cwd": "/Users/x/Projects/Demo",
        "version": "2.1.229",
        "message": {
            "id": message_id,
            "model": model,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": inputs,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": one_hour,
                    "ephemeral_5m_input_tokens": five_minute,
                },
            },
        },
    }
    row.update(extra)
    return row


class TranscriptParsing(unittest.TestCase):
    """The transcripts are the primary local telemetry source — and the easiest
    to read wrong, because one API response is written as several rows."""

    def _write(self, rows, name="sess.jsonl", subdir=None):
        root = Path(self.tmp.name) / "projects" / "-Users-x-Projects-Demo"
        if subdir:
            root = root / subdir
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_repeated_rows_for_one_response_are_counted_once(self):
        # Claude Code writes one row per content block, each repeating usage.
        rows = [_assistant_row("msg_1", output=500, cache_read=1000)] * 6
        records = transcripts.collect_usage([self._write(rows)])
        self.assertEqual(len(records), 1, "6 rows are one API response")
        self.assertEqual(records[0]["output_tokens"], 500)

    def test_same_response_across_two_files_is_counted_once(self):
        # A resumed or forked session re-copies rows into a second file.
        row = _assistant_row("msg_dup", output=42)
        first = self._write([row], name="a.jsonl")
        second = self._write([row], name="b.jsonl")
        records = transcripts.collect_usage([first, second])
        self.assertEqual(len(records), 1)

    def test_largest_row_wins_for_a_partially_flushed_response(self):
        rows = [
            _assistant_row("msg_1", output=10),
            _assistant_row("msg_1", output=900),
            _assistant_row("msg_1", output=120),
        ]
        records = transcripts.collect_usage([self._write(rows)])
        self.assertEqual(records[0]["output_tokens"], 900)

    def test_synthetic_rows_are_skipped(self):
        rows = [_assistant_row("msg_1"), _assistant_row("msg_2", model="<synthetic>")]
        self.assertEqual(len(transcripts.collect_usage([self._write(rows)])), 1)

    def test_cache_creation_is_split_by_ttl(self):
        rows = [_assistant_row("msg_1", one_hour=800, five_minute=200)]
        record = transcripts.collect_usage([self._write(rows)])[0]
        self.assertEqual(record["cache_creation_1h_tokens"], 800)
        self.assertEqual(record["cache_creation_5m_tokens"], 200)

    def test_legacy_flat_cache_field_counts_as_five_minute(self):
        row = _assistant_row("msg_1")
        del row["message"]["usage"]["cache_creation"]
        row["message"]["usage"]["cache_creation_input_tokens"] = 700
        record = transcripts.collect_usage([self._write([row])])[0]
        self.assertEqual(record["cache_creation_5m_tokens"], 700)
        self.assertEqual(record["cache_creation_1h_tokens"], 0)

    def test_subagent_transcripts_are_found_and_flagged(self):
        main = self._write([_assistant_row("msg_main")])
        self._write([_assistant_row("msg_sub", model="claude-fable-5")],
                    name="agent-1.jsonl", subdir="sess-uuid/subagents")
        records = transcripts.collect_usage(
            transcripts.transcript_files(Path(self.tmp.name))
        )
        self.assertEqual(len(records), 2, "nested subagent transcripts must be scanned")
        self.assertTrue(any(r["is_subagent"] for r in records))
        self.assertIn(main.name, [p.name for p in transcript_paths(self.tmp.name)])

    def test_truncated_final_line_is_tolerated(self):
        path = self._write([_assistant_row("msg_1")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"type":"assistant","message":{"id":"msg_2"')  # live write
        self.assertEqual(len(transcripts.collect_usage([path])), 1)

    def test_summary_totals_match_the_sum_of_groups(self):
        rows = [
            _assistant_row("msg_1", output=100, cache_read=50),
            _assistant_row("msg_2", output=200, cache_read=70, model="claude-sonnet-5"),
        ]
        report = transcripts.summarize(transcripts.collect_usage([self._write(rows)]), group_by="model")
        self.assertEqual(report["totals"]["output_tokens"], 300)
        self.assertEqual(
            sum(g["output_tokens"] for g in report["groups"]), report["totals"]["output_tokens"]
        )

    def test_every_reported_field_has_an_explanation(self):
        report = transcripts.summarize(
            transcripts.collect_usage([self._write([_assistant_row("msg_1")])])
        )
        for field in transcripts.TOKEN_FIELDS:
            self.assertTrue(report["field_notes"].get(field), f"{field} needs a gloss")


def transcript_paths(root: str) -> list[Path]:
    return transcripts.transcript_files(Path(root))


class Pricing(unittest.TestCase):
    def test_unknown_model_is_never_guessed(self):
        self.assertIsNone(pricing.estimate_cost("claude-something-unreleased-9", {"output_tokens": 1000}))
        self.assertIsNone(pricing.rates_for("<synthetic>"))

    def test_context_suffix_still_prices(self):
        self.assertEqual(pricing.rates_for("claude-opus-5[1m]"), (5.0, 25.0))

    def test_longest_prefix_wins(self):
        self.assertEqual(pricing.rates_for("claude-opus-4-1"), (15.0, 75.0))
        self.assertEqual(pricing.rates_for("claude-opus-4-8"), (5.0, 25.0))

    def test_cache_ttls_are_priced_differently(self):
        one_hour = pricing.estimate_cost("claude-opus-5", {"cache_creation_1h_tokens": 1_000_000})
        five_min = pricing.estimate_cost("claude-opus-5", {"cache_creation_5m_tokens": 1_000_000})
        self.assertAlmostEqual(one_hour, 10.0)   # 2.00x the $5 input rate
        self.assertAlmostEqual(five_min, 6.25)   # 1.25x
        self.assertGreater(one_hour, five_min, "a 1h write must not be priced as a 5m write")

    def test_cache_read_is_a_tenth_of_input(self):
        self.assertAlmostEqual(
            pricing.estimate_cost("claude-opus-5", {"cache_read_input_tokens": 1_000_000}), 0.5
        )


class OTLPParsing(unittest.TestCase):
    def test_attribute_type_wrappers_are_unwrapped(self):
        attrs = otlp.attributes(
            [
                {"key": "model", "value": {"stringValue": "claude-opus-5"}},
                {"key": "tokens", "value": {"intValue": "1500"}},
                {"key": "cost", "value": {"doubleValue": 0.25}},
                {"key": "ok", "value": {"boolValue": True}},
            ]
        )
        self.assertEqual(attrs, {"model": "claude-opus-5", "tokens": 1500, "cost": 0.25, "ok": True})

    def test_metric_points_carry_resource_and_point_attributes(self):
        payload = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "unit": "tokens",
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "asInt": "1500",
                                                "attributes": [{"key": "type", "value": {"stringValue": "input"}}],
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        points = list(otlp.flatten_metrics(payload))
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["value"], 1500.0)
        self.assertEqual(points[0]["attributes"]["service.name"], "claude-code")
        self.assertEqual(points[0]["attributes"]["type"], "input")

    def test_event_names_normalize_whether_or_not_they_are_prefixed(self):
        # The docs spell events `claude_code.api_error`; the wire sends `api_error`.
        self.assertEqual(otlp.event_name("claude_code.api_error"), "api_error")
        self.assertEqual(otlp.event_name("api_error"), "api_error")

    def test_logs_flatten_to_named_events(self):
        payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "attributes": [
                                        {"key": "event.name", "value": {"stringValue": "claude_code.api_error"}},
                                        {"key": "status_code", "value": {"intValue": "529"}},
                                    ]
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        events = list(otlp.flatten_logs(payload))
        self.assertEqual(events[0]["name"], "api_error")
        self.assertEqual(events[0]["attributes"]["status_code"], 529)

    def test_summary_of_an_empty_store_reports_not_captured(self):
        self.assertFalse(otlp.summarize([])["captured"])

    def _metrics_payload(self, value, temporality):
        return {
            "resourceMetrics": [
                {
                    "resource": {"attributes": []},
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {
                                        "aggregationTemporality": temporality,
                                        "dataPoints": [
                                            {
                                                "asInt": str(value),
                                                "startTimeUnixNano": "100",
                                                "attributes": [
                                                    {"key": "type", "value": {"stringValue": "input"}}
                                                ],
                                            }
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        }

    def _store(self, payloads):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "otlp-2026-08-13.ndjson"
        with path.open("w", encoding="utf-8") as handle:
            for payload in payloads:
                handle.write(
                    json.dumps(
                        {"received_at": "2026-08-13T10:00:00+00:00", "signal": "metrics", "payload": payload}
                    )
                    + "\n"
                )
        return [path]

    def test_delta_exports_are_summed(self):
        # Each delta export carries only the increment since the last one.
        store = self._store(
            [self._metrics_payload(100, otlp.TEMPORALITY_DELTA),
             self._metrics_payload(150, otlp.TEMPORALITY_DELTA)]
        )
        report = otlp.summarize(store)
        self.assertEqual(report["tokens_by_type"]["input"], 250)
        self.assertFalse(report["cumulative_temporality"])

    def test_cumulative_exports_are_not_double_counted(self):
        # Each cumulative export repeats the running total; summing them would
        # report 100+250+400 = 750 instead of the true 400.
        store = self._store(
            [self._metrics_payload(v, otlp.TEMPORALITY_CUMULATIVE) for v in (100, 250, 400)]
        )
        report = otlp.summarize(store)
        self.assertEqual(report["tokens_by_type"]["input"], 400)
        self.assertTrue(report["cumulative_temporality"])

    def test_missing_temporality_is_treated_as_delta(self):
        payload = self._metrics_payload(100, otlp.TEMPORALITY_DELTA)
        del payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["sum"]["aggregationTemporality"]
        report = otlp.summarize(self._store([payload, payload]))
        self.assertEqual(report["tokens_by_type"]["input"], 200)

    def test_every_documented_metric_and_event_is_explained(self):
        for name in otlp.METRIC_NOTES.values():
            self.assertTrue(name.strip())
        for name in otlp.EVENT_NOTES:
            self.assertFalse(name.startswith(otlp.EVENT_PREFIX), "notes are keyed bare")


class ClaudeConfigParsing(unittest.TestCase):
    def test_plan_fields_are_extracted(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(
                {
                    "oauthAccount": {
                        "emailAddress": "a@b.c",
                        "organizationType": "claude_max",
                        "organizationRateLimitTier": "default_claude_max_20x",
                        "hasExtraUsageEnabled": True,
                    },
                    "claudeCodeFirstTokenDate": "2025-06-26T02:49:28Z",
                },
                handle,
            )
            path = Path(handle.name)
        self.addCleanup(path.unlink)
        plan = claudeconfig.read_plan(path)
        self.assertEqual(plan["organization_type"], "claude_max")
        self.assertEqual(plan["rate_limit_tier"], "default_claude_max_20x")
        self.assertTrue(plan["extra_usage_enabled"])

    def test_missing_file_yields_empty_plan_rather_than_raising(self):
        plan = claudeconfig.read_plan(Path("/nonexistent/claude.json"))
        self.assertIsNone(plan["email"])


if __name__ == "__main__":
    unittest.main()
