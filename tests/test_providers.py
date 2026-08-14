"""Period parsing, and the OpenAI provider parsers.

The Codex fixtures are real payloads captured from that endpoint,
trimmed of identifying values. The OpenAI admin-usage path has no credential on
any dev machine here, so its bucket merging is pinned by fixture only.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokencheck import auth, cli, openai_api, period, render  # noqa: E402


def setUpModule():
    """Never touch the real Keychain from tests.

    Reads can block on a macOS approval dialog, which turns a fast suite into a
    multi-minute one and makes results depend on machine state.
    """
    auth.keychain_secret.cache_clear()
    unittest.mock.patch.object(auth, "keychain_secret", lambda service: None).start()


def tearDownModule():
    unittest.mock.patch.stopall()
    auth.keychain_secret.cache_clear()


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
        self.assertEqual(providers, {"claude", "openai"})

    def test_describe_sources_never_leaks_a_secret(self):
        blob = json.dumps(auth.describe_sources())
        for marker in ("sk-ant", "sk-admin", "eyJ", "ya29."):
            self.assertNotIn(marker, blob)


if __name__ == "__main__":
    unittest.main()


class Identity(unittest.TestCase):
    codex_usage = {"email": "live@example.com", "plan_type": "pro", "account_id": "acct-live"}
    codex_claims = {
        "email": "stale@example.com",
        "plan_type": "plus",
        "account_id": "acct-stale",
        "user_id": "user-1",
        "organizations": [{"id": "org-1", "title": "Personal", "role": "owner"}],
    }

    def test_live_api_wins_over_stale_claims(self):
        profile = openai_api.codex_identity(
            self.codex_usage, self.codex_claims, credential_source="test"
        )
        # id_token claims are written at refresh time and can be months old.
        self.assertEqual(profile["email"], "live@example.com")
        self.assertEqual(profile["subscription_type"], "pro")
        self.assertEqual(profile["account_uuid"], "acct-live")

    def test_claims_supply_what_the_api_does_not(self):
        profile = openai_api.codex_identity(
            self.codex_usage, self.codex_claims, credential_source="test"
        )
        self.assertEqual(profile["organization_name"], "Personal")
        self.assertEqual(profile["organization_role"], "owner")
        self.assertEqual(profile["user_id"], "user-1")

    def test_claims_alone_still_render(self):
        profile = openai_api.codex_identity({}, self.codex_claims, credential_source="test")
        self.assertEqual(profile["email"], "stale@example.com")
        self.assertIn("ChatGPT / Codex account", render.render_whoami(profile, render.Style(False)))

    def test_identity_claims_filters_session_identifiers(self):
        import base64

        raw = {
            "email": "x@example.com",
            "sid": "session-secret",
            "jti": "token-id",
            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "organizations": []},
        }
        blob = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
        credential = auth.CodexCredential(
            access_token="a", source="t", id_token=f"h.{blob}.s"
        )
        claims = credential.identity_claims()
        self.assertEqual(claims["email"], "x@example.com")
        self.assertNotIn("sid", claims)
        self.assertNotIn("jti", claims)

    def test_no_id_token_yields_empty_claims(self):
        self.assertEqual(auth.CodexCredential(access_token="a", source="t").identity_claims(), {})

    def test_whoami_skips_absent_fields(self):
        text = render.render_whoami({"title": "T", "email": "a@b.c"}, render.Style(False))
        self.assertIn("email", text)
        self.assertNotIn("org role", text)


class SetupReport(unittest.TestCase):
    def report(self):
        from tokencheck import cli

        return cli._setup_report()

    def test_every_step_resolves_to_a_state(self):
        for step in self.report()["steps"]:
            self.assertIn("done", step)
            self.assertIn("fix", step)
            self.assertIsInstance(step["done"], bool)

    def test_expired_is_not_done(self):
        for step in self.report()["steps"]:
            if step.get("expired"):
                self.assertFalse(step["done"], f"{step['name']} expired but marked done")

    def test_renders_without_color_escapes(self):
        text = render.render_setup(self.report(), render.Style(False))
        self.assertNotIn("\033", text)
        self.assertIn("TokenCheck setup", text)

    def test_never_prints_a_secret(self):
        text = render.render_setup(self.report(), render.Style(False))
        for marker in ("eyJ", "ya29.", "gho_"):
            self.assertNotIn(marker, text)

    def test_sources_carry_a_valid_flag(self):
        for row in auth.describe_sources():
            self.assertIn("valid", row)
            if row["valid"]:
                self.assertTrue(row["available"], "valid implies available")


class BarRendering(unittest.TestCase):
    def test_width_is_constant_across_percentages(self):
        # Column alignment depends on this; partial blocks must not change it.
        for percent in (0, 0.4, 1, 4, 14, 50, 99.6, 100, 150, -5):
            self.assertEqual(render._visible_len(render.bar(percent, 24)), 24, f"at {percent}%")

    def test_low_percentages_are_distinguishable(self):
        bars = {render.bar(p, 24) for p in (0.5, 1, 2, 4)}
        self.assertEqual(len(bars), 4, "sub-cell resolution should separate low values")

    def test_section_rule_matches_title(self):
        lines = render.section("Title", render.Style(False))
        self.assertEqual(lines[0], "Title")
        self.assertTrue(set(lines[1]) == {"─"})


class KeychainAdminKeys(unittest.TestCase):
    def setUp(self):
        for name in (*auth.ADMIN_KEY_ENVS, *auth.OPENAI_ADMIN_KEY_ENVS):
            self.addCleanup(unittest.mock.patch.dict("os.environ", {}, clear=False).stop)
        self.env = unittest.mock.patch.dict(
            "os.environ",
            {k: "" for k in (*auth.ADMIN_KEY_ENVS, *auth.OPENAI_ADMIN_KEY_ENVS)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _with_keychain(self, mapping):
        patcher = unittest.mock.patch.object(
            auth, "keychain_secret", lambda service: mapping.get(service)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_keychain_supplies_anthropic_key(self):
        self._with_keychain({auth.ANTHROPIC_ADMIN_KEYCHAIN_SERVICE: "sk-ant-admin-xyz"})
        self.assertEqual(auth.find_admin_key(), "sk-ant-admin-xyz")

    def test_keychain_supplies_openai_key(self):
        self._with_keychain({auth.OPENAI_ADMIN_KEYCHAIN_SERVICE: "sk-admin-xyz"})
        self.assertEqual(auth.find_openai_admin_key(), "sk-admin-xyz")

    def test_explicit_flag_beats_keychain(self):
        self._with_keychain({auth.ANTHROPIC_ADMIN_KEYCHAIN_SERVICE: "from-keychain"})
        self.assertEqual(auth.find_admin_key("from-flag"), "from-flag")

    def test_env_beats_keychain(self):
        self._with_keychain({auth.ANTHROPIC_ADMIN_KEYCHAIN_SERVICE: "from-keychain"})
        with unittest.mock.patch.dict("os.environ", {auth.ADMIN_KEY_ENVS[0]: "from-env"}):
            self.assertEqual(auth.find_admin_key(), "from-env")

    def test_missing_everywhere_names_the_keychain_service(self):
        self._with_keychain({})
        with self.assertRaises(auth.AuthError) as caught:
            auth.find_admin_key()
        self.assertIn(auth.ANTHROPIC_ADMIN_KEYCHAIN_SERVICE, str(caught.exception))

    def test_blocked_keychain_is_distinct_from_missing(self):
        def blocked(service):
            raise auth.KeychainBlocked("approval pending")

        patcher = unittest.mock.patch.object(auth, "keychain_secret", blocked)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(auth.KeychainBlocked):
            auth.find_admin_key()
        # An inventory must still render rather than propagating the block.
        rows = [r for r in auth.describe_sources() if r["method"] == "admin"]
        self.assertTrue(any("approval pending" in str(r["detail"]) for r in rows))


class KeyKindWarning(unittest.TestCase):
    def test_regular_anthropic_key_is_flagged(self):
        warning = auth.key_kind_warning("sk-ant-api03-abc", "anthropic")
        self.assertIn("sk-ant-admin", warning)

    def test_project_openai_key_is_flagged(self):
        self.assertIn("sk-admin", auth.key_kind_warning("sk-proj-abc", "openai"))

    def test_admin_keys_pass_silently(self):
        self.assertIsNone(auth.key_kind_warning("sk-ant-admin01-abc", "anthropic"))
        self.assertIsNone(auth.key_kind_warning("sk-admin-abc", "openai"))

    def test_unknown_shapes_are_not_guessed(self):
        # A future prefix should not be reported as wrong just for being unfamiliar.
        self.assertIsNone(auth.key_kind_warning("something-else", "anthropic"))
        self.assertIsNone(auth.key_kind_warning("", "anthropic"))
        self.assertIsNone(auth.key_kind_warning("sk-ant-api03", "unknown-vendor"))


class ExpiryReporting(unittest.TestCase):
    def _codex(self, offset):
        import time as _time

        return auth.CodexCredential(
            access_token="t", source="s", expires_at=_time.time() + offset
        )

    def test_phrases(self):
        self.assertEqual(auth.expires_in_phrase(None), "")
        self.assertEqual(auth.expires_in_phrase(-1), "expired")
        self.assertEqual(auth.expires_in_phrase(30), "in 1m", "sub-minute rounds up, never 0m")
        self.assertEqual(auth.expires_in_phrase(43 * 60), "in 43m")
        self.assertEqual(auth.expires_in_phrase(2 * 3600), "in 2h")
        self.assertEqual(auth.expires_in_phrase(2 * 3600 + 30 * 60), "in 2h 30m")
        self.assertEqual(auth.expires_in_phrase(3 * 86400), "in 3d")

    def test_warning_only_inside_the_window(self):
        from tokencheck import cli

        self.assertIsNone(cli._expiry_warning(self._codex(3600), "refresh"))
        self.assertIsNotNone(cli._expiry_warning(self._codex(10 * 60), "refresh"))

    def test_already_expired_is_not_a_soft_warning(self):
        # Expiry is a hard error on the command path; a warning would be wrong.
        from tokencheck import cli

        self.assertIsNone(cli._expiry_warning(self._codex(-60), "refresh"))

    def test_no_expiry_metadata_yields_no_warning(self):
        from tokencheck import cli

        credential = auth.OAuthCredential(access_token="t", source="s", expires_at=None)
        self.assertIsNone(cli._expiry_warning(credential, "refresh"))

    def test_detail_shows_remaining_life(self):
        self.assertIn("expires in", auth._expiry_detail(self._codex(3600), "refresh"))

    def test_detail_nags_when_close(self):
        detail = auth._expiry_detail(self._codex(5 * 60), "run `codex login`")
        self.assertIn("run `codex login`", detail)
        self.assertNotIn("valid", detail)

    def test_detail_reports_expired(self):
        self.assertTrue(auth._expiry_detail(self._codex(-1), "run `codex login`").startswith("expired"))

    def test_every_credential_type_exposes_remaining_life(self):
        for credential in (
            auth.OAuthCredential(access_token="t", source="s", expires_at=1),
            auth.CodexCredential(access_token="t", source="s", expires_at=1),
        ):
            self.assertIsNotNone(credential.expires_in_seconds, type(credential).__name__)
            self.assertTrue(credential.is_expired)

    def test_rejected_api_key_message_says_expired_or_revoked(self):
        from tokencheck import api, openai_api

        for hint in (api.ADMIN_KEY_HINT, openai_api.ADMIN_KEY_HINT):
            self.assertIn("expired", hint)
            self.assertIn("revoked", hint)
        # Anthropic's Admin API cannot be used at all from an individual
        # account, so the hint must say so rather than only offering a URL.
        self.assertIn("individual accounts", api.ADMIN_KEY_HINT)
        # Neither report covers subscription usage; saying so avoids a hunt for
        # Claude Code usage that will never appear there.
        self.assertIn("subscription usage", api.ADMIN_KEY_HINT)
        self.assertIn("api.usage.read", openai_api.ADMIN_KEY_HINT)

    def test_warning_reaches_the_rendered_report(self):
        report = {
            "title": "T",
            "windows": [{"label": "w", "utilization": 1.0, "resets_at": None}],
            "credential_source": "s",
            "expiry_warning": "credential expires in 3m — run `codex login`",
        }
        self.assertIn("expires in 3m", render.render_limits(report, render.Style(False)))


class AccountClassification(unittest.TestCase):
    def _classify(self, oauth):
        import json as _json
        import tempfile
        from tokencheck import account

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            _json.dump({"oauthAccount": oauth}, handle)
            path = Path(handle.name)
        self.addCleanup(path.unlink)
        return account.classify(path)

    def test_individual_plans(self):
        from tokencheck import account

        for plan in ("claude_max", "claude_pro", "claude_free"):
            self.assertEqual(self._classify({"organizationType": plan}).kind, account.INDIVIDUAL, plan)

    def test_team_and_enterprise(self):
        from tokencheck import account

        for plan in ("claude_team", "claude_enterprise", "some_business_tier"):
            self.assertEqual(self._classify({"organizationType": plan}).kind, account.ORGANIZATION, plan)

    def test_seat_tier_implies_organization(self):
        from tokencheck import account

        # Individual plans leave seatTier null; a seat is a per-member concept.
        found = self._classify({"organizationType": "claude_max", "seatTier": "standard"})
        self.assertEqual(found.kind, account.ORGANIZATION)
        self.assertIn("seat", found.reason)

    def test_invoice_billing_implies_organization(self):
        from tokencheck import account

        self.assertEqual(
            self._classify({"organizationType": "claude_max", "billingType": "invoice"}).kind,
            account.ORGANIZATION,
        )

    def test_fails_open_on_missing_or_unfamiliar_signals(self):
        from tokencheck import account

        # A misread must never block the tool, so anything unrecognised is
        # `unknown` — and `unknown` is neither gated nor treated as individual.
        for oauth in ({}, {"organizationType": "claude_plan_from_the_future"}, {"seatTier": None}):
            found = self._classify(oauth)
            self.assertEqual(found.kind, account.UNKNOWN, oauth)
            self.assertFalse(found.is_organization)
            self.assertFalse(found.is_individual)

    def test_null_seat_tier_is_not_an_organization(self):
        from tokencheck import account

        self.assertEqual(
            self._classify({"organizationType": "claude_max", "seatTier": None}).kind,
            account.INDIVIDUAL,
        )

    def test_override_env(self):
        from tokencheck import account

        self.assertFalse(account.override_active({}))
        self.assertFalse(account.override_active({account.OVERRIDE_ENV: "  "}))
        self.assertTrue(account.override_active({account.OVERRIDE_ENV: "1"}))

    def test_notices_name_the_reason_and_the_way_out(self):
        from tokencheck import account

        org = self._classify({"organizationType": "claude_team", "organizationName": "Acme"})
        notice = account.organization_notice(org)
        self.assertIn("Acme", notice)
        self.assertIn("claude_team", notice)
        self.assertIn(account.OVERRIDE_ENV, notice)

        individual = self._classify({"organizationType": "claude_max"})
        usage_notice = account.individual_usage_notice(individual)
        self.assertIn("unavailable for", usage_notice)
        # and points at the commands that do work on this account
        self.assertIn("tokencheck limits", usage_notice)
        self.assertIn("tokencheck local", usage_notice)


class ProviderSelection(unittest.TestCase):
    """`--provider` before or after the subcommand, and each command's default."""

    def _args(self, argv):
        return cli._build_parser().parse_args(argv)

    def test_gemini_is_no_longer_a_choice(self):
        self.assertEqual(cli.PROVIDERS, ("claude", "openai", "all"))
        for command in (cli._LIMIT_PROVIDERS, cli._WHOAMI_PROVIDERS):
            self.assertNotIn("gemini", command)

    def test_limits_and_whoami_default_to_every_provider(self):
        for command in ("limits", "whoami"):
            self.assertEqual(cli._provider(self._args([command]), "all"), "all")

    def test_bare_invocation_defaults_to_every_provider(self):
        self.assertEqual(cli._provider(self._args([]), "all"), "all")

    def test_flag_is_accepted_on_either_side_of_the_subcommand(self):
        for argv in (["limits", "-p", "openai"], ["-p", "openai", "limits"], ["--provider", "openai"]):
            self.assertEqual(cli._provider(self._args(argv), "all"), "openai", argv)

    def test_subcommand_absent_flag_does_not_clobber_the_global_one(self):
        # The subparser copy suppresses its default, so `-p openai limits`
        # survives `limits` contributing nothing.
        self.assertEqual(cli._provider(self._args(["-p", "openai", "limits"]), "all"), "openai")

    def test_usage_defaults_to_claude_not_all(self):
        # An admin-key report against one organization has nothing to merge, and
        # the claude branch prints the individual-account notice.
        self.assertEqual(cli._provider(self._args(["usage"]), "claude"), "claude")

    def test_usage_rejects_an_explicit_all(self):
        import contextlib
        import io

        args = self._args(["usage", "-p", "all"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cli._cmd_usage(args, render.Style(False)), cli.EXIT_ERROR)
        self.assertIn("one provider at a time", stderr.getvalue())

    def test_provider_flag_appears_in_top_level_help(self):
        text = cli._build_parser().format_help()
        self.assertIn("--provider", text)
        self.assertIn("default: all", text)
