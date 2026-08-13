# TokenCheck — plan

Goal: a CLI that reports Claude (Anthropic) usage — token counts, rate-limit
utilization, reset times, cost — using Anthropic's usage/observability APIs.

Protocol knowledge is ported from [CodexBar](https://github.com/steipete/CodexBar)
(MIT, © 2026 Peter Steinberger). Verified license permits commercial reuse with
notice preservation — see `NOTICE`.

## Assumption stated up front

The request said "my Cloud"; read as **Claude**. Evidence: CodexBar is a
Claude/Codex usage monitor, the machine has `Claude Code-credentials` in the
macOS Keychain, and Claude Code 2.1.229 is installed.

## Tasks

- [x] Verify CodexBar license (MIT — commercial reuse OK with attribution)
- [x] Extract the Claude auth + endpoint knowledge from CodexBar's Swift source
- [x] Python 3, stdlib-only package under `src/tokencheck/`
- [x] Auth resolution (OAuth): `CLAUDE_CODE_OAUTH_TOKEN` → macOS Keychain
      (`Claude Code-credentials`) → `$CLAUDE_CONFIG_DIR/.credentials.json` →
      `~/.claude/.credentials.json`
- [x] Auth resolution (Admin API): `--admin-key` → `ANTHROPIC_ADMIN_KEY` →
      `ANTHROPIC_ADMIN_API_KEY`
- [x] `limits` — subscription rate-limit windows from `GET /api/oauth/usage`
      (5-hour, 7-day, per-model weekly, extra usage), with reset times
- [x] `usage` — org token/cost report from the Admin API
      (`/v1/organizations/usage_report/messages`, `/v1/organizations/cost_report`)
- [x] `whoami` — account/org identity from `GET /api/oauth/profile`
- [x] `auth` — show which credential sources are available, no secrets printed
- [x] `--json` on every command
- [x] Table renderer with utilization bars and humanized reset times
- [x] README + NOTICE + pyproject
- [x] Verify live against real credentials

## Deliberate non-goals (v1)

- **No OAuth token refresh.** Refresh tokens rotate; refreshing with Claude
  Code's stored refresh token can invalidate the session Claude Code itself
  holds. TokenCheck reads the access token and never writes back to the
  Keychain. On 401 it tells the user to run `claude`.
- No claude.ai web-session (cookie) source — CodexBar has one, but it depends on
  scraping a browser session and is fragile.

## Phase 2 — capture all emitted telemetry (2026-08-13)

Goal: the first version only read two remote endpoints. Everything Claude Code
emits locally was untouched. Capture all four channels and explain each figure.

- [x] `pricing.py` — dated list-price table, 1h/5m cache multipliers, unknown
      models return None rather than a guessed rate
- [x] `transcripts.py` — parse `~/.claude/projects/**/*.jsonl`, dedupe on
      `message.id`, split cache TTLs, group by day/model/project/session/tool/
      skill/effort/version
- [x] `otlp.py` — stdlib OTLP/HTTP-JSON receiver + metric/log flattening
- [x] `claudeconfig.py` — plan, rate-limit tier, cached utilization, counters
- [x] `local`, `plan`, `collect`, `telemetry`, `all` commands
- [x] One-line gloss beside every figure (`--no-notes` to suppress)
- [x] CodexBar gap analysis, verified against its `main` branch
- [x] 23 new tests (39 total), README rewrite

### Two bugs the real data caught

1. **Row inflation.** Claude Code writes one transcript row per content block,
   each repeating the same `usage` object; resumed sessions re-copy rows. 2,607
   rows are 1,101 responses — naive summing overstates everything by 2.37x.
   Dedupe on `message.id`, keep the largest row per id.
2. **Subagent transcripts are a level deeper.** They live at
   `<project>/<session>/subagents/*.jsonl`; the initial `*/*.jsonl` glob missed
   them, hiding 100% of Fable 5 (advisor) spend. Fixed with `rglob`.

A third was caught by live telemetry: the documented event names are
`claude_code.api_error`, but the `event.name` actually emitted is bare
(`api_error`). Names are normalized both ways.

A fourth was caught by review rather than data, and is the same bug class again:
OTLP sums arrive as either **delta** (increment per export) or **cumulative**
(running total repeated every export). Claude Code sent delta here — verified
`aggregationTemporality=1` in the real capture — so the figures were right, but
blind summing would have multiplied a long session's totals by the number of
exports under a cumulative exporter. `collect` now pins delta explicitly, and
`summarize` counts only each series' increment when it sees cumulative points.
A short live run cannot expose this (one export equals its own delta), so it is
pinned by unit test instead.

## Review

Implemented as planned. Live-verified against the real Keychain credentials:
`limits` returned real 5-hour / 7-day / per-model windows with reset times;
`whoami` returned the account email and org UUID. The Admin API path is
implemented and exercised for error handling, but cannot be end-to-end verified
here — no `sk-ant-admin…` key is present on this machine (it errors clearly and
tells you where to mint one). Its bucket merging, cent→USD conversion and
renderer are covered by fixtures in `tests/test_parsing.py` (16 tests, all pass).

### Phase 2 verification

All live-verified against real data on this machine: `local` reports 1,139
deduplicated responses across 10 sessions and 8 projects (~$223 at list prices,
of which subagents are 0.4%); every `--by` dimension reconciles to the same
totals; `plan` reports `claude_max` / `default_claude_max_20x`; `collect`
captured a real `claude -p` run end-to-end and `telemetry` rendered its metrics,
events and token split. Protobuf requests are rejected with a clear message and
unknown paths 404. 39 tests pass. `limits`, `usage`, `whoami` and `auth` are
unchanged.

### Not committed

`/Users/abhijitgore` is itself a git repository with zero commits and everything
untracked, so `TokenCheck/` has no repo of its own. Committing from here would
have committed into the home repo; `git init` inside TokenCheck was blocked by
the permission classifier. Run `git init` here first, then commit.

---

# Round 3 — reporting period + OpenAI and Gemini

Request: control the report time period (1hr / 1day / last 30d, default 1day),
and add OpenAI and Gemini support.

## Tasks

- [x] `period.py` — parse `1h`/`1d`/`30d` plus arbitrary `Nh`/`Nd`; trailing
      window, not calendar-aligned; 31-day ceiling (what the usage APIs retain)
- [x] `--period` on `usage`, `local`, `telemetry`, `all`; default `1d`
- [x] `--days N` kept as a hidden alias so existing invocations keep working
- [x] `usage` default changed from 7 days to 1 day, per the request
- [x] Hourly buckets for periods ≤ 1 day, daily beyond; `--daily` renders either
- [x] OpenAI subscription limits — `GET chatgpt.com/backend-api/wham/usage`
      using the Codex CLI token in `~/.codex/auth.json`
- [x] OpenAI org usage — `/v1/organization/usage/completions` + `/costs`
- [x] Gemini quota — `POST cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota`
      using `~/.gemini/oauth_creds.json`, plus `:loadCodeAssist` for the tier
- [x] `limits --provider claude|openai|gemini|all`
- [x] `auth` inventories all three providers
- [x] Tests + README

## Decisions worth recording

**Cost endpoints are daily-only.** Anthropic's `cost_report` and OpenAI's
`/organization/costs` only accept `bucket_width=1d`, while the token endpoints
do `1h`. On a sub-day period the cost therefore covers the calendar day so far.
Rather than hide the mismatch, the report labels it `(day so far)` and the
hourly `--daily` table omits its COST column — a column of zeros next to a
non-zero total reads as a bug.

**Gemini has no usage report.** Google publishes no API returning token counts
or spend for a Gemini account; quota-remaining per model is all that exists. So
`usage --provider` offers only claude and openai, and the README states the gap
rather than leaving it to be discovered.

**Gemini reports remaining, everyone else reports used.** Inverted at the parser
(`used = (1 − remainingFraction) × 100`) so one renderer serves all three
providers instead of each carrying its own polarity.

**No token refresh, now across three providers.** `~/.codex/auth.json` and
`~/.gemini/oauth_creds.json` both hold refresh tokens owned by their CLIs.
Redeeming either would rotate it out from under that CLI's session, so
TokenCheck reads access tokens only and reports expiry instead.

**`--provider all` degrades per provider.** One provider being unconfigured
reports in place and the others still render; the run only fails if every
provider fails.

## Review

77 tests pass. Live-verified against real credentials for all three providers:
`limits --provider all` returned Claude's 5-hour/7-day windows, ChatGPT Plus's
7-day window, and Gemini's four per-model quotas — each with reset times.
`--period` verified on `local` (1h vs 30d return different populations) and on
the error path (`--period 5x` exits 1 with a usable message).

Both admin-key usage paths remain fixture-tested rather than live: no
`sk-ant-admin…` or `sk-admin…` key exists on this machine. Both were exercised
against bogus keys and produce the correct provider-specific 401 guidance.

Still not committed — `/Users/abhijitgore` is the enclosing git repo. Run
`git init` inside TokenCheck before any `git add`.
