"""Append-only result cache for the Companies House puller.

Every run so far has died partway: 552 stopped at 389, 135 stopped at 29,
106 stopped at 28. Each time the whole batch was lost and had to be paid for
again, in time and in Anthropic spend.

This stores each company the moment it completes, one JSON object per line.
Append-only means a run killed mid-write loses at most the record in flight,
and a re-run skips everything already done instead of repeating it.

Keyed on company number where known, falling back to a normalised name.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

CACHE_PATH = Path("ch_results_cache.jsonl")
_LOCK = threading.Lock()
_MEM: dict[str, dict[str, Any]] | None = None

_SUFFIX = re.compile(
    r"\b(ltd|limited|plc|llp|lp|uk|holdings?|group|company|co|the|and|intl|"
    r"international|services?|solutions?|trading)\b"
)


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def cache_key(name: str = "", number: str = "") -> str:
    num = str(number or "").strip().upper()
    if num:
        return f"n:{num}"
    stem = _norm(name)
    return f"s:{stem}" if stem else ""


def _keys_for(rec: dict[str, Any]) -> list[str]:
    keys = []
    if rec.get("number"):
        keys.append(cache_key(number=rec["number"]))
    if rec.get("company"):
        k = cache_key(name=rec["company"])
        if k:
            keys.append(k)
    return keys


def load_cache(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Read the cache into memory once. Corrupt lines are skipped, not fatal."""
    global _MEM
    if _MEM is not None:
        return _MEM
    p = path or CACHE_PATH
    out: dict[str, dict[str, Any]] = {}
    if p.exists():
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue          # half-written line from a killed run
                # index under BOTH keys: a resume looks up by name, while a
                # re-check of a known company looks up by number
                for key in _keys_for(rec):
                    out[key] = rec    # later entries win, so a re-run overwrites
    _MEM = out
    return out


def cache_get(name: str = "", number: str = "", path: Path | None = None):
    """Return a cached result, preferring a company-number hit over a name hit."""
    cache = load_cache(path)
    if number:
        hit = cache.get(cache_key(number=number))
        if hit:
            return hit
    return cache.get(cache_key(name=name)) if name else None


def cache_put(result: dict[str, Any], path: Path | None = None) -> None:
    """Append one completed company. Called immediately, not at the end of a batch."""
    p = path or CACHE_PATH
    key = cache_key(result.get("company", ""), result.get("number", ""))
    if not key:
        return
    rec = dict(result)
    rec["_key"] = key
    with _LOCK:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        cache = load_cache(path)
        for k in _keys_for(rec):
            cache[k] = rec


def pending(names: list[str], path: Path | None = None) -> list[str]:
    """Filter a run list down to the companies not already done."""
    cache = load_cache(path)
    return [n for n in names if cache_key(name=n) not in cache]


def cache_stats(path: Path | None = None) -> dict[str, int]:
    cache = load_cache(path)
    vals = {id(v): v for v in cache.values()}.values()
    return {
        "companies": len(vals),
        "with_turnover": sum(1 for v in vals if str(v.get("turnover", "")).strip()),
        "errors": sum(1 for v in vals if str(v.get("error", "")).strip()),
    }


def reset_memory() -> None:
    """Drop the in-memory copy, forcing a re-read. Mainly for tests."""
    global _MEM
    _MEM = None
