"""
Scraper for Russian MFA ambassador nomination pages.

mid.ru is protected by F5 Bot Defense (TSPD/DOSL7), which performs deep
browser fingerprinting. Standard Playwright/Selenium are detected and
blocked. This scraper uses undetected-chromedriver, which patches the
Chrome binary to remove automation signals.

Requirements:
  pip install undetected-chromedriver selenium beautifulsoup4 lxml certifi
  Google Chrome must be installed on the machine.

Usage:
  python3 scraper.py               # JSON on stdout
  python3 scraper.py --debug       # also print row counts per URL
  python3 scraper.py > output.json
"""

import json
import os
import re
import ssl
import sys
from dataclasses import asdict, dataclass

# macOS Python (python.org installer) ships without system CA certificates.
# Point the SSL stack at the certifi bundle before any HTTPS calls are made.
# This fixes the SSL error that occurs when undetected-chromedriver tries to
# download ChromeDriver from storage.googleapis.com.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    ssl.create_default_context = lambda *a, **kw: ssl.create_default_context(
        *a, cafile=certifi.where(), **kw
    )
except ImportError:
    pass  # certifi not installed — will try anyway

from bs4 import BeautifulSoup

URLS = [
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_ambassador/",
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_1_class/",
    "https://mid.ru/ru/activity/shots/persons/extraordinary_ambassador/diplomat_of_2_class/",
]

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
# HTML parsing helpers (unchanged)
# ---------------------------------------------------------------------------

def cell_text(cell) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def parse_name_cell(cell) -> tuple[str, str, str]:
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
            continue

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
# undetected-chromedriver fetch
# ---------------------------------------------------------------------------

def make_driver():
    import undetected_chromedriver as uc  # requires: pip install undetected-chromedriver
    options = uc.ChromeOptions()
    options.add_argument("--lang=ru-RU,ru")
    options.add_argument("--window-size=1280,900")
    # Do NOT add headless here — uc has its own headless patching if needed,
    # but visible mode is most reliable against F5.
    return uc.Chrome(options=options, use_subprocess=True)


def fetch_page(driver, url: str, debug: bool = False) -> list[Person]:
    print(f"Fetching {url} ...", file=sys.stderr)
    driver.get(url)

    # Wait up to 45 s for the real table to appear after the F5 challenge
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
        WebDriverWait(driver, 45).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table td"))
        )
    except Exception:
        print("  WARNING: table not found within 45 s.", file=sys.stderr)

    soup = BeautifulSoup(driver.page_source, "lxml")
    persons = parse_table(soup)

    if debug:
        tables = soup.find_all("table")
        rows5 = [r for r in soup.find_all("tr") if len(r.find_all("td")) == 5]
        print(
            f"  [debug] <table>: {len(tables)}  rows with 5 <td>: {len(rows5)}",
            file=sys.stderr,
        )

    print(f"  → {len(persons)} persons found", file=sys.stderr)
    return persons


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape_all(urls: list[str] = URLS, debug: bool = False) -> list[dict]:
    all_persons: list[Person] = []
    driver = make_driver()
    try:
        for url in urls:
            try:
                persons = fetch_page(driver, url, debug=debug)
                all_persons.extend(persons)
            except Exception as exc:
                print(f"  ERROR on {url}: {exc}", file=sys.stderr)
    finally:
        driver.quit()

    return [asdict(p) for p in all_persons]


def main() -> None:
    debug = "--debug" in sys.argv
    data = scrape_all(debug=debug)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nTotal persons: {len(data)}", file=sys.stderr)


if __name__ == "__main__":
    main()
