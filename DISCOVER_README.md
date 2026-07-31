# Building your own prospect lists

## Why

The last Lusha pull was 374 contacts, of which 234 (63%) were companies already
in your files. The sub-industry tag was "Food & Beverage Retail", which returned
cafes and restaurants. Only 18 of the 374 read as wholesale or import, and every
one of those was already held.

Companies House has an advanced search that filters on SIC code, location,
status and incorporation date. It is free, unlimited, and covers every UK
company. That is a better source of *companies* than any paid list.

Lusha is still the right tool for *contacts*. The change is that you stop paying
it to find companies.

## Twelve verticals mapped, 233 SIC codes

food and beverage, food wholesale, fabricated metal, industrial machinery,
chemicals, plastics and rubber, textiles and apparel, furniture and timber,
electronics and electrical, glass and ceramics, transport equipment,
wholesale import export.

## Use

```python
import ch_discover as cd

rows = cd.discover(
    vertical="fabricated metal",
    location="Birmingham",
    max_results=300,
    min_age_years=8,      # skips recently incorporated companies
)
cd.write_run_list(rows, "ch_run_birmingham_metal.csv")
```

That CSV goes straight into the puller. It carries the company number as well
as the name, so no name matching is needed and the S H Pratt problem of three
similarly named entities does not arise.

`min_age_years` defaults to 8. A business with £5m of currency moving through it
is rarely three years old, and the trade-list exercise showed how much noise
young companies add.

Only active companies are returned, so the 18 dissolved and liquidating
companies that previous runs downloaded and OCR'd would never have appeared.

## Suggested first runs

Based on where your qualified names have actually come from:

| Vertical | Location |
|---|---|
| food wholesale | (leave blank, national) |
| fabricated metal | Birmingham |
| fabricated metal | Sheffield |
| industrial machinery | Manchester |
| wholesale import export | Leicester |
| transport equipment | Coventry |

Run one, triage it, see the qualification rate before scaling up.

## Caveat

The paging, deduplication, age filter and CSV output are tested against a
mocked API. The live endpoint has not been called from here, because this
sandbox cannot reach Companies House. Run one small search first and check the
results look sane before firing off a batch.

Advanced search uses the same `CH_API_KEY` as the rest of the puller.
