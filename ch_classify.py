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

# Countries that mean treasury is very unlikely to sit in the UK. Designplan,
# Hochiki, Cargill, Altek and Bergstrom were all lost on exactly this.
FOREIGN = re.compile(
    r"\b(japan|jersey|germany|german|usa|u\.s\.a|united states|america|sweden|swedish|"
    r"denmark|danish|norway|netherlands|dutch|france|french|spain|spanish|italy|italian|"
    r"ireland|irish|switzerland|swiss|austria|belgium|poland|polish|china|chinese|india|"
    r"indian|korea|taiwan|hong kong|singapore|australia|canada|cayman|luxembourg|"
    r"jamaica|turkey|turkish|israel|brazil|south africa|"
    r"gmbh|s\.?a\.?r\.?l|b\.?v\b|a/s\b|\bab\b|\bas\b|\bnv\b|\bsas\b|\bspa\b|inc\.|"
    r"corporation|established under law of (a )?state other than)",
    re.I,
)
UK_MARKER = re.compile(
    r"\b(england and wales|england|wales|scotland|northern ireland|united kingdom|\buk\b)\b",
    re.I,
)
# A parent whose name echoes the subsidiary's is nearly always a holding vehicle
# rather than an operating group with its own treasury function.
HOLDING_WORD = re.compile(r"\b(holdings?|group|investments?|bidco|topco|midco|newco)\b", re.I)


def _stem_name(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    drop = {"limited", "ltd", "plc", "llp", "uk", "holdings", "holding", "group",
            "investments", "investment", "the", "and", "co", "company", "bidco",
            "topco", "midco", "newco", "england", "wales", "scotland", "united",
            "kingdom", "yes", "no", "not", "disclosed", "parent", "ultimate"}
    return {w for w in s.split() if w and w not in drop and len(w) > 2}


def parent_control(row) -> str:
    """Who is likely to decide on FX.

    none          no parent, the person you reach decides
    uk-holding    UK parent that echoes the company name, so a holding structure
    uk-group      UK parent with a different name, an operating group. Ask first
    foreign       overseas parent, treasury almost certainly sits abroad
    unknown       stated but not resolvable
    """
    raw = str(row.get("overseas_parent", "") or "").strip()
    if not raw or raw.lower() in ("no", "none", "not disclosed", "nan"):
        return "none"
    if FOREIGN.search(raw):
        return "foreign"
    # A holding-sounding name is only reassuring if the parent is also UK. Hochiki
    # Group reads like a holding company but the parent is Japanese, so requiring
    # a UK marker keeps that in "unknown" where it belongs.
    if UK_MARKER.search(raw):
        shared = _stem_name(raw) & _stem_name(row.get("company", ""))
        if shared or HOLDING_WORD.search(raw):
            return "uk-holding"
        return "uk-group"
    return "unknown"


# A listed group is its own parent, so parent_control returns "none" and the
# ownership filter waves it through. But a plc of this size runs treasury
# centrally, and the person on the contact record will not be deciding.
LISTED = re.compile(r"\bplc\b|\bp\.l\.c\b|listed|london stock exchange|"
                    r"\bAIM\b|premium listing|main market", re.I)
BIG = 250_000_000


def too_big(row) -> bool:
    """Listed, or large enough that treasury is a department rather than a person."""
    blob = " ".join(str(row.get(k, "")) for k in
                    ("company", "matched_name", "one_liner", "call_ammo"))
    try:
        t = float(re.sub(r"[^0-9.]", "", str(row.get("turnover", "")) or "0") or 0)
    except ValueError:
        t = 0
    return bool(LISTED.search(blob)) or t >= BIG


def winnable(row) -> bool:
    """Is this a company where the person you reach can actually decide?"""
    return parent_control(row) in ("none", "uk-holding") and not too_big(row)


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


DENIAL_IN_FINDINGS = re.compile(r"DENIAL: accounts state no FX exposure", re.I)
# The brief is written by a model reading the filing; when it says plainly that
# there is no exposure, that beats any keyword score. Soanes Poultry stated
# "the company does not deal in any foreign currencies" and still came back P1.
DENIAL_IN_PROSE = re.compile(
    r"do(es)? not deal in any foreign currenc|"
    r"no exposure to (foreign )?(exchange|currency)|"
    r"minimal exposure to exchange|"
    r"all (sales and purchases|transactions) are (denominated )?in sterling|"
    r"(sales and purchases|transactions) are (dominated|denominated) in sterling|"
    r"transacts exclusively in sterling|"
    r"considers? there to be no exposure to currency risk|"
    r"does not (deal|trade) in (any )?foreign currenc|"
    r"does not (purchase|buy|sell|engage in [a-z]+)[^.]{0,50}foreign currenc|"
    r"thereby avoiding exposure to foreign exchange|"
    r"avoid(ing|s)? exposure to (foreign )?(exchange|currency)|"
    r"no (material |significant )?(foreign )?(currency|exchange) (risk|exposure)|"
    r"not exposed to (significant |material )?(foreign )?(currency|exchange)",
    re.I,
)


# The brief is written after reading the filing; when it says the regex signal
# was not borne out, believe the brief. PJ Nicholls was flagged an active hedger
# by a keyword while its own brief said "no forward contracts or hedging
# instruments evidenced in accounts despite regex signal".
BRIEF_OVERRULES = re.compile(
    r"despite (the )?regex signal|"
    r"no (forward contracts?|hedging instruments?|derivatives?)[^.]{0,60}"
    r"(evidenced|held|disclosed|identified|in place)|"
    r"(hedging instruments?|forward contracts?)[^.]{0,30}not (held|used|evidenced)|"
    r"no evidence of (actual |active )?(forward|hedging|derivative)",
    re.I,
)


def brief_contradicts_hedger(row) -> bool:
    """The written brief says the hedging signal was a false positive."""
    return bool(BRIEF_OVERRULES.search(str(row.get("call_ammo", ""))))


def denies_fx(row) -> bool:
    """Has the company said in its own accounts that it has no FX exposure?"""
    if DENIAL_IN_FINDINGS.search(str(row.get("findings", ""))):
        return True
    blob = " ".join(str(row.get(k, "")) for k in
                    ("call_ammo", "excerpts", "one_liner", "hedging_instruments"))
    return bool(DENIAL_IN_PROSE.search(blob))


def priority(row) -> str:
    """Sortable rank. Ordered by how winnable the meeting is, not by filing size.

    P1  exposed and doing nothing about it, at real size. Easiest conversation.
    P2  exposed and only partly covered. A gap exists and they know it.
    P3  established hedger. Real money, but you are displacing an incumbent.
    P4  probably exposed, filing too thin to prove it. Qualify by phone.
    X   accounts state there is no exposure, or nothing readable.
    """
    # An explicit denial outranks every positive signal below.
    if denies_fx(row):
        return "X - accounts say no exposure"
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
    if pos == "hedged" and not brief_contradicts_hedger(row):
        if ev == "quantified" and (big or real):
            return "P3 - established hedger"
        if big or real:
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
