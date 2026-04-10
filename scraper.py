"""
Scraper for Russian MFA ambassador nomination pages.

Fetches persons from three diplomat-rank pages on mid.ru and writes a JSON
file with the following fields per person:
  family_name, name, patronymic, dob, department, position,
  diplomatic_rank, rank_acquisition_date
"""

import json
import re
import sys
from dataclasses import asdict, dataclass
from typing import Optional

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
        # Remove span elements (index markers), then grab remaining text
        for span in p.find_all("span"):
            span.decompose()
        text = p.get_text(strip=True)
        if text:
            parts.append(text)

    # Pad to exactly three components
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

    # The data lives inside <table> elements; find all rows with exactly 5 <td>s.
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

        # Skip header-like rows that contain no real data
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


def fetch(url: str, session: requests.Session) -> BeautifulSoup:
    response = session.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return BeautifulSoup(response.text, "lxml")


def scrape_all(urls: list[str] = URLS) -> list[dict]:
    all_persons: list[Person] = []
    with requests.Session() as session:
        for url in urls:
            print(f"Fetching {url} ...", file=sys.stderr)
            soup = fetch(url, session)
            persons = parse_table(soup)
            print(f"  → {len(persons)} persons found", file=sys.stderr)
            all_persons.extend(persons)

    return [asdict(p) for p in all_persons]


def main() -> None:
    data = scrape_all()
    output = json.dumps(data, ensure_ascii=False, indent=2)
    print(output)
    print(f"\nTotal persons: {len(data)}", file=sys.stderr)


if __name__ == "__main__":
    main()
