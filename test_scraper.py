"""
Offline tests for scraper.py — uses sample HTML from the mid.ru pages
so network access is not required.
"""

import json
from bs4 import BeautifulSoup
from scraper import parse_table, split_rank_and_date, parse_name_cell

SAMPLE_TABLE = """
<table>
  <tbody>
    <tr>
      <td>
        <p><span class="masha_index masha_index59" rel="59"></span>АНДРЕЕВ</p>
        <p><span class="masha_index masha_index60" rel="60"></span>АНДРЕЙ</p>
        <p><span class="masha_index masha_index61" rel="61"></span>ВЛАДИМИРОВИЧ</p>
      </td>
      <td>
        <p><span class="masha_index masha_index62" rel="62"></span>19.09.1956</p>
      </td>
      <td>
        <p><span class="masha_index masha_index63" rel="63"></span>ПОСОЛЬСТВО РОССИЙСКОЙ ФЕДЕРАЦИИ В РЕСПУБЛИКЕ МАДАГАСКАР</p>
      </td>
      <td>
        <p><span class="masha_index masha_index64" rel="64"></span>ЧРЕЗВЫЧАЙНЫЙ И ПОЛНОМОЧНЫЙ ПОСОЛ РОССИЙСКОЙ ФЕДЕРАЦИИ</p>
      </td>
      <td>
        <p><span class="masha_index masha_index65" rel="65"></span>ЧРЕЗВЫЧАЙНЫЙ И ПОЛНОМОЧНЫЙ ПОСОЛ 28.12.2021</p>
      </td>
    </tr>
    <tr>
      <td>
        <p>ИВАНОВ</p>
        <p>ИВАН</p>
        <p>ИВАНОВИЧ</p>
      </td>
      <td><p>01.01.1970</p></td>
      <td><p>ПОСОЛЬСТВО В ГЕРМАНИИ</p></td>
      <td><p>СОВЕТНИК</p></td>
      <td><p>СОВЕТНИК 1 КЛАССА 15.03.2018</p></td>
    </tr>
  </tbody>
</table>
"""


def test_split_rank_and_date():
    rank, date = split_rank_and_date("ЧРЕЗВЫЧАЙНЫЙ И ПОЛНОМОЧНЫЙ ПОСОЛ 28.12.2021")
    assert rank == "ЧРЕЗВЫЧАЙНЫЙ И ПОЛНОМОЧНЫЙ ПОСОЛ", repr(rank)
    assert date == "28.12.2021", repr(date)

    rank2, date2 = split_rank_and_date("СОВЕТНИК 1 КЛАССА 15.03.2018")
    assert date2 == "15.03.2018", repr(date2)

    rank3, date3 = split_rank_and_date("НЕТ ДАТЫ")
    assert rank3 == "НЕТ ДАТЫ"
    assert date3 == ""


def test_parse_name_cell():
    soup = BeautifulSoup(
        "<td><p><span></span>ФАМИЛИЯ</p><p>ИМЯ</p><p>ОТЧЕСТВО</p></td>", "lxml"
    )
    cell = soup.find("td")
    family, given, patronymic = parse_name_cell(cell)
    assert family == "ФАМИЛИЯ", repr(family)
    assert given == "ИМЯ", repr(given)
    assert patronymic == "ОТЧЕСТВО", repr(patronymic)


def test_parse_table_two_persons():
    soup = BeautifulSoup(SAMPLE_TABLE, "lxml")
    url = "https://example.com/test"
    persons = parse_table(soup, source_url=url)
    assert len(persons) == 2, f"Expected 2 persons, got {len(persons)}"

    p = persons[0]
    assert p.family_name == "АНДРЕЕВ"
    assert p.name == "АНДРЕЙ"
    assert p.patronymic == "ВЛАДИМИРОВИЧ"
    assert p.dob == "19.09.1956"
    assert "МАДАГАСКАР" in p.department
    assert "ПОСОЛ РОССИЙСКОЙ ФЕДЕРАЦИИ" in p.position
    assert p.diplomatic_rank == "ЧРЕЗВЫЧАЙНЫЙ И ПОЛНОМОЧНЫЙ ПОСОЛ"
    assert p.rank_acquisition_date == "28.12.2021"
    assert p.source_url == url

    p2 = persons[1]
    assert p2.family_name == "ИВАНОВ"
    assert p2.rank_acquisition_date == "15.03.2018"
    assert p2.source_url == url


def test_json_output_structure():
    """Verify the output serialises correctly to the required JSON shape."""
    import json
    from dataclasses import asdict
    from scraper import parse_table

    soup = BeautifulSoup(SAMPLE_TABLE, "lxml")
    persons = parse_table(soup, source_url="https://example.com/test")
    data = [asdict(p) for p in persons]
    blob = json.dumps(data, ensure_ascii=False)
    loaded = json.loads(blob)

    required_keys = {
        "family_name", "name", "patronymic", "dob",
        "department", "position", "diplomatic_rank", "rank_acquisition_date",
        "source_url",
    }
    for record in loaded:
        assert required_keys == set(record.keys()), set(record.keys())

    print("Sample output (first record):")
    print(json.dumps(loaded[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    tests = [
        test_split_rank_and_date,
        test_parse_name_cell,
        test_parse_table_two_persons,
        test_json_output_structure,
    ]
    for t in tests:
        t()
        print(f"PASS  {t.__name__}")
    print("\nAll tests passed.")
