#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Update publications.bib by merging ORCID (primary) + Crossref (BibTeX) + optional Google Scholar
with manual entries from data/manual.bib.

Environment variables:
  ORCID_ID=0000-0002-5238-4590
  SCHOLAR_ID=TnUmf84AAAAJ            # optional
  USE_SCHOLAR=true|false              # default false
  MAX_YEARS_BACK=0                    # 0 means no limit; else filter older works

Usage:
  python scripts/update_publications.py
"""

import os
import re
import json
import time
import html
import string
import logging
from collections import OrderedDict, defaultdict
from datetime import datetime

import requests
from unidecode import unidecode

# Optional imports wrapped to avoid hard failure when USE_SCHOLAR is false
try:
    from scholarly import scholarly
    SCHOLARLY_AVAILABLE = True
except Exception:
    SCHOLARLY_AVAILABLE = False

try:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    from bibtexparser.customization import convert_to_unicode
except ImportError as e:
    raise SystemExit("Please install dependencies: pip install requests bibtexparser unidecode scholarly")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

ORCID_ID = os.getenv("ORCID_ID", "0000-000-0000-0000").strip()
SCHOLAR_ID = os.getenv("SCHOLAR_ID", "").strip()
USE_SCHOLAR = os.getenv("USE_SCHOLAR", "false").lower() in {"1", "true", "yes", "on"}
MAX_YEARS_BACK = int(os.getenv("MAX_YEARS_BACK", "0") or "0")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
MANUAL_BIB_PATH = os.path.join(ROOT, "data", "manual.bib")
OUTPUT_BIB_PATH = os.path.join(ROOT, "publications.bib")


# --------------------------- Utilities ---------------------------

def normalize_title(t: str) -> str:
    t = unidecode(t or "")
    t = t.lower()
    t = re.sub(r"\s+", " ", t)
    t = t.translate(str.maketrans("", "", string.punctuation))
    return t.strip()

def safe_get(d, *keys, default=None):
    for k in keys:
        if d is None:
            return default
        d = d.get(k)
    return d if d is not None else default

def extract_year(date_str: str) -> int | None:
    if not date_str:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", date_str)
    return int(m.group()) if m else None

def choose_bibtype(orcid_type: str, fallback="misc") -> str:
    m = (orcid_type or "").lower()
    # ORCID work types (sample mapping)
    if "journal-article" in m or m == "article":
        return "article"
    if "book" in m and "chapter" not in m:
        return "book"
    if "book-chapter" in m or "book-chapter" in m:
        return "incollection"
    if "conference" in m or "proceedings" in m or "paper-conference" in m:
        return "inproceedings"
    if "dataset" in m:
        return "dataset"
    if "software" in m or "computer-program" in m:
        return "software"
    if "report" in m or "working-paper" in m:
        return "techreport"
    if "thesis" in m or "dissertation" in m:
        return "phdthesis"
    if "preprint" in m:
        return "unpublished"
    return fallback

def build_bibkey(first_author: str, year: str | int | None, title: str, existing_keys: set[str]) -> str:
    if not first_author:
        first = "anon"
    else:
        # Use last token as surname, remove accents/punct
        tokens = unidecode(first_author).split()
        first = re.sub(r"[^A-Za-z0-9]", "", tokens[-1] if tokens else "anon").lower() or "anon"
    yr = str(year) if year else datetime.now().year
    short = re.sub(r"[^A-Za-z0-9]", "", unidecode(title).lower())
    short = short[:12] if short else "title"
    base = f"{first}{yr}{short}"
    key = base
    i = 1
    while key in existing_keys:
        i += 1
        key = f"{base}{i}"
    existing_keys.add(key)
    return key

def make_bib_entry(entry_type: str, key: str, fields: dict) -> dict:
    fields = {k: v for k, v in fields.items() if v}
    return {"ENTRYTYPE": entry_type, "ID": key, **fields}


# --------------------------- Data Sources ---------------------------

def fetch_orcid_works(orcid_id: str) -> list[dict]:
    """
    Returns a list of dicts with basic metadata, including DOI, title, year, type, journal, authors (if available).
    """
    headers = {"Accept": "application/json"}
    url = f"https://pub.orcid.org/v3.0/{orcid_id}/works"
    logging.info(f"Fetching ORCID works: {url}")
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()

    works = []
    groups = data.get("group", []) or []
    for g in groups:
        summaries = g.get("work-summary", []) or []
        for s in summaries:
            put_code = s.get("put-code")
            # Fetch full work to get contributors, journal title, etc.
            work_url = f"https://pub.orcid.org/v3.0/{orcid_id}/work/{put_code}"
            wr = requests.get(work_url, headers=headers, timeout=30)
            if wr.status_code == 404:
                continue
            wr.raise_for_status()
            w = wr.json()

            # Extract DOI
            doi = None
            ext_ids = safe_get(w, "external-ids", "external-id", default=[]) or []
            for eid in ext_ids:
                typ = safe_get(eid, "external-id-type", default="")
                if str(typ).lower() == "doi":
                    doi = safe_get(eid, "external-id-value", default=None)
                    break

            # Title
            title = safe_get(w, "title", "title", "value", default="").strip()

            # Journal / container
            journal = safe_get(w, "journal-title", "value", default=None)

            # Year
            y = safe_get(w, "publication-date", "year", "value") or safe_get(w, "publication-date", "year") or None
            year = int(y) if y and str(y).isdigit() else extract_year(json.dumps(safe_get(w, "publication-date", default={})) or "")

            # Type
            work_type = safe_get(w, "type", default="")

            # URL
            url_val = safe_get(w, "url", "value", default=None)

            # Authors
            authors = []
            contributors = safe_get(w, "contributors", "contributor", default=[]) or []
            for c in contributors:
                name = safe_get(c, "credit-name", "value") or safe_get(c, "contributor-orcid", "path")
                if name:
                    authors.append(name)

            works.append({
                "doi": doi,
                "title": title,
                "journal": journal,
                "year": year,
                "type": work_type,
                "url": url_val,
                "authors": authors,
            })

            time.sleep(0.1)  # be polite
    logging.info(f"ORCID works fetched: {len(works)}")
    return works

def crossref_bibtex_from_doi(doi: str) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    if doi.lower().startswith("doi:"):
        doi = doi[4:].strip()
    url = f"https://doi.org/{doi}"
    headers = {"Accept": "application/x-bibtex; charset=UTF-8"}
    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        if r.status_code == 200 and r.text.strip():
            return r.text
    except Exception as e:
        logging.warning(f"Crossref DOI fetch failed for {doi}: {e}")
    return None

def crossref_bibtex_by_title(title: str) -> str | None:
    """
    Last-resort attempt: search Crossref by bibliographic title and return first hit as BibTeX.
    """
    if not title:
        return None
    qs = title.strip()
    url = "https://api.crossref.org/works"
    try:
        r = requests.get(url, params={"query.bibliographic": qs, "rows": 1}, timeout=30)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", []) or []
        if items:
            first = items[0]
            doi = first.get("DOI")
            if doi:
                return crossref_bibtex_from_doi(doi)
    except Exception as e:
        logging.warning(f"Crossref search failed for title: {title[:80]}... ({e})")
    return None

def scholar_bibs(scholar_id: str) -> list[dict]:
    """
    Optional: Use scholarly to fetch publications (title/authors/year/venue).
    Returns partial bib dicts (no DOI).
    """
    if not SCHOLARLY_AVAILABLE or not scholar_id:
        return []
    logging.info("Fetching publications from Google Scholar (best-effort)...")
    try:
        author = scholarly.search_author_id(scholar_id)
        author = scholarly.fill(author, sections=["publications"])
        pubs = []
        for p in author.get("publications", []):
            full = scholarly.fill(p)
            bib = full.get("bib", {})
            title = bib.get("title")
            year = bib.get("pub_year") or extract_year(bib.get("pub_year", ""))
            authors = bib.get("author", "")
            if isinstance(authors, str):
                authors_list = [a.strip() for a in authors.split(" and ")] if " and " in authors else [authors]
            else:
                authors_list = authors or []
            venue = bib.get("venue")
            pubs.append({
                "doi": None,
                "title": title,
                "journal": venue,
                "year": int(year) if year and str(year).isdigit() else None,
                "type": "article",
                "url": None,
                "authors": authors_list,
            })
        logging.info(f"Scholar pubs fetched: {len(pubs)}")
        return pubs
    except Exception as e:
        logging.warning(f"Google Scholar fetch failed: {e}")
        return []

# --------------------------- BibTeX IO ---------------------------

def load_bib(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        parser = BibTexParser(common_strings=True)
        parser.customization = convert_to_unicode
        db = bibtexparser.load(f, parser=parser)
        return db.entries or []

def save_bib(path: str, entries: list[dict]) -> None:
    db = bibtexparser.bibdatabase.BibDatabase()
    db.entries = entries
    writer = bibtexparser.bwriter.BibTexWriter()
    writer.order_entries_by = None
    writer.indent = "  "
    writer.comma_first = False
    with open(path, "w", encoding="utf-8") as f:
        f.write(writer.write(db))

# --------------------------- Merge & Build ---------------------------

def parse_bibtex_text_to_entry(bibtex_text: str) -> dict | None:
    try:
        db = bibtexparser.loads(bibtex_text)
        if db.entries:
            return db.entries[0]
    except Exception as e:
        logging.warning(f"BibTeX parse failed: {e}")
    return None

def entry_key(entry: dict) -> str:
    """Canonical dedup key: DOI if present; else normalized title + year."""
    doi = (entry.get("doi") or entry.get("DOI") or "").lower().strip()
    if doi:
        return f"doi::{doi}"
    title = normalize_title(entry.get("title", ""))
    year = str(entry.get("year") or "")
    return f"title::{title}::year::{year}"

def merge_fields(primary: dict, secondary: dict, prefer_existing=True) -> dict:
    """Merge two bib entries, preserve rich/manual fields."""
    out = dict(secondary) if prefer_existing else dict(primary)
    for k, v in (primary.items() if prefer_existing else secondary.items()):
        if k not in out or not out[k]:
            out[k] = v
    return out

def build_entries(orcid_items: list[dict], scholar_items: list[dict], manual_entries: list[dict]) -> list[dict]:
    # Map & dedupe with priority: ORCID/Crossref > Scholar > Manual
    entries_by_key: OrderedDict[str, dict] = OrderedDict()
    existing_bibkeys: set[str] = set()

    # 1) Pull ORCID items, convert to BibTeX via DOI (or title as fallback)
    for it in orcid_items:
        bib = None
        if it.get("doi"):
            bibtex_text = crossref_bibtex_from_doi(it["doi"])
            bib = parse_bibtex_text_to_entry(bibtex_text) if bibtex_text else None
        if not bib:
            # Fallback by title (best-effort)
            bibtex_text = crossref_bibtex_by_title(it.get("title", ""))
            bib = parse_bibtex_text_to_entry(bibtex_text) if bibtex_text else None

        if not bib:
            # Construct a minimal bib entry
            etype = choose_bibtype(it.get("type"))
            first_author = (it.get("authors") or [""])[0]
            key = build_bibkey(first_author, it.get("year"), it.get("title") or "untitled", existing_bibkeys)
            fields = {
                "title": it.get("title"),
                "year": str(it.get("year")) if it.get("year") else None,
                "journal": it.get("journal"),
                "url": it.get("url"),
                "author": " and ".join(it.get("authors") or []),
            }
            bib = make_bib_entry(etype, key, fields)
            # Include DOI field if known
            if it.get("doi"):
                bib["doi"] = it["doi"]

        k = entry_key(bib)
        entries_by_key[k] = merge_fields(bib, entries_by_key.get(k, {}), prefer_existing=True)

    # 2) Add Scholar items (if enabled), only where we don't already have a match
    if USE_SCHOLAR and scholar_items:
        for it in scholar_items:
            etype = choose_bibtype(it.get("type"))
            first_author = (it.get("authors") or [""])[0]
            key = build_bibkey(first_author, it.get("year"), it.get("title") or "untitled", existing_bibkeys)
            fields = {
                "title": it.get("title"),
                "year": str(it.get("year")) if it.get("year") else None,
                "journal": it.get("journal"),
                "url": it.get("url"),
                "author": " and ".join(it.get("authors") or []),
            }
            bib = make_bib_entry(etype, key, fields)
            k = entry_key(bib)
            if k not in entries_by_key:
                entries_by_key[k] = bib

    # 3) Merge manual entries (preserve manual extras like keywords, file, abstract)
    for m in manual_entries:
        k = entry_key(m)
        if k in entries_by_key:
            # Merge fields; preserve existing (auto) core, add manual extras where missing
            entries_by_key[k] = merge_fields(entries_by_key[k], m, prefer_existing=True)
        else:
            entries_by_key[k] = m

    # 4) Normalize: ensure every entry has an ID and ENTRYTYPE
    finalized = []
    for e in entries_by_key.values():
        entrytype = e.get("ENTRYTYPE") or choose_bibtype(e.get("type", ""))
        # Build/normalize bibkey if missing
        key = e.get("ID")
        fa = ""
        if "author" in e and e["author"]:
            fa = e["author"].split(" and ")[0]
        key = key or build_bibkey(fa, e.get("year"), e.get("title") or "untitled", existing_bibkeys)
        e["ENTRYTYPE"] = entrytype or "misc"
        e["ID"] = key
        finalized.append(e)

    # 5) Filter by year if MAX_YEARS_BACK > 0
    if MAX_YEARS_BACK > 0:
        current_year = datetime.now().year
        def _within(e):
            try:
                y = int(str(e.get("year", "")).strip())
                return (current_year - y) <= MAX_YEARS_BACK
            except Exception:
                return True
        finalized = [e for e in finalized if _within(e)]

    # 6) Sort: year desc, then title
    def sort_key(e):
        try:
            y = int(str(e.get("year", "")))
        except Exception:
            y = -9999
        t = normalize_title(e.get("title", ""))
        return (-y, t)

    finalized.sort(key=sort_key)
    return finalized

# --------------------------- Main ---------------------------

def main():
    if ORCID_ID == "0000-000-0000-0000":
        raise SystemExit("Please set ORCID_ID environment variable (e.g., 0000-0002-5238-4590)")

    manual_entries = load_bib(MANUAL_BIB_PATH)
    orcid_items = fetch_orcid_works(ORCID_ID)
    scholar_items = scholar_bibs(SCHOLAR_ID) if USE_SCHOLAR else []

    entries = build_entries(orcid_items, scholar_items, manual_entries)
    save_bib(OUTPUT_BIB_PATH, entries)
    logging.info(f"Wrote {len(entries)} entries to {OUTPUT_BIB_PATH}")

if __name__ == "__main__":
    main()