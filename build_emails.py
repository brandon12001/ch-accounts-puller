#!/usr/bin/env python3
"""Contact list in, bespoke merge emails out.

Upload a CSV of contacts and this does the whole chain:

  1. matches each contact to the accounts already in the cache
  2. runs the triage on any company that has not been read yet
  3. writes one email per contact from that company's own filed figures
  4. exports a merge CSV for Word

It does not send. That is deliberate: sending from a script bypasses the
Salesforce BCC and the Outlook signature carrying the regulatory footer, both
of which have to be on every email. Word and Outlook do the sending.

Input needs a company column. Name and email columns are used when present:

    company,name,email
    Meadow Vale Foods Ltd,Nigel O'Donnell,nigelo@meadowvalefoods.co.uk

    python build_emails.py --contacts leads.csv --out merge.csv
    python build_emails.py --contacts leads.csv --no-triage   # cached only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

# The rules that took a fortnight of drafts to settle on. They go in the prompt
# rather than being applied afterwards, because a model that knows the
# constraints writes better prose than one that gets edited into shape.
SYSTEM = """You write cold emails for Brandon Ellis, a senior sales executive at
Lumon, an FX brokerage. He emails MDs and finance directors of UK businesses
that trade in foreign currency.

THE SHAPE, in order, always five parts:

1. One paragraph quoting one or two specific things from their own filed
   accounts. Facts and figures, not adjectives.
2. One short paragraph, two or three lines, turning that fact into the question.
   This paragraph changes depending on what they do about currency. See the
   branches below.
3. The Lumon paragraph, close to verbatim:
   "At Lumon we don't just try to beat your existing providers on pricing. We
   build bespoke hedging strategies that minimise risk and maximise flexibility
   and upside potential, with no deposits or margin calls."
4. The conditional close, worded exactly like this:
   "If I could show you a way to protect against the downside on that exposure,
   while still participating when the market moves in your favour, would that be
   worth a conversation?"
   Use "that exposure" verbatim. Don't substitute "that buying", "your euro
   purchases" or anything else.
5. "Kind regards," on its own line. Nothing after it.

BRANCHES for paragraph 2. Pick the one that fits the account:

IF they hold no instruments and an exchange gain or loss is disclosed:
  Lead on the figure, then say plainly what it was: the rate moving on goods
  already priced. State it, do not dramatise it.
  e.g. "That came from the rate moving between agreeing the price and paying
  for the goods." Never write "a number nobody chose", "that wasn't a decision",
  "that's not a rounding error" or any line written for effect.

IF they hold no instruments and no figure is disclosed:
  The exposure itself is the story. Price agreed in one currency, paid in
  another, months apart, and the difference lands on the margin.

IF their policy uses discretionary wording, "where appropriate", "selective",
"when the Board considers it appropriate":
  That wording is the story. Cover that happens when somebody decides it should
  is a judgement each time rather than a policy, and the part sitting outside it
  is usually the part nobody measures.

IF they already hold forwards:
  Don't pitch hedging, they already do it. The instrument is the story, not the
  exposure. Use this argument, in your own words but keeping the sense:
  companies often hedge at a rate that looks good at the time, then the market
  moves to a more favourable position through the life of the contract and they
  are left trading at a worse rate than their competitors.
  e.g. "What we see with businesses already using forwards is that the rate
  looked right on the day it was booked. Then the market moves, and for the rest
  of the contract you are trading at a worse rate than the people you compete
  with."

IF they manage currency by holding foreign currency accounts:
  That handles the timing of when money moves. It does not set the rate they
  acquire the currency at, and that is where the cost sits.

IF they have taken a deliberate action on the trading side, changed suppliers,
opened a new market, invested in capacity:
  Credit them for it, then position currency as the other half of the same
  problem. e.g. "Securing new suppliers says you are already acting on the steel
  side of that. The exchange rate side is where we come in."

IF cover has fallen while the business grew, or cover fell year on year:
  Treat them as an existing hedger and use the hedger argument. The rate looked
  right on the day it was booked, then the market moves and for the rest of the
  contract they trade at a worse rate than the people they compete with. Mention
  the movement in cover as the fact in paragraph one, not as a criticism.

IF their policy permits forward contracts but none are held:
  Don't say the policy is unused or that nobody owns it. Ask the consequence
  question instead: what a five or ten percent move against them would do to
  margins or pricing.
  e.g. "If the market moved five or ten percent against you over a buying cycle,
  does that come out of margin, or does it go into your pricing?"

IF there has been an acquisition, a change of ownership, or a disposal:
  M&A changes what the currency requirement looks like. Say that plainly and
  position Lumon as working with businesses through it.
  e.g. "Acquisitions usually change what the currency requirement looks like,
  different suppliers, different volumes, sometimes a different currency
  altogether. We work with businesses going through that, not just to execute
  the trades but to build the strategy around what the requirement has become."

IF they manage currency by matching receipts against payments, or by holding
foreign currency accounts, a natural hedge:
  Matching only ever covers the overlap. Whatever is left over, the excess
  sitting in an account or the shortfall they go out and buy, is fully exposed.
  Ask how that difference is handled.

IF their own report names falling revenue, falling margin, or margin
compression:
  Name it, using their figures, and position a better currency strategy as one
  of the levers available. The point is the ability to take a better rate when
  one appears rather than being locked into a poor one for the life of a
  contract.
  e.g. "Gross margin came back from X to Y. Currency is one of the few lines on
  that where you can change the outcome, if the structure lets you take a better
  rate when one appears rather than holding a poor one to maturity."

IF what they buy or sell is a commodity priced in dollars, metals, resin,
timber, coffee, grain, rubber:
  The input cost moves with the dollar whether or not a foreign invoice is ever
  paid. State it as a fact about the commodity, not as something they have
  overlooked.
  e.g. "Copper is priced in dollars on the LME, so the sterling cost of your
  input moves with the dollar regardless of who invoices you and in what
  currency."

ABSOLUTE RULES:

- Blank line between every paragraph.
- Never open with "I am getting in touch about foreign currency" or a variant.
  Open on their accounts.
- Never sell on price, rates, or being cheaper.
- No meeting dates, no days of the week. The close is a question.
- No name after "Kind regards," and no regulatory footer.
- British English.
- Every money figure carries its symbol. Write "£413,686" not "413,686", in the
  body and in the subject line. Use the currency the accounts state, so euro
  figures take a euro sign.
- Use contractions the way a person typing an email does: don't, doesn't, won't,
  you're, we're, it's, that's, I've, you've, there's, isn't, hasn't, wasn't.
  Writing "do not" and "you are" in full is the clearest sign an email was
  machine-written. Contract by default and only write it out where the emphasis
  genuinely needs it.
- Never suggest they have overlooked, ignored or failed to consider anything.
  Never write "most companies treat this as", "many businesses do not realise",
  "what nobody measures" or any variant. State facts about their accounts and
  ask questions. The reader has thought about their own business.

BANNED WORDS AND PHRASES, these read as machine-written:
  em dashes, en dashes, semicolons
  "I hope this finds you well", "I wanted to reach out", "I noticed that"
  "In today's", "In an increasingly", "landscape", "navigate", "leverage"
  "streamline", "robust", "seamless", "unlock", "empower", "delve", "tapestry"
  "it's worth noting", "that said", "moreover", "furthermore", "additionally"
  "crucial", "vital", "pivotal", "game-changer", "transformative"
  "Let's face it", "The reality is", "Here's the thing"
  "not only... but also"
  the whole contrastive family: "that's not X, it's Y", "this isn't about X,
    it's about Y", "it's not the rate, it's the timing", "less about X, more
    about Y", "not so much X as Y". Say the thing you mean and stop. If you
    find yourself setting up a contrast to make a point land, cut the first
    half and keep the second.
  rhetorical questions used as filler
  three-item lists used for rhythm rather than meaning
  short dramatic sentences written for effect: "That is a number nobody chose",
    "That was not a decision", "That is not a rounding error", "And that is the
    problem", "Which is the point"
  any sentence whose job is emphasis rather than information
  starting consecutive sentences the same way
  any sentence that could appear in an email to any company

Write the way a person types when they have read something and have one
question about it. Short sentences. Plain words. If a sentence sounds like it
was written to sound good, cut it.

Return JSON only, no other text:
{"subject": "...", "body": "..."}
Use \n\n between paragraphs.

THE SUBJECT LINE. It decides whether the email is opened, so it carries the one
thing a stranger could not know: something from their own accounts.

- Under nine words.
- Quote their own figure or their own wording wherever there is one.
  Good: "The 339,678 in your FY25 accounts"
  Good: "Your two month cover and Baltic lead times"
  Good: "Your euro forwards and what happens after you book"
- Never a question mark. Questions in subject lines read as marketing.
- Never the company name. "Arden Fine Foods, a quick question" is the shape
  every mail merge takes and people recognise it instantly.
- Never FX, foreign exchange, currency risk, hedging, or the word Lumon.
- No greeting words, no "quick question", no "following up", no "opportunity".
- Sentence case, not title case. Lower case after the first word."""


THIN_SYSTEM = """You write cold emails for Brandon Ellis, a senior sales executive
at Lumon, an FX brokerage.

This company files accounts too thin to prove anything about currency, so you
have almost nothing specific. Do not invent figures, do not guess at currencies,
and do not claim to have read their accounts.

Four paragraphs, in this order:

1. What they do and the sector, then the general observation. Close to this
   wording:
   "I work with businesses like yours in [sector]. Quite often when companies
   are paying overseas suppliers or receiving international payments, movements
   in exchange rates end up affecting margins."
   Adapt the middle clause to whichever fits: paying overseas suppliers,
   receiving payments from overseas customers, or both.

2. The Lumon paragraph, close to verbatim:
   "At Lumon we don't just try to beat your existing providers on pricing. We
   build bespoke hedging strategies that minimise risk and maximise flexibility
   and upside potential, with no deposits or margin calls."

3. The conditional close, worded exactly:
   "If I could show you a way to protect against the downside on that exposure,
   while still participating when the market moves in your favour, would that be
   worth a conversation?"

4. "Kind regards," on its own line. Nothing after it.

Rules: blank line between paragraphs. No em dashes, no semicolons. No dates or
weekdays. No name after the sign-off. British English. Use contractions, don't
and you're rather than do not and you are. Never suggest they have overlooked
anything. Keep it under 100 words before the sign-off, because there
is nothing specific to justify length.

Return JSON only:
{"subject": "...", "body": "..."}
Subject under nine words. No question mark, no company name, no FX, foreign
exchange, currency risk, hedging or the word Lumon. Sentence case. Describe the
flow rather than the product, e.g. "Paying suppliers overseas"."""


def _richness(rec: dict) -> int:
    """How useful a cache record is, for picking between duplicates."""
    score = 0
    for f, w in (("call_ammo", 3), ("turnover", 2), ("one_liner", 1),
                 ("hedging_instruments", 1), ("currencies_named", 1)):
        if str(rec.get(f, "")).strip():
            score += w
    return score


def squash(s: str) -> str:
    """Normalised with all spaces removed.

    Salesforce and Companies House disagree constantly about spacing and
    initials: MJAllen against M.J. ALLEN, Meadowvale against Meadow Vale,
    Eurowrap against EURO WRAP, PJ Nicholls against P.J. NICHOLLS. Collapsing
    to a single token makes all of those the same string.
    """
    return norm(s).replace(" ", "")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\b(ltd|limited|plc|llp|uk|holdings?|group|company|co|the|and|"
               r"international|intl|europe|european)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def call_claude(payload: str, retries: int = 3, system: str = "") -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return None
    body = json.dumps({
        "model": MODEL, "max_tokens": 900, "system": system or SYSTEM,
        "messages": [{"role": "user", "content": payload}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            text = "".join(b.get("text", "") for b in data.get("content", []))
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            return json.loads(text)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(2 * (attempt + 1))
    return None


def brief_for(row: dict) -> str:
    """Everything the model needs about one company, and nothing else."""
    fields = [
        ("Company", row.get("company")),
        ("What they do", row.get("one_liner")),
        ("Turnover", row.get("turnover")),
        ("Currencies named", row.get("currencies_named")),
        ("Hedging instruments held", row.get("hedging_instruments")),
        ("Exchange gains or losses disclosed", row.get("fx_pnl_figures")),
        ("Export split", row.get("export_split")),
        ("Estimated FX volume", row.get("est_fx_volume")),
        ("Position", row.get("sophistication")),
        ("From their accounts", row.get("call_ammo")),
        ("Recent events", row.get("triggers")),
    ]
    out = []
    for label, v in fields:
        v = str(v or "").strip()
        if v and v.lower() not in ("not disclosed", "none", "no", "nan"):
            out.append(f"{label}: {v}")
    return "\n".join(out)


# Rules in a prompt are guidance, not a guarantee. These are checked on the way
# out so a tell that slips through gets flagged rather than sent.
TELLS = [
    (r"[\u2014\u2013]", "em or en dash"),
    (r";", "semicolon"),
    (r"\bI hope this (finds|email finds)\b", "hope this finds you well"),
    (r"\bI wanted to reach out\b", "wanted to reach out"),
    (r"\bIn today'?s\b|\bIn an increasingly\b", "in today's"),
    (r"\blandscape\b|\bnavigat|\bleverag|\bstreamlin|\bseamless\b|\bunlock\b|"
     r"\bempower|\bdelve\b|\btapestry\b|\brobust\b", "corporate filler"),
    (r"\bit'?s worth noting\b|\bthat said\b|\bmoreover\b|\bfurthermore\b|"
     r"\badditionally\b", "connective filler"),
    (r"\bcrucial\b|\bvital\b|\bpivotal\b|\bgame.chang|\btransformative\b",
     "inflated adjective"),
    (r"\bLet'?s face it\b|\bThe reality is\b|\bHere'?s the thing\b", "false opener"),
    (r"not only\b[^.]{0,60}\bbut also\b", "not only but also"),
    # The whole "not X, it's Y" family. It is the most recognisable machine
    # construction there is, and it turns up in a dozen shapes.
    (r"\b(that|this|it|which)('?s| is| was)? ?(not|isn'?t|wasn'?t) "
     r"(just |simply |only |really |about )?[^.;!?]{2,50}[,.] ?(it'?s|that'?s|"
     r"they'?re|this is|it is)\b", "not X it's Y construction"),
    (r"\b(isn'?t|aren'?t|wasn'?t|is not) (just|simply|only) about\b[^.]{0,50}"
     r"\b(it'?s|but) about\b", "not just about, it's about"),
    (r"\bnot (so much|as much)\b[^.]{0,40}\b(as|but)\b", "not so much as"),
    (r"\bless about\b[^.]{0,40}\bmore about\b", "less about, more about"),
    (r"\bit'?s not\b[^.]{0,40}\bthat matters\b", "it's not X that matters"),
    (r"\b(Monday|Tuesday|Wednesday|Thursday|Friday)\b", "weekday, no dates in bulk"),
    (r"\b(that|this|it)('?s| is| was| wasn'?t| isn'?t)? ?(a )?number nobody|"
     r"\bnobody (chose|decided|picked)|that (was|is)n'?t a decision|"
     r"not a rounding error|and that('?s| is) the (problem|point)|"
     r"which is (the|exactly the) point", "dramatic line written for effect"),
    (r"most (companies|businesses)|many (companies|businesses) (do not|don'?t)|"
     r"nobody (measures|owns|decides)|often overlook|tend to overlook|"
     r"fail to (realise|consider)|may not (realise|be aware)",
     "implies they have overlooked something"),
]


# Expanding contractions is the single most reliable tell of machine writing, so
# it is fixed on the way out rather than left to the prompt. Only forms that are
# unambiguous in context are contracted: "we are" is safe, "it is" before a noun
# is not always, so it is left alone.
CONTRACTIONS = [
    (r"\bdo not\b", "don't"), (r"\bdoes not\b", "doesn't"),
    (r"\bdid not\b", "didn't"), (r"\bis not\b", "isn't"),
    (r"\bare not\b", "aren't"), (r"\bwas not\b", "wasn't"),
    (r"\bwere not\b", "weren't"), (r"\bhas not\b", "hasn't"),
    (r"\bhave not\b", "haven't"), (r"\bhad not\b", "hadn't"),
    (r"\bwill not\b", "won't"), (r"\bwould not\b", "wouldn't"),
    (r"\bcould not\b", "couldn't"), (r"\bshould not\b", "shouldn't"),
    (r"\bcannot\b", "can't"), (r"\bcan not\b", "can't"),
    (r"\byou are\b", "you're"), (r"\bwe are\b", "we're"),
    (r"\bthey are\b", "they're"), (r"\byou have\b", "you've"),
    (r"\bwe have\b", "we've"), (r"\bI have\b", "I've"),
    (r"\bI am\b", "I'm"), (r"\bthat is\b", "that's"),
    (r"\bthere is\b", "there's"), (r"\bwhat is\b", "what's"),
    (r"\bwe will\b", "we'll"), (r"\byou will\b", "you'll"),
]


# The model drops currency symbols surprisingly often, and a bare "413,686"
# in a subject line looks like a mistake. Money-shaped numbers get the symbol
# put back, unless something already precedes them.
MONEY = re.compile(r"(?<![\d£$\u20ac.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+m\b)")


def add_symbol(text: str, symbol: str = "\u00a3") -> str:
    def swap(m):
        before = text[max(0, m.start() - 12):m.start()].lower()
        # leave alone anything that is plainly not money
        if any(w in before for w in ("company number", "registered", "no. ", "fy",
                                     "year ", "20")):
            return m.group(0)
        return symbol + m.group(0)
    return MONEY.sub(swap, text)


def contract(text: str) -> str:
    """Contract expanded forms, preserving the case of the first letter."""
    def swap(m):
        rep = m.group(0)
        for pat, r in CONTRACTIONS:
            if re.fullmatch(pat, m.group(0), re.I):
                rep = r
                break
        return rep[0].upper() + rep[1:] if m.group(0)[0].isupper() else rep

    for pat, _ in CONTRACTIONS:
        text = re.sub(pat, swap, text, flags=re.I)
    return text


def find_tells(text: str) -> list[str]:
    import re as _re
    return [label for pat, label in TELLS if _re.search(pat, text, _re.I)]


def check_subject(subject: str, company: str) -> list[str]:
    """The subject decides whether it is opened, so it gets its own checks."""
    out = []
    if "?" in subject:
        out.append("question mark in subject")
    if len(subject.split()) > 11:
        out.append("subject too long")
    if re.search(r"\b(fx|foreign exchange|currency risk|hedging|lumon)\b", subject, re.I):
        out.append("banned word in subject")
    # Only flag when the subject is actually addressing them by name, not when
    # it happens to reuse a word: "Timber priced in dollars" is fine for North
    # West Timber Treatments, "Revive! UK turnover up 20%" is not.
    words = [w for w in re.sub(r"[^a-z ]", " ", company.lower()).split()
             if len(w) > 4 and w not in
             ("group", "limited", "services", "solutions", "international",
              "holdings", "trading", "supplies", "systems", "products")]
    low = subject.lower()
    lead = low.split()[0] if low.split() else ""
    hits = sum(1 for w in words if w in low)
    if hits >= 2 or (words and lead == words[0]):
        out.append("company name in subject")
    return out


def confidence(row: dict, body: str = "") -> str:
    """Flag rows worth reading before they go out.

    Judged on the email that was actually written, not on which cache fields
    happen to be populated. An email quoting three figures from the accounts is
    fine even if fx_pnl_figures was blank.
    """
    if not str(row.get("call_ammo", "")).strip():
        return "review - nothing from the accounts"
    figures = len(re.findall(r"[\u00a3\u20ac$]\s?\d", body))
    if figures == 0:
        return "review - no figures in the email"
    if figures == 1 and not str(row.get("turnover", "")).strip():
        return "review - one figure, no turnover on file"
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contacts", required=True, type=Path)
    ap.add_argument("--cache", type=Path, default=Path("ch_results_cache.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("merge.csv"))
    ap.add_argument("--no-triage", action="store_true",
                    help="skip companies not already in the cache")
    ap.add_argument("--priority", default="P1,P2,P3",
                    help="only write emails for these, or ALL")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-cache-ok", action="store_true",
                    help="proceed even if the cache is empty")
    ap.add_argument("--include-thin", action="store_true",
                    help="also write short sector emails for P4 and X companies")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    import ch_cache
    try:
        import ch_classify
        HAS_CLASSIFY = True
    except ImportError:
        HAS_CLASSIFY = False

    # read contacts, tolerating whatever the export called its columns
    contacts = []
    with args.contacts.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            co = low.get("company") or low.get("company name") or low.get("account") or ""
            if not co:
                continue
            contacts.append({
                "company": co,
                "name": low.get("name") or low.get("contact") or low.get("contact name") or "",
                "email": low.get("email") or low.get("work email") or "",
                "title": low.get("title") or low.get("job title") or "",
                "phone": low.get("phone") or low.get("mobile") or "",
            })
    if args.limit:
        contacts = contacts[: args.limit]
    print(f"{len(contacts)} contacts", flush=True)

    cache = ch_cache.load_cache(args.cache)
    if not cache:
        print(f"WARNING: no records loaded from {args.cache}.", flush=True)
        print("Either the file is missing from the repo or the commit step failed "
              "on the triage run. Every company will be re-triaged, which costs "
              "time and credits for work already done.", flush=True)
        if not args.no_cache_ok:
            print("Stopping. Re-run with --no-cache-ok to triage from scratch "
                  "anyway.", flush=True)
            return 1
    else:
        companies = len({id(r) for r in cache.values()})
        print(f"cache holds {companies} companies", flush=True)

    # Index every way a contact might refer to the same company. The cache
    # stores both the name we searched and the name Companies House matched,
    # and those often differ ("Top Tubes Ltd" vs "TOP TUBES LIMITED").
    by_name: dict[str, dict] = {}
    by_number: dict[str, dict] = {}

    def index(key: str, rec: dict) -> None:
        if not key:
            return
        # prefer the richer record when the same key appears twice
        old = by_name.get(key)
        if old is None or _richness(rec) > _richness(old):
            by_name[key] = rec

    for rec in cache.values():
        num = str(rec.get("number", "")).strip()
        if num:
            by_number[num] = rec
        for field in ("company", "matched_name"):
            index(norm(rec.get(field, "")), rec)
            index(squash(rec.get(field, "")), rec)
        # also index without the leading "the", and on the first two words,
        # which catches "Lawton Tubes" against "LAWTON TUBES LIMITED"
        base = norm(rec.get("company", ""))
        if base.startswith("the "):
            index(base[4:], rec)
        words = base.split()
        if len(words) > 2:
            index(" ".join(words[:2]), rec)


    def lookup(company: str, number: str = "") -> dict | None:
        if number and number.strip() in by_number:
            return by_number[number.strip()]
        k = norm(company)
        if k in by_name:
            return by_name[k]
        if squash(company) in by_name:
            return by_name[squash(company)]
        if k.startswith("the ") and k[4:] in by_name:
            return by_name[k[4:]]
        words = k.split()
        # try progressively shorter prefixes, then a unique substring match
        for n in range(len(words) - 1, 1, -1):
            cand = " ".join(words[:n])
            if cand in by_name:
                return by_name[cand]
        if len(k) > 6:
            hits = [v for kk, v in by_name.items()
                    if kk.startswith(k) or k.startswith(kk)]
            uniq = {id(h) for h in hits}
            if len(uniq) == 1:
                return hits[0]
        return None

    # triage anything not already read
    matched = sum(1 for c in contacts if lookup(c["company"], c.get("number", "")))
    print(f"{matched}/{len(contacts)} contacts matched to cached accounts", flush=True)
    missing = [c for c in contacts if not lookup(c["company"], c.get("number", ""))]
    if missing and not args.no_triage:
        print(f"{len(missing)} companies not yet read, triaging them now", flush=True)
        import ch_engine as eng
        for i, c in enumerate(missing, 1):
            try:
                res = eng.process_company(name=c["company"], do_brief=True)
            except Exception as exc:
                res = eng.blank_result(c["company"])
                res["error"] = f"crashed: {exc}"
            for field in ("company", "matched_name"):
                k = norm(res.get(field, "")) or norm(c["company"])
                if k:
                    by_name[k] = res
            if res.get("number"):
                by_number[str(res["number"])] = res
            print(f"  [{i}/{len(missing)}] {c['company'][:44]:46} "
                  f"{res.get('grade') or res.get('error','')[:30]}", flush=True)
    elif missing:
        print(f"{len(missing)} not in the cache, skipping (--no-triage)", flush=True)

    wanted = None if args.priority.upper() == "ALL" else tuple(
        p.strip() for p in args.priority.split(",") if p.strip())

    rows, skipped, failed, blocked_owner, no_fx = [], 0, 0, 0, 0
    for i, c in enumerate(contacts, 1):
        acct = lookup(c["company"], c.get("number", ""))
        if not acct:
            skipped += 1
            continue
        pri = ch_classify.priority(acct) if HAS_CLASSIFY else ""
        if wanted and not str(pri).startswith(wanted):
            skipped += 1
            continue
        # A listed plc or a company under a foreign parent will not have the
        # decision sitting with the person on the contact record, so there is
        # no point writing to them however good the accounts look.
        if HAS_CLASSIFY and not ch_classify.winnable(acct):
            blocked_owner += 1
            continue
        # No currency evidence in the filing means there is nothing to write
        # about, and writing anyway produces an email that argues with the
        # accounts. Cheaper to skip than to generate and bin.
        if HAS_CLASSIFY and not ch_classify.fx_evidence(acct):
            no_fx += 1
            continue

        brief = brief_for(acct)
        thin = str(pri).startswith(("P4", "X"))
        if thin:
            # Nothing quotable, so send only what is safe to assert: what they
            # do and the sector. Needs at least that much to be worth sending.
            what = str(acct.get("one_liner", "")).strip()
            if not what or not args.include_thin:
                skipped += 1
                continue
            brief = f"Company: {acct.get('company','')}\nWhat they do: {what}"
        elif not brief.strip():
            skipped += 1
            continue

        first = c["name"].split()[0] if c["name"] else ""
        out = call_claude(brief, system=THIN_SYSTEM if thin else "")
        if not out or not out.get("body"):
            failed += 1
            continue

        # em dashes are usually mid-sentence, so a comma replaces them cleanly.
        # Strip any space that was sitting before the dash, or the comma floats.
        body = re.sub(r"\s*[\u2014\u2013]\s*", ", ", out["body"]).strip()
        # The model is told not to sign off, but strip anything that slips
        # through. Repeat until nothing more comes off, since a sign-off is
        # usually two lines: the valediction and then the name.
        SIGNOFF = re.compile(
            r"\n\s*(kind regards|best regards|many thanks|best wishes|regards|"
            r"best|thanks|brandon[\w\s]*|lumon[\w\s]*)[,.]?\s*$", re.I)
        while True:
            trimmed = SIGNOFF.sub("", body).strip()
            if trimmed == body:
                break
            body = trimmed
        # normalise spacing: exactly one blank line between paragraphs, then
        # the sign-off on its own line
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        paras = [p for p in paras if p.lower().rstrip(",.") not in
                 ("kind regards", "best regards", "regards", "best")]
        cur = "\u20ac" if re.search(r"\beur\b|euro", str(acct.get("currencies_named", "")), re.I) \
              and not re.search(r"\bgbp\b|sterling", str(acct.get("turnover", "")), re.I) else "\u00a3"
        body = add_symbol(contract("\n\n".join(paras))) + "\n\nKind regards,"

        rows.append({
            "company": acct.get("company", c["company"]),
            "name": c["name"], "first_name": first, "email": c["email"],
            "title": c["title"], "phone": c["phone"],
            "greeting": f"Hi {first}," if first else "Hi,",
            "subject": add_symbol(out.get("subject", "").strip()),
            "body": body,
            "priority": pri,
            "turnover": acct.get("turnover", ""),
            "ch_number": acct.get("number", ""),
            "check": "; ".join(find_tells(body)
                               + check_subject(out.get("subject", ""),
                                               acct.get("company", "")))
                     or ("thin - sector email, no figures" if thin
                         else confidence(acct, body)),
            "template": "sector" if thin else "accounts",
        })
        if i % 10 == 0:
            print(f"  written {len(rows)}/{i}", flush=True)
        time.sleep(0.4)

    if rows:
        with args.out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(f"\n{len(rows)} emails written to {args.out}")
    print(f"  skipped (no accounts data or wrong priority): {skipped}")
    print(f"  skipped (listed, too large, or parent controls FX): {blocked_owner}")
    print(f"  skipped (no currency evidence in the filing): {no_fx}")
    print(f"  failed (model error): {failed}")
    flagged = [r for r in rows if r["check"] != "ok"]
    if flagged:
        print(f"\n{len(flagged)} worth reading before sending:")
        for r in flagged[:12]:
            print(f"   {r['company'][:40]:42} {r['check']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
