"""
Scraper for Russian MFA ambassador nomination pages.

mid.ru is protected by F5 Bot Defense (TSPD/DOSL7), which serves a JS
challenge page to plain HTTP clients. This scraper uses Playwright — a
real headless browser — to execute the challenge and retrieve the actual
page content.

Requirements:
  pip install playwright beautifulsoup4 lxml
  playwright install chromium

Usage:
  python3 scraper.py               # JSON on stdout
  python3 scraper.py --debug       # also print row counts per URL
  python3 scraper.py > output.json
"""

import json
import re
import sys
from dataclasses import asdict, dataclass

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

URLS = [
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_ambassador/",
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_1_class/",
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_2_class/",
]

# Date pattern: DD.MM.YYYY
_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")


@dataclass
class Person:
    family_name: str
    name: str
    patronymic: str
    dob: str
    department: str
    position: str
    diplomatic_rank: str
    rank_acquisition_date: str


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def cell_text(cell) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def parse_name_cell(cell) -> tuple[str, str, str]:
    """
    Name cell contains up to three <p> tags (family / given / patronymic).
    Each <p> may contain an invisible <span> index marker — strip those first.
    Falls back to splitting the plain cell text when no <p> tags are present.
    """
    parts: list[str] = []
    for p in cell.find_all("p"):
        for span in p.find_all("span"):
            span.decompose()
        text = p.get_text(strip=True)
        if text:
            parts.append(text)

    if not parts:
        raw = cell_text(cell)
        if raw:
            parts = raw.split()

    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def split_rank_and_date(raw: str) -> tuple[str, str]:
    """
    'ЧРЕЗВЫЧАЙНЫЙ И ПОЛНОМОЧНЫЙ ПОСОЛ 28.12.2021'
    → ('ЧРЕЗВЫЧАЙНЫЙ И ПОЛНОМОЧНЫЙ ПОСОЛ', '28.12.2021')
    """
    m = _DATE_RE.search(raw)
    if m:
        return raw[: m.start()].strip(), m.group(1)
    return raw, ""


def parse_table(soup: BeautifulSoup) -> list[Person]:
    persons: list[Person] = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != 5:
            continue

        family_name, given_name, patronymic = parse_name_cell(cells[0])
        dob = cell_text(cells[1])
        department = cell_text(cells[2])
        position = cell_text(cells[3])
        rank_raw = cell_text(cells[4])
        diplomatic_rank, rank_acquisition_date = split_rank_and_date(rank_raw)

        if not family_name and not dob:
            continue  # skip header-like rows

        persons.append(Person(
            family_name=family_name,
            name=given_name,
            patronymic=patronymic,
            dob=dob,
            department=department,
            position=position,
            diplomatic_rank=diplomatic_rank,
            rank_acquisition_date=rank_acquisition_date,
        ))
    return persons


# ---------------------------------------------------------------------------
# Playwright-based fetch (handles F5 Bot Defense JS challenge)
# ---------------------------------------------------------------------------

def fetch_with_playwright(url: str, page, debug: bool = False) -> list[Person]:
    print(f"Fetching {url} ...", file=sys.stderr)

    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
    except PWTimeoutError:
        # networkidle can time out on heavy pages; try domcontentloaded instead
        print("  networkidle timed out, retrying with domcontentloaded ...",
              file=sys.stderr)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # give JS challenge extra time to complete and reload
        page.wait_for_timeout(5_000)

    # Wait until at least one <td> appears (real page), up to 30 s
    try:
        page.wait_for_selector("td", timeout=30_000)
    except PWTimeoutError:
        print("  WARNING: no <td> found after waiting 30 s — "
              "challenge may not have resolved.", file=sys.stderr)

    html = page.content()
    soup = BeautifulSoup(html, "lxml")
    persons = parse_table(soup)

    if debug:
        tables = soup.find_all("table")
        rows_with_5td = [r for r in soup.find_all("tr") if len(r.find_all("td")) == 5]
        print(f"  [debug] <table>: {len(tables)}  "
              f"rows with 5 <td>: {len(rows_with_5td)}", file=sys.stderr)

    print(f"  → {len(persons)} persons found", file=sys.stderr)
    return persons


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape_all(urls: list[str] = URLS, debug: bool = False) -> list[dict]:
    all_persons: list[Person] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for url in urls:
            try:
                persons = fetch_with_playwright(url, page, debug=debug)
                all_persons.extend(persons)
            except Exception as exc:
                print(f"  ERROR on {url}: {exc}", file=sys.stderr)

        browser.close()

    return [asdict(p) for p in all_persons]


def main() -> None:
    debug = "--debug" in sys.argv
    data = scrape_all(debug=debug)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nTotal persons: {len(data)}", file=sys.stderr)


if __name__ == "__main__":
    main()
