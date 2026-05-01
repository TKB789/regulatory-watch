#!/usr/bin/env python3
"""
scrape_sites.py — fetches HTML pages from regulators that don't publish RSS,
parses out news/notification items, and returns them in the same shape as the
RSS fetcher (so they can be merged into the same feed.json).

This is best-effort. Government sites change layout without warning, so each
scraper logs an error and returns an empty list rather than crashing.

Maintenance reality:
- Run this monthly to verify each scraper still works.
- If a scraper returns 0 items for a week, the site layout probably changed.
- The CSS selectors at the top of each function are the things you'll edit.

Run by GitHub Actions on a schedule. Free, no API keys needed.
"""

import re
import socket
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Hard timeout for any single network request
socket.setdefaulttimeout(20)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RegulatoryWatchBot/1.0; "
        "+https://github.com/regulatory-watch) feed-aggregator"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

DEVICE_KEYWORDS = [
    "device", "devices", "510(k)", "pma", "udi", "ivd", "diagnostic",
    "implant", "mdr", "ivdr", "premarket", "post-market", "postmarket",
    "recall", "vigilance", "samd", "software as a medical",
    "in vitro", "instrument", "surgical", "medtech", "medical equipment",
    "clinical investigation", "notification", "import", "register",
]

# Words that suggest a sentence describes an actual regulatory CHANGE,
# rather than boilerplate. Higher-weighted words come first.
CHANGE_SIGNALS = [
    # Strong action verbs (weight 3)
    ("amends", 3), ("amended", 3), ("repeals", 3), ("repealed", 3),
    ("replaces", 3), ("replaced", 3), ("withdraws", 3), ("withdrawn", 3),
    ("revokes", 3), ("revoked", 3), ("supersedes", 3),
    ("now requires", 3), ("now mandates", 3), ("introduces", 3),
    # Effective-date / deadline language (weight 3)
    ("effective", 3), ("comes into force", 3), ("takes effect", 3),
    ("deadline", 3), ("expires", 3), ("transition period", 3),
    ("must be submitted", 3), ("must comply", 3),
    # Modal obligations (weight 2)
    ("shall", 2), ("must", 2), ("required to", 2), ("obligated to", 2),
    ("mandatory", 2), ("prohibited", 2), ("not permitted", 2),
    # Subject-matter relevance (weight 1)
    ("manufacturer", 1), ("authorisation", 1), ("authorization", 1),
    ("approval", 1), ("classification", 1), ("registration", 1),
    ("recall", 1), ("safety alert", 1), ("notification", 1),
    ("guidance", 1), ("standard", 1), ("compliance", 1),
]


def is_device_related(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in DEVICE_KEYWORDS)


def extract_body_text(soup) -> str:
    """Strip nav/script/style/footer and return clean body text."""
    if not soup:
        return ""
    # Remove non-content elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "form", "noscript", "iframe"]):
        tag.decompose()
    # Prefer the main content area if marked
    main = (soup.find("main")
            or soup.find("article")
            or soup.find("div", id=re.compile(r"content|main", re.I))
            or soup.find("div", class_=re.compile(r"content|main|body", re.I))
            or soup.body
            or soup)
    text = main.get_text(separator=" ", strip=True)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text


def split_sentences(text: str) -> list:
    """Naive sentence splitter — good enough for our purposes."""
    # Split on . ! ? followed by space + capital, or newline
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'\d])", text)
    return [p.strip() for p in parts if 25 <= len(p.strip()) <= 350]


def score_sentence(sentence: str) -> int:
    """Higher score = more likely to describe a real regulatory change."""
    s = sentence.lower()
    score = 0
    for word, weight in CHANGE_SIGNALS:
        if word in s:
            score += weight
    # Slight penalty for boilerplate-sounding phrases
    if "this notification" in s or "this guidance" in s or "this document" in s:
        score -= 1
    if "issued under" in s or "in accordance with" in s:
        score -= 1
    # Bonus for containing a date (suggests a deadline or effective date)
    if re.search(r"\b(20\d{2}|january|february|march|april|may|june|july|august|september|october|november|december)\b", s, re.I):
        score += 1
    return score


def heuristic_summary(body_text: str, max_sentences: int = 3, max_chars: int = 500) -> str:
    """
    Pick the top-scoring sentences from a body of text and return them
    in their original order, capped to max_chars.
    """
    if not body_text:
        return ""
    sentences = split_sentences(body_text)
    if not sentences:
        # Fall back to the first chunk of the body
        return body_text[:max_chars].rsplit(" ", 1)[0] + "…"

    # Score every sentence, pair with its index so we can preserve order
    scored = [(score_sentence(s), idx, s) for idx, s in enumerate(sentences)]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Take the top N by score, then sort those back into reading order
    top = sorted(scored[:max_sentences], key=lambda x: x[1])
    chosen = [s for _, _, s in top if _ > 0]  # only keep ones with positive score

    if not chosen:
        # Nothing scored well — return a polite fallback
        return sentences[0][:max_chars]

    summary = " ".join(chosen)
    if len(summary) > max_chars:
        summary = summary[:max_chars - 1].rsplit(" ", 1)[0] + "…"
    return summary


def normalize_item(*, agency, region, category, source, title, link, summary="", published=None):
    """Return a dict in the same shape as the RSS fetcher."""
    return {
        "title": title.strip(),
        "summary": (summary or "").strip()[:500],
        "link": link,
        "published": published or datetime.now(timezone.utc).isoformat(),
        "agency": agency,
        "region": region,
        "category": category,
        "source": source,
    }


def safe_get(url, timeout=15):
    """Fetch a URL with a friendly UA, return BeautifulSoup or None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"    ✗ HTTP error fetching {url}: {e}", flush=True)
        return None


def fetch_summary(detail_url: str, fallback: str = "") -> str:
    """
    Fetch a detail page and produce a heuristic summary of its body.
    Returns the fallback text if the page can't be fetched or summarized.
    Skips PDFs and other non-HTML content.
    """
    if not detail_url:
        return fallback
    if detail_url.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")):
        return fallback
    try:
        soup = safe_get(detail_url, timeout=10)
        if not soup:
            return fallback
        body = extract_body_text(soup)
        summary = heuristic_summary(body)
        return summary or fallback
    except Exception as e:
        print(f"    ⚠ summary fetch failed: {e}", flush=True)
        return fallback


# ---------------------------------------------------------------------------
# PMDA — Japan
# ---------------------------------------------------------------------------
# Source: https://www.pmda.go.jp/english/safety/info-services/medical-devices/
#         English-language landing page for medical-device safety info.
# ---------------------------------------------------------------------------

def scrape_pmda():
    print("  → PMDA  English medical-device safety", flush=True)
    url = "https://www.pmda.go.jp/english/safety/info-services/medical-devices/0001.html"
    soup = safe_get(url)
    if not soup:
        return []
    items = []
    main = soup.find("main") or soup.find("div", id="main") or soup
    seen_urls = set()
    for a in main.find_all("a", href=True):
        title = a.get_text(strip=True)
        href = a["href"]
        if not title or len(title) < 10:
            continue
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        full_url = urljoin(url, href)
        if "pmda.go.jp" not in full_url:
            continue
        if not is_device_related(title):
            continue
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        summary = fetch_summary(full_url, fallback=title)
        items.append(normalize_item(
            agency="PMDA",
            region="Japan",
            category="postmarket",
            source="PMDA medical-device safety (scraped)",
            title=title,
            link=full_url,
            summary=summary,
        ))
        time.sleep(0.4)  # be polite — PMDA is a smaller server
        if len(items) >= 8:
            break
    print(f"    ✓ {len(items)} item(s)", flush=True)
    return items


# ---------------------------------------------------------------------------
# CDSCO — India
# ---------------------------------------------------------------------------
# Source: https://cdsco.gov.in/opencms/opencms/en/Notifications/
#         Public notifications page; English; usually a simple table.
# ---------------------------------------------------------------------------

def scrape_cdsco():
    print("  → CDSCO India notifications", flush=True)
    url = "https://cdsco.gov.in/opencms/opencms/en/Notifications/"
    soup = safe_get(url)
    if not soup:
        return []
    items = []
    seen = set()
    # CDSCO uses tables for notifications. Be liberal in selection.
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        href = a["href"]
        if not title or len(title) < 15:
            continue
        if not (href.lower().endswith(".pdf") or "/Notifications/" in href):
            continue
        full_url = urljoin(url, href)
        if not is_device_related(title):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)

        # Most CDSCO notifications link to PDFs — we can't summarize those
        # without a PDF parser, so we fall back to the title.
        summary = fetch_summary(full_url, fallback=title)
        items.append(normalize_item(
            agency="CDSCO",
            region="India",
            category="postmarket",
            source="CDSCO Notifications (scraped)",
            title=title,
            link=full_url,
            summary=summary,
        ))
        time.sleep(0.3)
        if len(items) >= 8:
            break
    print(f"    ✓ {len(items)} item(s)", flush=True)
    return items


# ---------------------------------------------------------------------------
# ANVISA — Brazil
# ---------------------------------------------------------------------------
# Source: https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa
#         Portuguese-language news. We prefix items with [PT] so users know.
# ---------------------------------------------------------------------------

ANVISA_PT_KEYWORDS = [
    # Portuguese device-related terms
    "dispositivo", "produto para saúde", "produtos para saúde",
    "diagnóstico in vitro", "ivd", "registro", "alerta", "recall",
    "tecnovigilância", "fabricante", "anvisa", "rdc",
    # English carries through too
    "device", "medical", "recall",
]


def scrape_anvisa():
    print("  → ANVISA Brazil news", flush=True)
    url = "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa"
    soup = safe_get(url)
    if not soup:
        return []
    items = []
    seen = set()
    for art in soup.find_all("article")[:30]:
        a = art.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 10:
            continue
        full_url = urljoin(url, a["href"])
        text = title.lower()
        if not any(k in text for k in ANVISA_PT_KEYWORDS):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)

        summary = fetch_summary(full_url, fallback=title)
        items.append(normalize_item(
            agency="ANVISA",
            region="Brazil",
            category="postmarket",
            source="ANVISA Notícias (scraped, Portuguese)",
            title=f"[PT] {title}",
            link=full_url,
            summary=summary,
        ))
        time.sleep(0.3)
        if len(items) >= 6:
            break
    print(f"    ✓ {len(items)} item(s)", flush=True)
    return items


# ---------------------------------------------------------------------------
# MAIN — runs all scrapers, never throws
# ---------------------------------------------------------------------------

SCRAPERS = [
    scrape_pmda,
    scrape_cdsco,
    scrape_anvisa,
]


def fetch_all_scraped():
    """Run all scrapers and return a combined list. Failures return []."""
    all_items = []
    for fn in SCRAPERS:
        try:
            all_items.extend(fn())
        except Exception as e:
            print(f"    ✗ scraper {fn.__name__} crashed: {e}", flush=True)
    return all_items


if __name__ == "__main__":
    # When run directly, just print what we'd collect.
    import json
    items = fetch_all_scraped()
    print(json.dumps(items, indent=2, ensure_ascii=False))
