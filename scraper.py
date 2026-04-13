"""
Scraper for Russian MFA ambassador nomination pages.

Fetches persons from three diplomat-rank pages on mid.ru and writes a JSON
file with the following fields per person:
  family_name, name, patronymic, dob, department, position,
  diplomatic_rank, rank_acquisition_date

Usage:
  python3 scraper.py                  # normal run, JSON on stdout
  python3 scraper.py --debug          # save raw HTML + print structure info
"""

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URLS = [
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_ambassador/",
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_1_class/",
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_2_class/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Date pattern: DD.MM.YYYY (may appear standalone or appended after a rank title)
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


def cell_text(cell) -> str:
    """Return clean, collapsed whitespace text from a <td> element."""
    return " ".join(cell.get_text(" ", strip=True).split())


def parse_name_cell(cell) -> tuple[str, str, str]:
    """
    The name cell contains up to three <p> tags: family, given, patronymic.
    Each <p> may include an invisible <span> before the text; strip it.
    """
    parts: list[str] = []
    for p in cell.find_all("p"):
        for span in p.find_all("span"):
            span.decompose()
        text = p.get_text(strip=True)
        if text:
            parts.append(text)

    # Fallback: the whole cell is one block of text (no <p> tags)
    if not parts:
        raw = cell_text(cell)
        if raw:
            parts = raw.split()

    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def split_rank_and_date(raw: str) -> tuple[str, str]:
    """
    The last column usually looks like:
      "ЧРЕЗВЫЧАЙНЫЙ И ПОЛНОМОЧНЫЙ ПОСОЛ 28.12.2021"
    Split off the trailing date.
    """
    m = _DATE_RE.search(raw)
    if m:
        date = m.group(1)
        rank = raw[: m.start()].strip()
        return rank, date
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
            continue

        persons.append(
            Person(
                family_name=family_name,
                name=given_name,
                patronymic=patronymic,
                dob=dob,
                department=department,
                position=position,
                diplomatic_rank=diplomatic_rank,
                rank_acquisition_date=rank_acquisition_date,
            )
        )

    return persons


def debug_structure(soup: BeautifulSoup, slug: str) -> None:
    """Print a structural summary and save raw HTML for inspection."""
    html_path = Path(f"debug_{slug}.html")
    html_path.write_text(soup.prettify(), encoding="utf-8")
    print(f"  [debug] Raw HTML saved → {html_path}", file=sys.stderr)

    tables = soup.find_all("table")
    print(f"  [debug] <table> elements found: {len(tables)}", file=sys.stderr)

    all_rows = soup.find_all("tr")
    print(f"  [debug] <tr> elements found (total): {len(all_rows)}", file=sys.stderr)

    # Tally rows by their td count
    td_counts: dict[int, int] = {}
    for row in all_rows:
        n = len(row.find_all("td"))
        td_counts[n] = td_counts.get(n, 0) + 1
    for n, count in sorted(td_counts.items()):
        print(f"  [debug]   rows with {n} <td>: {count}", file=sys.stderr)

    # Show first non-empty row so we can see the actual tag structure
    for row in all_rows:
        cells = row.find_all(["td", "th"])
        if cells:
            print(f"  [debug] First non-empty row sample:\n{row.prettify()[:800]}",
                  file=sys.stderr)
            break


def fetch(url: str, session: requests.Session) -> requests.Response:
    response = session.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    return response


def scrape_all(urls: list[str] = URLS, debug: bool = False) -> list[dict]:
    all_persons: list[Person] = []
    with requests.Session() as session:
        for url in urls:
            slug = url.rstrip("/").split("/")[-1]
            print(f"Fetching {url} ...", file=sys.stderr)

            try:
                response = fetch(url, session)
            except Exception as exc:
                print(f"  ERROR: request failed — {exc}", file=sys.stderr)
                continue

            print(f"  HTTP {response.status_code}  final URL: {response.url}",
                  file=sys.stderr)

            if response.status_code != 200:
                print(f"  ERROR: non-200 response, skipping.", file=sys.stderr)
                if debug:
                    print(f"  Response body (first 2000 chars):\n{response.text[:2000]}",
                          file=sys.stderr)
                continue

            response.encoding = response.apparent_encoding or "utf-8"
            raw_html = response.text

            if debug:
                # Print first 3000 chars of raw HTML directly — no file needed
                print(f"\n{'='*60}", file=sys.stderr)
                print(f"  RAW HTML (first 3000 chars) for {slug}:", file=sys.stderr)
                print(raw_html[:3000], file=sys.stderr)
                print(f"{'='*60}\n", file=sys.stderr)

                # Also save to file (absolute path printed so it's findable)
                html_path = Path(slug + "_debug.html").resolve()
                try:
                    html_path.write_text(raw_html, encoding="utf-8")
                    print(f"  Full HTML saved → {html_path}", file=sys.stderr)
                except Exception as exc:
                    print(f"  Could not save file: {exc}", file=sys.stderr)

            soup = BeautifulSoup(raw_html, "lxml")

            if debug:
                debug_structure(soup, slug)

            persons = parse_table(soup)
            print(f"  → {len(persons)} persons found", file=sys.stderr)
            all_persons.extend(persons)

    return [asdict(p) for p in all_persons]


def main() -> None:
    debug = "--debug" in sys.argv
    data = scrape_all(debug=debug)
    output = json.dumps(data, ensure_ascii=False, indent=2)
    print(output)
    print(f"\nTotal persons: {len(data)}", file=sys.stderr)


if __name__ == "__main__":
    main()
