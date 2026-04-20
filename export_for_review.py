"""
Export unlinked diplomats for review, enriched with:
  - Bio from vneshpol.ru
  - Wikipedia links from Russian Wikipedia diplomat lists

Steps:
  1. Scrape vneshpol.ru letter index pages → name-to-URL map
  2. Scrape 3 Wikipedia diplomat list pages → name-to-wiki-URL map
  3. Query DB for unlinked diplomats
  4. For each diplomat found on vneshpol, fetch their bio page
  5. Export CSV with bio + wikipedia pre-filled for quick review

Uses local caches to avoid re-fetching on reruns.

Usage:
  python3 export_for_review.py                  # full run
  python3 export_for_review.py --skip-fetch     # use cache only, no HTTP
"""

import csv
import json
import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from sshtunnel import SSHTunnelForwarder
import psycopg2
from config import (
    SSH_HOST, SSH_PORT, SSH_USER, SSH_KEY_PATH, SSH_KEY_PASSWORD,
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
)

OUTPUT = "diplomats_review.csv"
CACHE_INDEX = "vneshpol_index.json"
CACHE_BIOS = "vneshpol_bios.json"
SKIP_FETCH = "--skip-fetch" in sys.argv

BASE_URL = "https://vneshpol.ru"
LETTERS = list("АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЩЭЮЯ")
DELAY = 1.5  # seconds between requests


def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    return s


# ── Step 1: scrape letter index pages ─────────────────────────────────────

def scrape_index(session) -> list[dict]:
    """Scrape all letter pages, return list of {family_name, name, patronymic, url}."""
    if os.path.exists(CACHE_INDEX):
        print(f"Loading cached index from {CACHE_INDEX}")
        with open(CACHE_INDEX, encoding="utf-8") as f:
            return json.load(f)

    if SKIP_FETCH:
        print("No cache and --skip-fetch specified, cannot proceed.")
        sys.exit(1)

    all_persons = []
    for letter in LETTERS:
        url = f"{BASE_URL}/diplomat/{letter}"
        print(f"  Index: {url} ...", end=" ", flush=True)
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("a[href^='/diplomat/']")
        count = 0
        for link in links:
            text = link.get_text(strip=True)
            if len(text) <= 3 or text == "Дипломаты":
                continue
            href = link.get("href", "")
            if not href or href == "/diplomat" or re.match(r'^/diplomat/[А-Я]$', href):
                continue

            parts = text.split()
            if len(parts) < 2:
                continue

            all_persons.append({
                "family_name": parts[0],
                "name": parts[1] if len(parts) > 1 else "",
                "patronymic": parts[2] if len(parts) > 2 else "",
                "url": f"{BASE_URL}{href}",
            })
            count += 1
        print(f"{count} persons")
        time.sleep(DELAY)

    # Save cache
    with open(CACHE_INDEX, "w", encoding="utf-8") as f:
        json.dump(all_persons, f, ensure_ascii=False, indent=2)
    print(f"  Cached {len(all_persons)} persons → {CACHE_INDEX}")
    return all_persons


# ── Step 2: fetch individual bio pages ─────────────────────────────────────

def parse_bio_page(html: str) -> dict:
    """Extract structured data from a vneshpol diplomat page."""
    soup = BeautifulSoup(html, "html.parser")

    result = {"bio": "", "rank_vneshpol": "", "department_vneshpol": "", "position_vneshpol": ""}

    # The bio and fields are in the main content area
    # Look for field labels
    content = soup.find("div", class_="field-name-body") or soup.find("article")
    if not content:
        # Fallback: get all text from main region
        content = soup

    text = content.get_text("\n", strip=True)

    # Extract specific fields using patterns
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("Биография:"):
            # Everything after "Биография:" until the end of the content block
            idx = text.index("Биография:")
            bio_text = text[idx + len("Биография:"):].strip()
            # Cut at known section markers
            for marker in ["Ошибка?", "Календарь", "Дипломаты", "Все события"]:
                if marker in bio_text:
                    bio_text = bio_text[:bio_text.index(marker)].strip()
            result["bio"] = bio_text[:2000]  # cap at 2000 chars for CSV
            break

    # Also try to extract rank/dept/position from structured fields
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("Ранг:"):
            result["rank_vneshpol"] = line.replace("Ранг:", "").strip()[:500]
        elif line.startswith("Подразделение:"):
            result["department_vneshpol"] = line.replace("Подразделение:", "").strip()[:500]
        elif line.startswith("Должность:"):
            result["position_vneshpol"] = line.replace("Должность:", "").strip()[:500]

    return result


def fetch_bios(session, urls_to_fetch: dict) -> dict:
    """
    Fetch individual diplomat pages.
    urls_to_fetch: {diplomat_id: url}
    Returns: {diplomat_id: {bio, rank_vneshpol, ...}}
    """
    # Load existing cache
    cache = {}
    if os.path.exists(CACHE_BIOS):
        with open(CACHE_BIOS, encoding="utf-8") as f:
            cache = json.load(f)

    results = {}
    to_fetch = []

    for did, url in urls_to_fetch.items():
        did_str = str(did)
        if did_str in cache:
            results[did] = cache[did_str]
        else:
            to_fetch.append((did, url))

    if to_fetch and SKIP_FETCH:
        print(f"  {len(to_fetch)} pages not cached, skipping (--skip-fetch)")
    elif to_fetch:
        print(f"  Fetching {len(to_fetch)} bio pages...")
        for i, (did, url) in enumerate(to_fetch):
            print(f"    [{i+1}/{len(to_fetch)}] {url} ...", end=" ", flush=True)
            try:
                resp = session.get(url, timeout=30)
                resp.raise_for_status()
                data = parse_bio_page(resp.text)
                results[did] = data
                cache[str(did)] = data
                print("OK")
            except Exception as e:
                print(f"ERROR: {e}")
                results[did] = {"bio": f"FETCH ERROR: {e}",
                                "rank_vneshpol": "", "department_vneshpol": "",
                                "position_vneshpol": ""}
            time.sleep(DELAY)

        # Update cache
        with open(CACHE_BIOS, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"  Cache updated → {CACHE_BIOS}")

    return results


# ── Step 3: match diplomats to vneshpol index ─────────────────────────────

def build_vneshpol_lookup(index: list[dict]) -> dict:
    """
    Build lookup: (lower_family, lower_name) → list of vneshpol entries.
    For matching, we use family+name first, then disambiguate by patronymic.
    """
    lookup = {}
    for entry in index:
        key = (entry["family_name"].lower(), entry["name"].lower())
        lookup.setdefault(key, []).append(entry)
    return lookup


def find_vneshpol_url(diplomat: dict, lookup: dict) -> tuple:
    """
    Returns (url, patronymic_from_vneshpol) or (None, None).
    """
    key = (diplomat["family_name"].lower(), diplomat["name"].lower())
    candidates = lookup.get(key, [])

    if not candidates:
        return None, None

    if len(candidates) == 1:
        return candidates[0]["url"], candidates[0]["patronymic"]

    # Multiple: try to narrow by patronymic
    d_pat = (diplomat.get("patronymic") or "").lower()
    if d_pat:
        exact = [c for c in candidates if c["patronymic"].lower() == d_pat]
        if len(exact) == 1:
            return exact[0]["url"], exact[0]["patronymic"]

    # Still ambiguous — return None, reviewer will search manually
    return None, None


# ── Step 2: scrape Wikipedia diplomat lists ────────────────────────────────

WIKI_PAGES = [
    "Список_чрезвычайных_и_полномочных_послов_России",
    "Список_чрезвычайных_и_полномочных_посланников_1_класса_России",
    "Список_чрезвычайных_и_полномочных_посланников_2_класса_России",
]
CACHE_WIKI = "wikipedia_diplomats.json"


def scrape_wikipedia(session) -> dict:
    """
    Scrape Wikipedia diplomat list pages.
    Returns dict: (lower_family, lower_name, lower_patronymic) → wiki_url
    """
    if os.path.exists(CACHE_WIKI):
        print(f"  Loading cached Wikipedia data from {CACHE_WIKI}")
        with open(CACHE_WIKI, encoding="utf-8") as f:
            return json.load(f)

    if SKIP_FETCH:
        print("  No Wikipedia cache and --skip-fetch specified, skipping.")
        return {}

    wiki_map = {}  # "family|name|patronymic" → url  (keys as strings for JSON)

    for page_title in WIKI_PAGES:
        # Use MediaWiki API to get parsed HTML
        api_url = (
            f"https://ru.wikipedia.org/w/api.php"
            f"?action=parse&page={page_title}&prop=text&format=json"
            f"&disabletoc=1&disableeditsection=1"
        )
        print(f"  Fetching Wikipedia: {page_title} ...", end=" ", flush=True)
        try:
            resp = session.get(api_url, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            html = data.get("parse", {}).get("text", {}).get("*", "")
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        soup = BeautifulSoup(html, "html.parser")
        count = 0

        # Find all internal links to person articles
        # Names appear as "Фамилия, Имя Отчество" in link text or nearby text
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            text = link.get_text(strip=True)

            # Skip non-article links
            if not href.startswith("/wiki/") or ":" in href:
                continue
            # Skip links to list/category pages
            if "Список" in href or "Категория" in href:
                continue

            # Parse "Фамилия, Имя Отчество" or "Фамилия, Имя Отчество (дипломат)"
            # Remove disambiguation suffixes
            clean = re.sub(r'\s*\(.*?\)\s*$', '', text).strip()
            if "," not in clean:
                continue

            parts = clean.split(",", 1)
            family = parts[0].strip()
            rest = parts[1].strip().split()
            if not rest:
                continue

            name = rest[0]
            patronymic = rest[1] if len(rest) > 1 else ""

            wiki_url = f"https://ru.wikipedia.org{href}"
            key = f"{family.lower()}|{name.lower()}|{patronymic.lower()}"
            wiki_map[key] = wiki_url
            count += 1

        print(f"{count} linked persons")
        time.sleep(DELAY)

    # Save cache
    with open(CACHE_WIKI, "w", encoding="utf-8") as f:
        json.dump(wiki_map, f, ensure_ascii=False, indent=2)
    print(f"  Cached {len(wiki_map)} Wikipedia entries → {CACHE_WIKI}")
    return wiki_map


def find_wikipedia_url(diplomat: dict, wiki_map: dict) -> str:
    """
    Look up diplomat in Wikipedia map.
    Try full ФИО first, then family+name only.
    Returns URL or empty string.
    """
    fam = diplomat["family_name"].lower()
    nam = diplomat["name"].lower()
    pat = (diplomat.get("patronymic") or "").lower()

    # Exact match
    key = f"{fam}|{nam}|{pat}"
    if key in wiki_map:
        return wiki_map[key]

    # Try without patronymic (if diplomat has empty patronymic)
    if not pat:
        for k, url in wiki_map.items():
            parts = k.split("|")
            if parts[0] == fam and parts[1] == nam:
                return url

    return ""


# ── main ───────────────────────────────────────────────────────────────────

def main():
    session = make_session()

    # Step 1: build vneshpol index
    print("=== Step 1: vneshpol.ru index ===\n")
    index = scrape_index(session)
    lookup = build_vneshpol_lookup(index)
    print(f"  {len(index)} persons indexed, {len(lookup)} unique (family, name) keys\n")

    # Step 2: build Wikipedia index
    print("=== Step 2: Wikipedia diplomat lists ===\n")
    wiki_map = scrape_wikipedia(session)
    print(f"  {len(wiki_map)} Wikipedia entries loaded\n")

    # Step 3: get unlinked diplomats from DB
    print("=== Step 3: query unlinked diplomats ===\n")
    with SSHTunnelForwarder(
        (SSH_HOST, SSH_PORT),
        ssh_username=SSH_USER,
        ssh_pkey=SSH_KEY_PATH,
        ssh_private_key_password=SSH_KEY_PASSWORD,
        remote_bind_address=(DB_HOST, DB_PORT),
    ) as tunnel:
        conn = psycopg2.connect(
            host="127.0.0.1",
            port=tunnel.local_bind_port,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
        cur = conn.cursor()

        cur.execute("""
            SELECT
                d.id                                    AS diplomat_id,
                initcap(lower(d.body->>'family_name'))  AS family_name,
                initcap(lower(d.body->>'name'))         AS name,
                initcap(lower(d.body->>'patronymic'))   AS patronymic,
                d.body->>'dob'                          AS dob,
                d.body->>'department'                   AS department,
                d.body->>'position'                     AS position,
                d.body->>'diplomatic_rank'              AS diplomatic_rank,
                d.body->>'rank_acquisition_date'        AS rank_date,
                d.body->>'source_url'                   AS source_url
            FROM jvirblis.diplomats d
            WHERE d.person_id IS NULL
            ORDER BY d.body->>'family_name', d.body->>'name'
        """)
        cols = [desc[0] for desc in cur.description]
        diplomats = [dict(zip(cols, row)) for row in cur.fetchall()]
        cur.close()
        conn.close()

    print(f"  {len(diplomats)} unlinked diplomats\n")

    # Step 4: match to vneshpol and collect URLs to fetch
    print("=== Step 4: match vneshpol & fetch bios ===\n")
    urls_to_fetch = {}
    vneshpol_urls = {}     # diplomat_id → url
    vneshpol_pats = {}     # diplomat_id → patronymic from vneshpol

    matched = 0
    for d in diplomats:
        url, pat = find_vneshpol_url(d, lookup)
        if url:
            urls_to_fetch[d["diplomat_id"]] = url
            vneshpol_urls[d["diplomat_id"]] = url
            vneshpol_pats[d["diplomat_id"]] = pat or ""
            matched += 1

    print(f"  Matched to vneshpol: {matched}/{len(diplomats)}")

    # Fetch bio pages
    bios = fetch_bios(session, urls_to_fetch)

    # Step 5: match to Wikipedia
    wiki_matched = 0
    wiki_urls = {}  # diplomat_id → wiki_url
    for d in diplomats:
        url = find_wikipedia_url(d, wiki_map)
        if url:
            wiki_urls[d["diplomat_id"]] = url
            wiki_matched += 1
    print(f"\n  Matched to Wikipedia: {wiki_matched}/{len(diplomats)}")

    # Step 6: export CSV
    print(f"\n=== Step 6: export CSV ===\n")

    fieldnames = [
        "diplomat_id", "family_name", "name", "patronymic", "dob",
        "department", "position", "diplomatic_rank", "rank_date", "source_url",
        "vneshpol_url", "vneshpol_patronymic", "vneshpol_bio",
        "action", "wikipedia", "comment",
    ]

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for d in diplomats:
            did = d["diplomat_id"]
            bio_data = bios.get(did, {})
            row = {
                **d,
                "vneshpol_url": vneshpol_urls.get(did, ""),
                "vneshpol_patronymic": vneshpol_pats.get(did, ""),
                "vneshpol_bio": bio_data.get("bio", ""),
                "action": "",
                "wikipedia": wiki_urls.get(did, ""),
                "comment": "",
            }
            w.writerow(row)

    print(f"  → {OUTPUT} ({len(diplomats)} rows)")
    print(f"\nFill the 'action' column:")
    print(f'  "create"  → new person will be created')
    print(f'  "123456"  → link to existing person_id')
    print(f'  "skip"    → leave for later')


if __name__ == "__main__":
    main()
