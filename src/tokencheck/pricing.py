"""Anthropic list prices, for turning locally recorded token counts into dollars.

These are **API list prices**, not what you are billed. A Claude subscription
(Pro/Max) has no per-token charge, so the figures TokenCheck derives from local
transcripts are "what this would have cost on the API" — useful for comparing
sessions and models, useless as an invoice.

Prices are per million tokens, from the Anthropic pricing page as of the date
below. Cache multipliers are API-wide: a 5-minute cache write costs 1.25x the
input rate, a 1-hour write 2x, and a cache read 0.1x.
"""

from __future__ import annotations

PRICES_AS_OF = "2026-08-13"

CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.1

# (model id prefix, input $/MTok, output $/MTok). Longest prefix wins, so a
# more specific id can override a family default.
_PRICES: list[tuple[str, float, float]] = [
    ("claude-fable-5", 10.00, 50.00),
    ("claude-mythos-5", 10.00, 50.00),
    ("claude-mythos-preview", 10.00, 50.00),
    ("claude-opus-5", 5.00, 25.00),
    ("claude-opus-4-8", 5.00, 25.00),
    ("claude-opus-4-7", 5.00, 25.00),
    ("claude-opus-4-6", 5.00, 25.00),
    ("claude-opus-4-5", 5.00, 25.00),
    ("claude-opus-4-1", 15.00, 75.00),
    ("claude-opus-4", 15.00, 75.00),
    ("claude-sonnet-5", 3.00, 15.00),
    ("claude-sonnet-4-6", 3.00, 15.00),
    ("claude-sonnet-4-5", 3.00, 15.00),
    ("claude-sonnet-4", 3.00, 15.00),
    ("claude-haiku-4-5", 1.00, 5.00),
    ("claude-3-5-haiku", 0.80, 4.00),
    ("claude-3-haiku", 0.25, 1.25),
]

ESTIMATE_NOTE = (
    "cost is estimated at Anthropic API list prices "
    f"(as of {PRICES_AS_OF}) — subscription usage is not billed per token"
)


def normalize_model(model: str | None) -> str | None:
    """Strip the decorations Claude Code appends to a model id.

    ``claude-opus-5[1m]`` and ``claude-opus-5-fast`` both price as
    ``claude-opus-5``. Returns None for synthetic/absent models.
    """
    if not isinstance(model, str):
        return None
    name = model.strip().lower()
    if not name or name.startswith("<"):
        return None
    return name.split("[", 1)[0].strip()


def rates_for(model: str | None) -> tuple[float, float] | None:
    """(input, output) dollars per million tokens, or None if unpriced."""
    name = normalize_model(model)
    if name is None:
        return None
    best: tuple[str, float, float] | None = None
    for prefix, input_rate, output_rate in _PRICES:
        if name.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, input_rate, output_rate)
    if best is None:
        return None
    return best[1], best[2]


def estimate_cost(model: str | None, counts: dict[str, int]) -> float | None:
    """List-price cost of one response's token counts.

    Returns None when the model is unknown — callers render that as "—" rather
    than guessing a price for a model that may not have shipped yet.
    """
    rates = rates_for(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    per_token = input_rate / 1_000_000
    return (
        counts.get("input_tokens", 0) * per_token
        + counts.get("cache_creation_5m_tokens", 0) * per_token * CACHE_WRITE_5M_MULTIPLIER
        + counts.get("cache_creation_1h_tokens", 0) * per_token * CACHE_WRITE_1H_MULTIPLIER
        + counts.get("cache_read_input_tokens", 0) * per_token * CACHE_READ_MULTIPLIER
        + counts.get("output_tokens", 0) * (output_rate / 1_000_000)
    )


def is_priced(model: str | None) -> bool:
    return rates_for(model) is not None
