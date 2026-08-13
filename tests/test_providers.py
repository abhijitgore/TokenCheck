"""Period parsing, and the OpenAI/Gemini provider parsers.

The Codex and Gemini fixtures are real payloads captured from those endpoints,
trimmed of identifying values. The OpenAI admin-usage path has no credential on
any dev machine here, so its bucket merging is pinned by fixture only.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokencheck import auth, gemini_api, openai_api, period, render  # noqa: E402


class PeriodParsing(unittest.TestCase):
    def test_named_choices(self):
        self.assertEqual(period.parse("1h").seconds, 3600)
        self.assertEqual(period.parse("1d").seconds, 86400)
        self.assertEqual(period.parse("30d").seconds, 30 * 86400)

    def test_default_is_one_day(self):
        self.assertEqual(period.parse(None).label, "1d")
        self.assertEqual(period.parse("").label, "1d")
        self.assertEqual(period.DEFAULT, "1d")

    def test_arbitrary_spans(self):
        self.assertEqual(period.parse("6h").seconds, 6 * 3600)
        self.assertEqual(period.parse(" 14D ").label, "14d")

    def test_rejects_junk(self):
        for bad in ("5x", "abc", "0d", "-1h", "90d"):
            with self.assertRaises(period.PeriodError, msg=bad):
                period.parse(bad)

    def test_bucket_width_switches_at_one_day(self):
        self.assertEqual(period.parse("1h").bucket_width, "1h")
        self.assertEqual(period.parse("1d").bucket_width, "1h")
        self.assertEqual(period.parse("30d").bucket_width, "1d")
        self.assertTrue(period.parse("1d").is_hourly)
        self.assertFalse(period.parse("30d").is_hourly)

    def test_bucket_limit_matches_width(self):
        self.assertEqual(period.parse("1h").bucket_limit(), 1)
        self.assertEqual(period.parse("1d").bucket_limit(), 24)
        self.assertEqual(period.parse("30d").bucket_limit(), 30)

    def test_range_is_trailing_not_calendar_aligned(self):
        now = dt.datetime(2026, 8, 13, 15, 30, tzinfo=dt.timezone.utc)
        start, end = period.parse("1d").range(now)
        self.assertEqual(end, now)
        self.assertEqual(start, dt.datetime(2026, 8, 12, 15, 30, tzinfo=dt.timezone.utc))

    def test_days_alias_still_works(self):
        self.assertEqual(period.from_args(None, 7).label, "7d")
        self.assertEqual(period.from_args("1h", 7).label, "1h", "--period wins over --days")
        self.assertEqual(period.from_args(None, None).label, "1d")

    def test_describe(self):
        self.assertEqual(period.parse("1h").describe(), "last hour")
        self.assertEqual(period.parse("1d").describe(), "last 24 hours")
        self.assertEqual(period.parse("30d").describe(), "last 30 days")


class CodexParsing(unittest.TestCase):
    # A real `wham/usage` response, with identifiers replaced.
    payload = {
        "email": "someone@example.com",
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 12.5,
                "limit_window_seconds": 604800,
                "reset_at": 1787263258,
            },
            "secondary_window": None,
        },
        "additional_rate_limits": None,
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    }

    def test_window_normalization(self):
        windows = openai_api.parse_codex_windows(self.payload)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["label"], "primary (7-day)")
        self.assertEqual(windows[0]["utilization"], 12.5)

    def test_reset_at_becomes_iso(self):
        # Codex reports unix seconds; the shared renderer wants ISO-8601 UTC.
        resets = openai_api.parse_codex_windows(self.payload)[0]["resets_at"]
        self.assertEqual(resets, "2026-08-20T22:00:58Z")

    def test_missing_reset_at_is_none_not_epoch_zero(self):
        payload = {"rate_limit": {"primary_window": {"used_percent": 1.0, "reset_at": 0}}}
        self.assertIsNone(openai_api.parse_codex_windows(payload)[0]["resets_at"])

    def test_null_secondary_window_is_skipped(self):
        self.assertNotIn("secondary", [w["key"] for w in openai_api.parse_codex_windows(self.payload)])

    def test_additional_rate_limits(self):
        payload = dict(self.payload)
        payload["additional_rate_limits"] = [
            {
                "limit_name": "code_review",
                "rate_limit": {"used_percent": 40.0, "limit_window_seconds": 3600, "reset_at": 1787263258},
            }
        ]
        labels = [w["label"] for w in openai_api.parse_codex_windows(payload)]
        self.assertIn("code_review (1-hour)", labels)

    def test_credits_hidden_when_absent(self):
        self.assertIsNone(openai_api.parse_codex_credits(self.payload))
        with_credits = {"credits": {"has_credits": True, "balance": "42", "unlimited": False}}
        self.assertEqual(openai_api.parse_codex_credits(with_credits)["balance"], "42")

    def test_report_shape_feeds_the_shared_renderer(self):
        report = openai_api.codex_limits_report(self.payload, credential_source="test")
        self.assertEqual(report["subscription_type"], "plus")
        self.assertEqual(report["account"], "someone@example.com")
        text = render.render_limits(report, render.Style(False))
        self.assertIn("ChatGPT / Codex limits", text)
        self.assertIn("plus", text)


class OpenAIOrgUsageMerging(unittest.TestCase):
    now = dt.datetime(2026, 8, 13, 12, 0, tzinfo=dt.timezone.utc)
    window = period.parse("30d")

    usage = [
        {
            "start_time": 1786924800,
            "end_time": 1787011200,
            "results": [
                {
                    "input_tokens": 1000,
                    "input_cached_tokens": 400,
                    "output_tokens": 250,
                    "num_model_requests": 12,
                    "model": "gpt-5",
                },
                {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "num_model_requests": 1,
                    "model": "gpt-5-mini",
                },
            ],
        }
    ]

    # OpenAI reports cost as {"value": <dollars>}, unlike Anthropic's cents string.
    costs = [
        {
            "start_time": 1786924800,
            "end_time": 1787011200,
            "results": [{"amount": {"value": 3.25, "currency": "usd"}, "line_item": "gpt-5 input"}],
        }
    ]

    def merged(self):
        return openai_api.merge_org_buckets(
            self.usage, self.costs, period=self.window, now=self.now
        )

    def test_totals(self):
        totals = self.merged()["totals"]
        self.assertEqual(totals["input_tokens"], 1010)
        self.assertEqual(totals["cache_read_input_tokens"], 400)
        self.assertEqual(totals["output_tokens"], 255)
        self.assertEqual(totals["total_tokens"], 1010 + 400 + 255)

    def test_cost_is_dollars_not_cents(self):
        self.assertAlmostEqual(self.merged()["totals"]["cost_usd"], 3.25)

    def test_request_count(self):
        self.assertEqual(self.merged()["requests"], 13)

    def test_models_sorted_by_total(self):
        self.assertEqual([m["name"] for m in self.merged()["models"]], ["gpt-5", "gpt-5-mini"])

    def test_no_cache_write_counter(self):
        self.assertEqual(self.merged()["totals"]["cache_creation_input_tokens"], 0)

    def test_hourly_period_flags_daily_only_cost(self):
        report = openai_api.merge_org_buckets(
            self.usage, self.costs, period=period.parse("1h"), now=self.now
        )
        self.assertTrue(report["cost_is_daily_only"])
        self.assertIn("(day so far)", render.render_usage(report, render.Style(False), show_daily=False))

    def test_report_is_json_serializable(self):
        json.dumps(self.merged(), default=str)


class GeminiParsing(unittest.TestCase):
    # A real `retrieveUserQuota` response.
    quota = {
        "buckets": [
            {
                "resetTime": "2026-08-14T22:01:34Z",
                "tokenType": "REQUESTS",
                "modelId": "gemini-2.5-pro",
                "remainingFraction": 0.25,
            },
            {
                "resetTime": "2026-08-14T22:01:34Z",
                "tokenType": "REQUESTS",
                "modelId": "gemini-2.5-flash",
                "remainingFraction": 1,
            },
        ]
    }

    def test_remaining_fraction_is_inverted_to_used_percent(self):
        windows = gemini_api.parse_quota_windows(self.quota)
        used = {w["label"]: w["utilization"] for w in windows}
        self.assertAlmostEqual(used["gemini-2.5-pro"], 75.0)
        self.assertAlmostEqual(used["gemini-2.5-flash"], 0.0)

    def test_sorted_most_used_first(self):
        self.assertEqual(gemini_api.parse_quota_windows(self.quota)[0]["label"], "gemini-2.5-pro")

    def test_out_of_range_fraction_is_clamped(self):
        odd = {"buckets": [{"modelId": "m", "remainingFraction": 1.4}, {"modelId": "n", "remainingFraction": -2}]}
        used = sorted(w["utilization"] for w in gemini_api.parse_quota_windows(odd))
        self.assertEqual(used, [0.0, 100.0])

    def test_non_request_token_type_is_labelled(self):
        payload = {"buckets": [{"modelId": "m", "tokenType": "INPUT_TOKENS", "remainingFraction": 0.5}]}
        self.assertEqual(gemini_api.parse_quota_windows(payload)[0]["label"], "m (input_tokens)")

    def test_tier_falls_back_to_default_allowed_tier(self):
        tier = gemini_api.parse_tier(
            {
                "allowedTiers": [{"id": "standard-tier", "name": "Gemini Code Assist", "isDefault": True}],
                "ineligibleTiers": [
                    {"tierId": "free-tier", "tierName": "For individuals", "reasonMessage": "unsupported client"}
                ],
            }
        )
        self.assertEqual(tier["tier_name"], "Gemini Code Assist")
        self.assertEqual(tier["ineligible"][0]["reason"], "unsupported client")

    def test_current_tier_wins_when_present(self):
        tier = gemini_api.parse_tier(
            {"currentTier": {"id": "paid", "name": "Paid"}, "allowedTiers": [{"id": "x", "name": "X"}]}
        )
        self.assertEqual(tier["tier_name"], "Paid")

    def test_missing_tier_is_not_fatal(self):
        report = gemini_api.limits_report(self.quota, None, credential_source="test")
        self.assertIsNone(report["subscription_type"])
        self.assertIn("Gemini quota", render.render_limits(report, render.Style(False)))

    def test_no_buckets_renders_without_crashing(self):
        report = gemini_api.limits_report({}, None, credential_source="test")
        self.assertIn("No rate-limit windows", render.render_limits(report, render.Style(False)))


class ProviderCredentials(unittest.TestCase):
    def test_jwt_expiry_is_read_without_verification(self):
        import base64

        claims = base64.urlsafe_b64encode(json.dumps({"exp": 1787263258}).encode()).decode().rstrip("=")
        self.assertEqual(auth._jwt_expiry(f"header.{claims}.signature"), 1787263258.0)

    def test_malformed_jwt_yields_no_expiry(self):
        for bad in ("", "notajwt", "a.b", "a.!!!.c"):
            self.assertIsNone(auth._jwt_expiry(bad))

    def test_describe_sources_covers_every_provider(self):
        providers = {row["provider"] for row in auth.describe_sources()}
        self.assertEqual(providers, {"claude", "openai", "gemini"})

    def test_describe_sources_never_leaks_a_secret(self):
        blob = json.dumps(auth.describe_sources())
        for marker in ("sk-ant", "sk-admin", "eyJ", "ya29."):
            self.assertNotIn(marker, blob)


if __name__ == "__main__":
    unittest.main()
