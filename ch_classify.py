"""Classify a processed company on three separate axes instead of one grade.

The old `fx_grade` was score x turnover. Score measures how much readable
disclosure a filing contains, so large companies with wordy strategic reports
graded A whether or not they touched foreign currency. Caterite came back A
while its own accounts state that all sales are to UK customers.

The reverse failure mattered more. Unhedged companies have no forwards to
disclose, no hedge reserve and no fair value movements, so they score lower
precisely because they are unhedged. That buried Nationwide Produce at £204m
and International Tyres at £45.5m, both exposed and both far easier to sell to
than an established hedger.

So: separate what the accounts prove, what the company does about it, and how
far the filing can be trusted. Then rank on winnability rather than on wordiness.
"""

from __future__ import annotations

import re

# Only currency-specific denials count. Sales geography does not: an importer
# can sell 100% into the UK and still buy everything in dollars. Kedem Europe
# reports "domestic UK sales only" in the same breath as "imports and wholesales
# wines", and is a perfectly good prospect on the purchase side.
DENIED = re.compile(
    r"minimal exposure to (foreign )?(exchange|currency)|"
    r"not exposed to (any )?(foreign |currency )?exchange|"
    r"no foreign currency (transactions|exposure|risk)|"
    r"no exposure to (foreign )?(exchange|currency)|"
    r"all (purchases|transactions) are (denominated )?in (sterling|GBP|pounds)",
    re.I,
)
QUANTIFIED = re.compile(r"\d", re.I)
HEDGE_WORDS = re.compile(r"forward|derivative|option|collar|swap|hedg", re.I)
NO_HEDGE = re.compile(
    r"none evident|no hedging|not use (financial instruments|derivatives)|"
    r"does not hedge|no derivative|no forward|none held|not currently",
    re.I,
)
DISCRETIONARY = re.compile(
    r"where appropriate|selective|as required|when considered|from time to time|"
    r"where possible|if appropriate|may (enter|use)",
    re.I,
)
IMPORTER = re.compile(
    r"import|export|overseas|foreign|abroad|international|far east|europe|"
    r"china|india|usa|united states|germany|italy|poland|scandinav",
    re.I,
)

THIN_FILING = {"small", "abridged", "micro", "dormant", "unknown", ""}


def _txt(row, *keys) -> str:
    return " ".join(str(row.get(k, "") or "") for k in keys)


def fx_evidence(row) -> str:
    """What the filed accounts actually demonstrate about currency exposure."""
    blob = _txt(row, "call_ammo", "est_fx_volume", "one_liner", "findings",
                "red_flags", "export_split", "fx_pnl_figures")
    if DENIED.search(blob):
        return "denied"

    hard = _txt(row, "fx_pnl_figures", "hedging_instruments", "export_split")
    if QUANTIFIED.search(hard) and not re.fullmatch(r"[\s\-]*", hard):
        return "quantified"

    est = str(row.get("est_fx_volume", ""))
    if est.strip() and "cannot estimate" not in est.lower():
        return "quantified"

    if str(row.get("currencies_named", "")).strip() not in ("", "not disclosed"):
        return "stated"
    if HEDGE_WORDS.search(blob) or "currenc" in blob.lower() or "exchange" in blob.lower():
        return "stated"
    if IMPORTER.search(blob):
        return "implied"
    return "none"


def fx_position(row) -> str:
    """Whether they already do something about it. Unhedged is the easier sell."""
    inst = str(row.get("hedging_instruments", "")).strip()
    soph = str(row.get("sophistication", "")).lower()
    blob = _txt(row, "hedging_instruments", "call_ammo", "one_liner")

    if NO_HEDGE.search(inst) or "exposed-unhedged" in soph:
        return "unhedged"
    if inst and HEDGE_WORDS.search(inst):
        return "partial" if DISCRETIONARY.search(blob) else "hedged"
    if "hedger" in soph:
        return "hedged"
    if "minimal" in soph:
        # "minimal" is the brief's judgement that exposure is small, not that a
        # large exposure is uncovered. Mapping it to unhedged inflated P1 badly.
        return "minimal"
    return "unknown"


def confidence(row) -> str:
    """How far the filing can be trusted. Small and abridged accounts prove nothing."""
    cat = str(row.get("accounts_category", "")).lower().strip()
    err = str(row.get("error", "")).lower()
    if "scanned pdf" in err or "no readable" in err or "no confident match" in err:
        return "none"
    if cat in ("micro", "dormant"):
        return "none"
    if cat in THIN_FILING:
        return "low"
    if cat == "medium":
        return "medium"
    return "high"


def _turnover(row) -> float:
    try:
        return float(re.sub(r"[^0-9.]", "", str(row.get("turnover", "")) or "0") or 0)
    except ValueError:
        return 0.0


def priority(row) -> str:
    """Sortable rank. Ordered by how winnable the meeting is, not by filing size.

    P1  exposed and doing nothing about it, at real size. Easiest conversation.
    P2  exposed and only partly covered. A gap exists and they know it.
    P3  established hedger. Real money, but you are displacing an incumbent.
    P4  probably exposed, filing too thin to prove it. Qualify by phone.
    X   accounts state there is no exposure, or nothing readable.
    """
    ev, pos, conf, t = fx_evidence(row), fx_position(row), confidence(row), _turnover(row)

    if ev == "denied":
        return "X - accounts say no exposure"
    if conf == "none" and ev in ("none", "implied"):
        return "X - nothing readable"

    # A turnover figure is required for the top tiers. Without one there is no
    # way to tell a £40m importer from a £400k one, and 140 companies with no
    # figure were previously landing in P1.
    big = t >= 5_000_000
    real = t >= 2_000_000
    if pos == "minimal":
        return "P4 - thin filing, qualify by phone" if conf in ("low", "none") else "X - no evidence"
    if not real:
        return "P4 - thin filing, qualify by phone" if ev in ("quantified", "stated", "implied") else "X - no evidence"

    if pos == "unhedged" and ev in ("quantified", "stated") and (big or real):
        return "P1 - exposed, no cover"
    if pos == "partial" and ev in ("quantified", "stated") and (big or real):
        return "P2 - partly covered"
    if pos == "unhedged" and ev == "implied" and big:
        return "P2 - partly covered"
    if pos == "hedged" and ev == "quantified" and (big or real):
        return "P3 - established hedger"
    if pos == "hedged" and (big or real):
        return "P3 - established hedger"
    if conf in ("low", "none"):
        return "P4 - thin filing, qualify by phone"
    if ev in ("stated", "implied"):
        return "P4 - thin filing, qualify by phone"
    return "X - no evidence"


def classify(row) -> dict:
    """All four fields for one processed company."""
    return {
        "fx_evidence": fx_evidence(row),
        "fx_position": fx_position(row),
        "confidence": confidence(row),
        "priority": priority(row),
    }
