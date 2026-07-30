# Fixing "ocr failed: Unable to get page count. Is poppler installed and in PATH?"

## What was wrong

Companies House serves many older filings as image-only scans. Those have no
text layer, so `pdfplumber` returns nothing and the code falls through to OCR.
OCR used `pdf2image`, which shells out to `pdfinfo` from the **poppler-utils**
apt package. Streamlit Community Cloud does not install apt packages unless a
`packages.txt` file is present, so every scanned PDF failed.

Across two runs that silently killed roughly 224 companies, including S H Pratt,
John Hornby Skewes, Falcon Coffees and Tropifruit.

## The fix, three files

**1. `packages.txt`** (new file, repo root, next to requirements.txt)

Streamlit Cloud reads this at build time and installs the apt packages listed.
`tesseract-ocr` is required: pytesseract is only a wrapper and needs the real
binary. `poppler-utils` is now optional but harmless to keep.

**2. `requirements.txt`** (updated)

Adds `pypdfium2`, plus the packages the engine already imports but which were
missing from the file: pdfplumber, pytesseract, beautifulsoup4, lxml.

**3. `ch_engine.py`** (updated `text_from_pdf`)

Rendering now tries **pypdfium2** first, which ships its own binary inside the
Python wheel and needs no apt packages at all, then falls back to pdf2image.
So a missing poppler install is no longer fatal.

Error strings now name the stage that failed, e.g.
`scanned pdf, tesseract binary missing?` rather than a raw exception.

`read_method` now reports `ocr-pdfium` or `ocr-poppler` instead of just `ocr`,
so you can see which path ran.

## Deploying

1. Add all three files to the repo root of `ch-accounts-puller`.
2. Streamlit Cloud only reads `packages.txt` on a rebuild. Pushing is not
   enough. Go to Manage app, then Reboot. Expect a slower first build.
3. Test on five names before running a batch:
   S H PRATT GROUP LIMITED, John Hornby Skewes & Co, Falcon Coffees,
   Tropifruit UK Limited, Browns More Hair Now Limited
4. Check the `read_method` column. `ocr-pdfium` or `text-layer` means fixed.

## Still worth doing separately

- Cache `process_company` on company number so a dropped session costs nothing
- Write results to disk as each company completes, not only at the end
- Add the registered office address to the output so runs can be filtered by region
