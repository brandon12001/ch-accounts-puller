# FX Prospecting Platform — Companies House Triage (v4)

A local web app over your existing CH triage engine. Single-search or bulk-search
companies, confirm the right match (no more wrong-company pulls), read the accounts,
score FX exposure, and get an AI call brief — all in a browser tab on your own machine.

## What's new vs the v3.1 script

- **Runs on demand in a browser**, not as a GitHub Action. No stale re-run footgun:
  you choose the input every time, so a fresh run is the only kind of run.
- **Smart match-and-skip.** A name search only pulls accounts when the top hit is
  confidently the right company. Trivial differences ("Ltd", "Limited", "Group",
  "The", punctuation) are ignored. A genuinely different company (shares only a
  common word like "Eden") is **skipped and flagged**, not guessed.
- **Single search shows a shortlist to confirm** before anything is pulled.
- **Bulk search auto-accepts** only confident matches and quarantines the rest for
  a quick manual pass.
- **v4 extras:** model-extracted turnover, A–D FX-volume grade (intensity × size),
  and an overseas-parent flag (so you catch the Ritter/Musgrave situation before dialling).

The fetch / OCR / FX-scan / brief logic is unchanged from v3.1 — it's the same engine,
now importable and wrapped in a UI.

## One-time setup

You need Python 3.10+ and Tesseract/Poppler for the OCR fallback.

```bash
# system deps for OCR (Ubuntu/Debian; on Mac use: brew install tesseract poppler)
sudo apt-get update && sudo apt-get install -y tesseract-ocr poppler-utils

# python deps
pip install streamlit pandas requests pdfplumber pdf2image pytesseract beautifulsoup4 gspread google-auth
```

Set your keys (put these in your shell profile so they persist):

```bash
export CH_API_KEY="your_companies_house_key"
export ANTHROPIC_API_KEY="your_anthropic_key"   # briefs are skipped without this
```

## Run it

```bash
cd ch-platform
streamlit run app.py
```

It opens `http://localhost:8501` in your browser. That's the whole app.

## Using it

- **Single search** — type a name, pick the right company from the shortlist, get the
  brief inline. Or search by company number to skip matching entirely (fastest, and
  never ambiguous — capture the number once from a single search, reuse it forever).
- **Bulk search** — paste names (one per line) or upload a CSV with a `name` column
  (a `number` column is used when present). Confident matches run automatically;
  skipped ones are listed at the end for you to search by hand.
- **Results** — everything from this session, filterable by grade/sophistication,
  sortable, downloadable as CSV.

## Notes

- Keep the "accept weak matches" box **off** by default. A skipped company costs you
  one manual search; a wrong match costs you a whole call on the wrong figures.
- Always sanity-check `matched_name` before quoting figures on a call.
- Restricted sectors (firearms, cannabis, adult, radioactive) surface in `red_flags`
  — the brief is told to flag them.
