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
   "At Lumon we don't just try to beat your existing providers on margins. We
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
  Lead on the figure. The point is that the number moved without anyone
  deciding it should, and the next one is a coin toss.
  e.g. "That is a number nobody chose. It moved because the rate moved, on
  goods you had already priced."

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
  "not only... but also", "isn't just... it's"
  rhetorical questions used as filler
  three-item lists used for rhythm rather than meaning
  starting consecutive sentences the same way
  any sentence that could appear in an email to any company

Write the way a person types when they have read something and have one
question about it. Short sentences. Plain words. If a sentence sounds like it
was written to sound good, cut it.

Return JSON only, no other text:
{"subject": "...", "body": "..."}
Use \n\n between paragraphs. The subject must be under nine words, must not
contain FX, foreign exchange, currency risk or hedging, and should quote their
own number or their own wording where possible."""


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
   "At Lumon we don't just try to beat your existing providers on margins. We
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
Subject under nine words, no FX, foreign exchange, currency risk or hedging."""


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
    (r"\bisn'?t just\b[^.]{0,40}\bit'?s\b", "isn't just, it's"),
    (r"\b(Monday|Tuesday|Wednesday|Thursday|Friday)\b", "weekday, no dates in bulk"),
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


def confidence(row: dict) -> str:
    """Flag rows worth reading before they go out."""
    has_number = bool(re.search(r"\d", str(row.get("fx_pnl_figures", ""))
                                + str(row.get("hedging_instruments", ""))
                                + str(row.get("export_split", ""))))
    if not str(row.get("call_ammo", "")).strip():
        return "review - nothing from the accounts"
    if not str(row.get("turnover", "")).strip():
        return "review - no turnover"
    return "ok" if has_number else "review - no hard figure"


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
    by_name = {}
    for rec in cache.values():
        k = norm(rec.get("company", ""))
        if k and (k not in by_name or str(rec.get("turnover", "")).strip()):
            by_name[k] = rec

    # triage anything not already read
    missing = [c for c in contacts if norm(c["company"]) not in by_name]
    if missing and not args.no_triage:
        print(f"{len(missing)} companies not yet read, triaging them now", flush=True)
        import ch_engine as eng
        for i, c in enumerate(missing, 1):
            try:
                res = eng.process_company(name=c["company"], do_brief=True)
            except Exception as exc:
                res = eng.blank_result(c["company"])
                res["error"] = f"crashed: {exc}"
            by_name[norm(c["company"])] = res
            print(f"  [{i}/{len(missing)}] {c['company'][:44]:46} "
                  f"{res.get('grade') or res.get('error','')[:30]}", flush=True)
    elif missing:
        print(f"{len(missing)} not in the cache, skipping (--no-triage)", flush=True)

    wanted = None if args.priority.upper() == "ALL" else tuple(
        p.strip() for p in args.priority.split(",") if p.strip())

    rows, skipped, failed = [], 0, 0
    for i, c in enumerate(contacts, 1):
        acct = by_name.get(norm(c["company"]))
        if not acct:
            skipped += 1
            continue
        pri = ch_classify.priority(acct) if HAS_CLASSIFY else ""
        if wanted and not str(pri).startswith(wanted):
            skipped += 1
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
        body = contract("\n\n".join(paras)) + "\n\nKind regards,"

        rows.append({
            "company": acct.get("company", c["company"]),
            "name": c["name"], "first_name": first, "email": c["email"],
            "title": c["title"], "phone": c["phone"],
            "greeting": f"Hi {first}," if first else "Hi,",
            "subject": out.get("subject", "").strip(),
            "body": body,
            "priority": pri,
            "turnover": acct.get("turnover", ""),
            "ch_number": acct.get("number", ""),
            "check": "; ".join(find_tells(out.get("subject", "") + " " + body))
                     or ("thin - sector email, no figures" if thin
                         else confidence(acct)),
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
    print(f"  failed (model error): {failed}")
    flagged = [r for r in rows if r["check"] != "ok"]
    if flagged:
        print(f"\n{len(flagged)} worth reading before sending:")
        for r in flagged[:12]:
            print(f"   {r['company'][:40]:42} {r['check']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
