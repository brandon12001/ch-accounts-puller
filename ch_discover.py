"""Build prospect lists straight from Companies House, instead of buying them.

The Lusha pulls have been expensive and poorly targeted. The last one was 374
contacts of which 234, or 63%, were names already held, and the sub-industry
tag returned cafes and restaurants rather than importers.

Companies House exposes an advanced search that filters on SIC code, location,
company status and incorporation date. It is free and it covers every company
in the UK. That is a better source of *companies* than any paid list. Lusha is
still needed for *contacts*, which is what it is actually good at.

Typical use:

    names = discover(vertical="fabricated metal", location="Birmingham",
                     max_results=300)
    write_run_list(names, "ch_run_birmingham_metal.csv")

Then feed that CSV into the puller as normal.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

import requests

BASE = "https://api.company-information.service.gov.uk"

# UK SIC 2007 prefixes for the Tier 1 verticals. Prefixes are used rather than
# full codes because Companies House matches on the full five digits, and
# listing every one would be unreadable.
VERTICALS: dict[str, list[str]] = {
    "food and beverage": [
        "10110", "10120", "10130", "10200", "10310", "10320", "10390", "10410",
        "10511", "10512", "10519", "10520", "10611", "10612", "10620", "10710",
        "10720", "10730", "10820", "10830", "10840", "10850", "10860", "10890",
        "11010", "11020", "11030", "11040", "11050", "11070",
    ],
    "food wholesale": [
        "46310", "46320", "46330", "46341", "46342", "46350", "46360", "46370",
        "46380", "46390", "46170",
    ],
    "fabricated metal": [
        "24100", "24200", "24310", "24320", "24330", "24340", "24410", "24420",
        "24430", "24440", "24450", "25110", "25120", "25210", "25290", "25300",
        "25400", "25500", "25610", "25620", "25710", "25720", "25730", "25910",
        "25920", "25930", "25940", "25990",
    ],
    "industrial machinery": [
        "28110", "28120", "28130", "28140", "28150", "28210", "28220", "28230",
        "28240", "28250", "28290", "28301", "28302", "28410", "28490", "28910",
        "28920", "28930", "28940", "28950", "28960", "28990",
    ],
    "chemicals": [
        "20110", "20120", "20130", "20140", "20150", "20160", "20170", "20200",
        "20301", "20302", "20411", "20412", "20420", "20510", "20520", "20530",
        "20590", "20600",
    ],
    "plastics and rubber": [
        "22110", "22190", "22210", "22220", "22230", "22290",
    ],
    "textiles and apparel": [
        "13100", "13200", "13300", "13910", "13921", "13922", "13923", "13931",
        "13939", "13940", "13950", "13960", "13990", "14110", "14120", "14130",
        "14140", "14190", "14200", "14310", "14390",
    ],
    "furniture and timber": [
        "16100", "16210", "16220", "16230", "16240", "16290", "31010", "31020",
        "31030", "31090",
    ],
    "electronics and electrical": [
        "26110", "26120", "26200", "26301", "26309", "26400", "26511", "26512",
        "26513", "26520", "26600", "26701", "26702", "26800", "27110", "27120",
        "27200", "27310", "27320", "27330", "27400", "27510", "27520", "27900",
    ],
    "glass and ceramics": [
        "23110", "23120", "23130", "23140", "23190", "23200", "23310", "23320",
        "23410", "23420", "23430", "23440", "23490",
    ],
    "transport equipment": [
        "29100", "29201", "29202", "29203", "29310", "29320", "30110", "30120",
        "30200", "30300", "30400", "30910", "30920", "30990",
    ],
    "wholesale import export": [
        "46110", "46120", "46130", "46140", "46150", "46160", "46180", "46190",
        "46410", "46420", "46431", "46439", "46440", "46450", "46460", "46470",
        "46480", "46491", "46499", "46510", "46520", "46610", "46620", "46630",
        "46640", "46650", "46660", "46690", "46710", "46720", "46730", "46740",
        "46750", "46760", "46770", "46900",
    ],
    # ---- added 25/08/2026 -------------------------------------------------
    # The original twelve have each had a full pass and the qualification rate
    # has fallen from 12% to 7%, which is what a worked-out vertical looks like.
    # These eight are untouched and chosen because the goods are imported or
    # exported directly rather than through a UK middleman, which is what the
    # FX gate needs in order to find anything.
    "seafood and fish": [
        "03110", "03120", "03210", "03220",   # fishing and aquaculture
        "10200",                              # processing fish, crustaceans
        "46380",                              # wholesale of fish
    ],
    "pharma and medical": [
        "21100", "21200",                     # pharmaceutical manufacture
        "32500",                              # medical and dental instruments
        "26600",                              # irradiation and electromedical
        "46460",                              # wholesale of pharmaceutical goods
        "72110",                              # biotech research
    ],
    "automotive parts": [
        "29310", "29320",                     # parts and accessories
        "45310", "45320",                     # wholesale and retail of parts
        "22190",                              # rubber products, tyres
        "28150",                              # bearings, gears, drive elements
    ],
    "building products": [
        "23610", "23620", "23630", "23640", "23650", "23690",  # concrete, cement
        "16230",                              # builders carpentry and joinery
        "25110", "25120",                     # metal structures, doors, windows
        "46730",                              # wholesale of wood and materials
        "43320", "43330",                     # joinery and floor installation
    ],
    "agriculture and horticulture": [
        "01110", "01130", "01190", "01250",   # growing crops
        "01610", "01620",                     # support activities
        "20150", "20200",                     # fertiliser, agrochemicals
        "46210", "46220",                     # wholesale of grain, flowers
        "28300",                              # agricultural machinery
    ],
    "paper and print": [
        "17110", "17120", "17211", "17219",   # pulp, paper, corrugated
        "17220", "17230", "17240", "17290",
        "18110", "18121", "18129", "18130",   # printing
        "46760",                              # wholesale of other intermediate
    ],
    "energy and environmental": [
        "27110", "27120", "27200",            # motors, generators, batteries
        "28110",                              # engines and turbines
        "35110", "35140",                     # electricity generation and trade
        "38210", "38320",                     # waste treatment, recovery
        "42220",                              # utility projects
    ],
    # Added 26/08. International staffing invoices clients in one currency and
    # pays contractors in another, every payroll cycle, on both sides. That is
    # a continuous two-sided flow rather than an occasional import, and these
    # are among Lumon's best existing clients. No recruitment SIC code was in
    # any vertical, so discovery had never looked for them.
    "recruitment and staffing": [
        "78100",          # activities of employment placement agencies
        "78101",          # motion picture, TV and other theatrical casting
        "78109",          # other activities of employment placement agencies
        "78200",          # temporary employment agency activities
        "78300",          # human resources provision and management
        "70229",          # management consultancy, catches search firms
        "82911",          # activities of collection agencies
    ],
    "marine and offshore": [
        "30110", "30120",                     # shipbuilding, pleasure craft
        "33150",                              # repair of ships and boats
        "50100", "50200",                     # sea and coastal transport
        "52220",                              # service activities for water transport
        "09100",                              # support for petroleum extraction
    ],

}


def _key() -> str:
    import os
    return os.environ.get("CH_API_KEY", "")


def advanced_search(
    sic_codes: list[str] | None = None,
    location: str = "",
    name_includes: str = "",
    status: str = "active",
    incorporated_to: str = "",
    size: int = 100,
    start_index: int = 0,
    session: requests.Session | None = None,
) -> tuple[list[dict], int]:
    """One page of Companies House advanced search. Returns (items, total_hits)."""
    params: dict[str, object] = {"size": min(size, 5000), "start_index": start_index}
    if sic_codes:
        params["sic_codes"] = sic_codes
    if location:
        params["location"] = location
    if name_includes:
        params["company_name_includes"] = name_includes
    if status:
        params["company_status"] = status
    if incorporated_to:
        params["incorporated_to"] = incorporated_to

    get = (session or requests).get
    r = get(f"{BASE}/advanced-search/companies", params=params,
            auth=(_key(), ""), timeout=30)
    if r.status_code != 200:
        return [], 0
    j = r.json()
    out = []
    for item in j.get("items", []) or []:
        addr = item.get("registered_office_address", {}) or {}
        out.append({
            "name": item.get("company_name", ""),
            "number": item.get("company_number", ""),
            "status": item.get("company_status", ""),
            "incorporated": item.get("date_of_creation", ""),
            "sic_codes": ", ".join(item.get("sic_codes", []) or []),
            "locality": addr.get("locality", ""),
            "postcode": addr.get("postal_code", ""),
            "region": addr.get("region", ""),
        })
    return out, int(j.get("hits", 0) or 0)


def discover(
    vertical: str = "",
    sic_codes: list[str] | None = None,
    location: str = "",
    max_results: int = 500,
    min_age_years: int = 8,
    pause: float = 0.35,
) -> list[dict]:
    """Pull candidate companies for a vertical and optional location.

    `min_age_years` filters out recently incorporated companies. A business with
    £5m of currency flowing through it is very rarely three years old, and the
    trade-list exercise showed how much noise young shell-like companies add.
    """
    codes = list(sic_codes or [])
    if vertical:
        key = vertical.strip().lower()
        if key not in VERTICALS:
            raise ValueError(f"unknown vertical {vertical!r}. "
                             f"Options: {', '.join(sorted(VERTICALS))}")
        codes += VERTICALS[key]
    if not codes:
        raise ValueError("give either a vertical or explicit sic_codes")

    cutoff = ""
    if min_age_years:
        cutoff = time.strftime("%Y-%m-%d",
                               time.gmtime(time.time() - min_age_years * 365.25 * 86400))

    seen: set[str] = set()
    out: list[dict] = []
    with requests.Session() as s:
        start = 0
        while len(out) < max_results:
            page, hits = advanced_search(
                sic_codes=codes, location=location, incorporated_to=cutoff,
                size=min(100, max_results - len(out)), start_index=start, session=s,
            )
            if not page:
                break
            for row in page:
                if row["number"] and row["number"] not in seen:
                    seen.add(row["number"])
                    out.append(row)
            start += len(page)
            if start >= hits:
                break
            time.sleep(pause)          # CH allows 600 requests per 5 minutes
    return out


def write_run_list(rows: list[dict], path: str | Path, include_detail: bool = True) -> Path:
    """Write a CSV the puller can read. `name` first so the existing loader works."""
    p = Path(path)
    cols = ["name", "number", "locality", "postcode", "incorporated", "sic_codes"]
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        if include_detail:
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, "") for c in cols])
        else:
            w.writerow(["name"])
            for r in rows:
                w.writerow([r.get("name", "")])
    return p
