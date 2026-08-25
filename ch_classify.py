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

import os
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


_FX_AMOUNT = re.compile(
    r"(exchange|fx|currency)\s+(gains?|losses?|credit|movement)[^.]{0,40}"
    r"[£$\u20ac]?\s?\d[\d,]{2,}|"
    r"[£$\u20ac]\s?\d[\d,]{2,}[^.]{0,30}(exchange|fx|currency)\s+(gains?|losses?)|"
    r"forward (currency |exchange |usd |eur )?(purchase )?contracts?[^.]{0,40}"
    r"[£$\u20ac]\s?\d[\d,]{2,}|"
    r"(committed to (pay|buy|sell)|outstanding|valued at)[^.]{0,25}[£$\u20ac]\s?\d[\d,]{2,}|"
    r"\d{1,3}%\s+of\s+(stock|purchases?|sales?|turnover)[^.]{0,30}"
    r"(usd|eur|dollar|euro)",
    re.I,
)
_REAL_INSTRUMENT = re.compile(
    r"(uses?|using|holds?|held|outstanding|in use|actively hedges?|"
    r"enters? into|entered into)[^.]{0,50}(forward|currency contract|swap|collar|option)|"
    r"forward[^.]{0,30}(contracts?|contracts? of)[^.]{0,30}(outstanding|held|in use|valued)",
    re.I,
)


def has_quantified_fx(row) -> bool:
    """A real currency figure or forward holding somewhere in the brief."""
    blob = " ".join(str(row.get(k, "")) for k in
                    ("call_ammo", "fx_pnl_figures", "hedging_instruments", "est_fx_volume"))
    if "not disclosed" in blob.lower() and len(blob) < 60:
        return False
    return bool(_FX_AMOUNT.search(blob))


def holds_instruments(row) -> bool:
    blob = " ".join(str(row.get(k, "")) for k in ("hedging_instruments", "call_ammo"))
    if re.search(r"none evident|no hedging instruments|not hedged", blob, re.I) \
       and not _REAL_INSTRUMENT.search(blob):
        return False
    return bool(_REAL_INSTRUMENT.search(blob))


_XBRL_ONLY = re.compile(r"taxonomy|metadata|schema|xbrl|listed in .{0,20}tag", re.I)
_UK_ONLY = re.compile(
    r"all (turnover|sales|revenue)[^.]{0,40}(arose|are|is)[^.]{0,30}"
    r"(within |in )?the united kingdom|"
    r"all sales are to uk|100% (uk|domestic)|"
    r"no (export|overseas) (revenue|sales|turnover) disclosed",
    re.I,
)


def has_currency_flow(row) -> bool:
    """Positive evidence that money actually crosses a currency border.

    Absence of hedging is not evidence of exposure. A UK builders merchant with
    no forwards is not an unhedged importer, it is a company with nothing to
    hedge. P1 means "exposed and uncovered", so the exposure has to be shown,
    not inferred from silence.
    """
    fx = str(row.get("fx_pnl_figures", "")).strip().lower()
    if fx and fx != "not disclosed":
        return True

    cur = str(row.get("currencies_named", "")).strip()
    if cur and cur.lower() != "not disclosed" and not _XBRL_ONLY.search(cur):
        # GBP alone is not a foreign currency
        if re.search(r"\b(eur|usd|euro|dollar|yen|jpy|chf|aud|cad|nzd|zar|"
                     r"renminbi|rmb|cny|sek|nok|dkk|pln)\b", cur, re.I):
            return True

    hedge = str(row.get("hedging_instruments", "")).strip().lower()
    if hedge and hedge not in ("not disclosed", "none evident"):
        return True

    exp = str(row.get("export_split", "")).strip().lower()
    if exp and exp != "not disclosed" and not _UK_ONLY.search(exp):
        if re.search(r"\d", exp):
            return True

    est = str(row.get("est_fx_volume", "")).strip()
    if est.upper().startswith("EST"):
        return True

    blob = " ".join(str(row.get(k, "")) for k in ("call_ammo", "one_liner", "excerpts"))
    if _UK_ONLY.search(blob):
        return False
    # buying or selling abroad, stated in prose
    if re.search(r"(import|purchas\w+|sourc\w+|buy\w*)[^.]{0,60}"
                 r"(overseas|abroad|from (europe|china|the far east|the eu)|"
                 r"in (euro|dollar|usd|eur))", blob, re.I):
        return True
    if re.search(r"(export|sell\w*|sales)[^.]{0,50}(to (europe|the us|overseas)|"
                 r"overseas|international markets)", blob, re.I):
        return True
    return False



# ---------------------------------------------------------------------------
# FX evidence gate
#
# One definition of "does this company touch foreign currency", shared by every
# tool. It lived in run_discover only, which meant the triage, the email builder
# and the call sheet each had their own idea and disagreed with each other.
#
# The rule: a company earns a place only if its own filing shows money crossing
# a border. Absence of hedging is not evidence of exposure, XBRL currency codes
# are not evidence of anything, and a denial beats every positive signal.
# ---------------------------------------------------------------------------

_CCY = re.compile(
    r"\b(eur|usd|euro|dollar|yen|jpy|chf|aud|cad|nzd|zar|sek|nok|dkk|pln|"
    r"renminbi|rmb|cny|rupee|inr|krona|franc)\b",
    re.I,
)
# Currency codes turn up in the XBRL header of every filing ever made. That is
# not evidence of anything.
_XBRL_NOISE = re.compile(
    r"taxonomy|metadata|schema|xbrl|iso4217|"
    r"(named|listed|present|referenced) in .{0,24}(tag|taxonomy|metadata|schema)",
    re.I,
)
_FX_WORDS = re.compile(
    r"foreign (currency|exchange)|exchange (gain|loss|rate|difference)|"
    r"forward (contract|currency|exchange)|hedg\w+|currency risk|"
    r"import\w*|export\w*|overseas (suppl|custom|sale|purchas)|"
    r"denominated in|invoiced in|priced in",
    re.I,
)
_UK_ONLY = re.compile(
    r"all (turnover|sales|revenue)[^.]{0,40}(arose|are|is)[^.]{0,30}"
    r"(within |in )?the united kingdom|"
    r"no (export|overseas) (revenue|sales|turnover)|"
    r"does not (deal|trade) in (any )?foreign currenc|"
    r"no exposure to (foreign )?(exchange|currency)",
    re.I,
)


# Strict mode drops the inferred routes and keeps only the four that quote
# something explicit from the filing. Set FX_STRICT=0 in the environment to go
# back to the wider gate.
FX_STRICT = os.environ.get("FX_STRICT", "1") != "0"


def fx_evidence(res: dict) -> str:
    """Why this company looks like it touches foreign currency, or ''.

    Strict mode keeps only what the accounts state outright: a figure in the
    P&L, instruments actually held, a named foreign currency, or an export
    split with numbers. The softer routes, currency words appearing near trade
    words and importer/exporter in the description, are inference rather than
    disclosure and are switched off.
    """
    # a figure in the P&L is the strongest signal there is
    pnl = str(res.get("fx_pnl_figures", "")).strip()
    if pnl and pnl.lower() != "not disclosed":
        return "exchange figure disclosed"

    hedge = str(res.get("hedging_instruments", "")).strip().lower()
    # The field records what the filing said about instruments, which includes
    # saying there are none. Caterite reads "none evident - accounts state they
    # have not entered into any hedging arrangements", and that is a denial,
    # not evidence. Nil values are the same trap.
    denies = (hedge.startswith(("none", "not disclosed", "no hedging"))
              or "not entered into any" in hedge
              or "does not hedge" in hedge
              or re.search(r"[£$\u20ac]?\s?nil\b", hedge))
    if hedge and not denies:
        return "hedging instruments disclosed"

    cur = str(res.get("currencies_named", "")).strip()
    if cur and cur.lower() != "not disclosed" and not _XBRL_NOISE.search(cur) \
       and _CCY.search(cur):
        return "foreign currencies named"

    exp = str(res.get("export_split", "")).strip()
    if exp and exp.lower() != "not disclosed" and re.search(r"\d", exp) \
       and not _UK_ONLY.search(exp):
        return "export split disclosed"

    if FX_STRICT:
        return ""

    est = str(res.get("est_fx_volume", "")).strip()
    if est.upper().startswith("EST"):
        return "FX volume estimated from the filing"

    blob = " ".join(str(res.get(k, "")) for k in
                    ("call_ammo", "excerpts", "findings", "one_liner", "triggers"))
    if _UK_ONLY.search(blob):
        return ""
    if _CCY.search(blob) and _FX_WORDS.search(blob):
        return "currency and trade language in the accounts"
    if re.search(r"import|export", str(res.get("one_liner", "")), re.I):
        return "importer or exporter by description"
    return ""



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
    # A thin filing is not the same as a thin prospect. Small-regime companies
    # still disclose real figures: itsu grocery reported a £42k exchange gain,
    # forwards in use and over 30% of purchases in each of USD and EUR, and
    # still landed in P4 because the filing withheld everything else. A
    # quantified currency figure or a real forward holding outranks that.
    if has_quantified_fx(row):
        return "P3 - established hedger" if holds_instruments(row) \
               else "P1 - exposed, no cover"

    # No positive evidence of currency flow means this cannot be P1 or P2.
    # Absence of hedging is not evidence of exposure.
    if not has_currency_flow(row):
        cat = str(row.get("accounts_category", "")).lower()
        if cat in ("full", "medium", "group"):
            # Full accounts and still nothing about currency means there is
            # probably nothing there.
            return "X - no currency flow evident"
        if cat in ("micro", "dormant") or str(row.get("error", "")):
            # Never readable in the first place, so leave it where it was.
            return "X - no evidence"
        return "P4 - thin filing, qualify by phone"

    # NOTE: fx_evidence is the gate and returns a reason string, not a
    # category. The old category function was _fx_category. Mixing them made
    # every comparison below silently false, which is how J. Barbour & Sons
    # ended up at X while passing the gate.
    ev = _fx_category(row) if "_fx_category" in globals() else (
        "quantified" if has_quantified_fx(row)
        else "stated" if fx_evidence(row)
        else "none")
    pos, conf, t = fx_position(row), confidence(row), _turnover(row)

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
    # The gate and the priority must not contradict each other. J. Barbour &
    # Sons discloses a currency hedging strategy in operation and an estimated
    # £15-25m exposure, passes fx_evidence, and still fell through to X because
    # no single confidence test caught it. If the filing carries currency
    # evidence, the floor is P4, never X.
    if fx_evidence(row):
        if holds_instruments(row) or "hedger" in str(row.get("sophistication", "")).lower():
            return "P3 - established hedger"
        return "P4 - thin filing, qualify by phone"

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
