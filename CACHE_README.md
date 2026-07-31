# Resume, caching and two new fields

## What this fixes

Every batch so far has died partway and lost everything:

| Run | Submitted | Completed |
|---|---|---|
| Trade list | 552 | 389 |
| Failed PDFs | 135 | 29 |
| Continuation | 106 | 28 |

Each restart re-fetched, re-OCR'd and re-billed Anthropic for work already done.

## Files

**`ch_cache.py`** (new). Append-only JSONL cache, one line per company, written
the moment that company completes. A run killed mid-write loses at most the
record in flight. Corrupt lines are skipped on load rather than being fatal.

Records are indexed under both the company number and a normalised company
name, so a resume by name and a re-check by number both hit.

**`ch_engine.py`** (updated).

- `process_company` checks the cache first and returns immediately on a hit.
  Two new arguments: `use_cache=True` and `force=False`. Cached rows come back
  with `from_cache = yes`.
- Every exit path writes to the cache, including error paths, so a company that
  fails is not retried pointlessly.
- Rows whose only error is a scanned-PDF failure are **not** served from cache,
  so anything blocked by the old poppler bug will be re-read rather than
  returning the old blank.
- New `company_profile(number)`: one cheap call returning registered office,
  status and SIC codes.
- **Dead companies are skipped before any document is fetched.** Dissolved,
  liquidation, receivership, administration. Eighteen such companies had their
  filings downloaded and OCR'd across previous runs.
- Four new output columns: `address`, `postcode`, `company_status`,
  `sic_codes`. The address one matters most: runs can now be sorted by region,
  calls clustered by area, and visits planned.

## Using it

Nothing to change in the app for the basic behaviour. Re-run any list and
completed companies return instantly.

To filter a run list down to what is outstanding:

```python
import ch_cache
todo = ch_cache.pending(list_of_names)
```

To force a re-read of one company:

```python
process_company(name="Best Foods Ltd", force=True)
```

To see what is stored:

```python
ch_cache.cache_stats()   # {'companies': n, 'with_turnover': n, 'errors': n}
```

## Note on Streamlit Community Cloud

The cache file lives in the app's working directory, which is wiped on
redeploy. It survives session drops, browser closes and CPU throttling, which
are what has actually been killing your runs, but not a rebuild. Download
`ch_results_cache.jsonl` before redeploying, or run the batches locally or on
GitHub Actions where the file persists properly.
