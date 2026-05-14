from __future__ import annotations

"""
News headline classifier — keyword-based for backtest (Tier 1, free).
In Phase 2 this is replaced by the Groq LLM classifier for real-time use.
Returns (tier, category): tier is A/B/C, category is a descriptive label.
"""

from typing import Tuple

# Tier C patterns: avoid — check first (if matched, skip the trade)
_TIER_C = [
    ("dilut", "dilution"),
    ("secondary offering", "secondary_offering"),
    ("going concern", "going_concern"),
    ("delisting", "delisting"),
    ("reverse split", "reverse_split"),
    ("reverse-split", "reverse_split"),
    ("earnings miss", "earnings_miss"),
    ("misses estimate", "earnings_miss"),
    ("missed estimate", "earnings_miss"),
    ("downgrade", "downgrade"),
    ("cut to", "downgrade"),
    ("lowered to", "downgrade"),
    ("bankruptcy", "bankruptcy"),
    ("chapter 11", "bankruptcy"),
    ("withdrawn", "withdrawn"),
    ("investigation", "investigation"),
    ("sec charges", "regulatory_action"),
    ("fraud", "fraud"),
]

# Tier A patterns: strongest catalysts
_TIER_A = [
    ("fda approv", "FDA_approval"),
    ("fda grants", "FDA_approval"),
    ("fda breakthrough", "FDA_breakthrough"),
    ("breakthrough therapy", "FDA_breakthrough"),
    ("pdufa", "FDA_PDUFA"),
    ("priority review", "FDA_priority"),
    ("patent approv", "patent_approval"),
    ("patent granted", "patent_approval"),
    ("acquired by", "M&A_target"),
    ("acquisition", "M&A_target"),
    ("tender offer", "M&A_target"),
    ("takeover", "M&A_target"),
    ("merger agreement", "M&A_target"),
    ("going private", "M&A_target"),
    ("buyout", "M&A_target"),
    ("microsoft", "partnership_major"),
    ("apple", "partnership_major"),
    ("google", "partnership_major"),
    ("amazon", "partnership_major"),
    ("nvidia", "partnership_major"),
    ("department of defense", "major_contract"),
    ("dod contract", "major_contract"),
    ("government contract", "major_contract"),
    ("regulatory approv", "regulatory_win"),
    ("sec approv", "regulatory_win"),
]

# Tier B patterns: good but secondary
_TIER_B = [
    ("upgrade", "analyst_upgrade"),
    ("raised to buy", "analyst_upgrade"),
    ("raised to outperform", "analyst_upgrade"),
    ("price target", "analyst_upgrade"),
    ("partnership", "partnership"),
    ("collaboration", "partnership"),
    ("agreement", "agreement"),
    ("contract", "contract"),
    ("license", "license"),
    ("earnings beat", "earnings_beat"),
    ("beats estimate", "earnings_beat"),
    ("beat estimate", "earnings_beat"),
    ("exceeded estimate", "earnings_beat"),
    ("surpassed estimate", "earnings_beat"),
    ("revenue beat", "earnings_beat"),
    ("guidance raise", "guidance_raise"),
    ("raises guidance", "guidance_raise"),
    ("raises outlook", "guidance_raise"),
    ("insider buying", "insider_buying"),
    ("insider purchase", "insider_buying"),
    ("short squeeze", "short_squeeze"),
    ("high short interest", "short_squeeze"),
    ("positive data", "positive_data"),
    ("phase 2", "clinical_data"),
    ("phase 3", "clinical_data"),
    ("clinical trial", "clinical_data"),
    ("topline results", "clinical_data"),
]


def classify_headline(headline: str, ticker: str = "") -> Tuple[str, str]:
    """
    Returns (tier, category).
    Tier A = strong catalyst, B = moderate, C = avoid/negative.
    """
    h = headline.lower()

    # Check C first — these are deal-breakers
    for pattern, category in _TIER_C:
        if pattern in h:
            return "C", category

    # Check A
    for pattern, category in _TIER_A:
        if pattern in h:
            return "A", category

    # Check B
    for pattern, category in _TIER_B:
        if pattern in h:
            return "B", category

    # Default: if news exists but is unclassified, treat as B (neutral positive)
    return "B", "general_catalyst"
