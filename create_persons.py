"""
Create new declarations_person records from review_no_match.csv
and link them back to jvirblis.diplomats.

Reads the CSV produced by link_diplomats.py (Tier 5: no match).
For each row:
  1. INSERT into declarations_person (with proper field defaults)
  2. UPDATE jvirblis.diplomats SET person_id = new_id

Names are converted from CAPS to Title Case (initcap).

Usage:
  python3 create_persons.py review_no_match.csv             # dry-run
  python3 create_persons.py review_no_match.csv --apply      # commit
"""

import csv
import sys
from sshtunnel import SSHTunnelForwarder
import psycopg2
from config import (
    SSH_HOST, SSH_PORT, SSH_USER, SSH_KEY_PATH, SSH_KEY_PASSWORD,
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
)

APPLY = "--apply" in sys.argv
CREATED_BY_ID = 73  # your user ID


def initcap(s: str) -> str:
    """АГАСАНДЯН → Агасандян, handles hyphenated names too."""
    if not s:
        return s
    return "-".join(part.capitalize() for part in s.lower().split("-"))


def parse_dob(dob_str: str):
    """Parse DD.MM.YYYY → (YYYY-MM-DD, birth_year) or (None, None)."""
    if not dob_str or not dob_str.strip():
        return None, None
    parts = dob_str.strip().split(".")
    if len(parts) == 3:
        dd, mm, yyyy = parts
        return f"{yyyy}-{mm}-{dd}", int(yyyy)
    return None, None


def load_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 create_persons.py <review_no_match.csv> [--apply]")
        sys.exit(1)

    csv_path = sys.argv[1]
    rows = load_csv(csv_path)
    print(f"Loaded {len(rows)} rows from {csv_path}")

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

        created = 0
        linked = 0
        results = []

        for row in rows:
            diplomat_id = int(row["diplomat_id"])
            family_name = initcap(row["family_name"])
            name = initcap(row["name"])
            patronymic = initcap(row.get("patronymic", ""))
            dob_str, birth_year = parse_dob(row.get("dob", ""))

            # Build description from department + position
            dept = (row.get("department", "") or "").strip()
            pos = (row.get("position", "") or "").strip()
            if dept and pos:
                description = f"{dept} - {pos}".lower().title()
            elif dept:
                description = dept.lower().title()
            elif pos:
                description = pos.lower().title()
            else:
                description = ""

            # INSERT new person
            cur.execute("""
                INSERT INTO declarations_person (
                    family_name,
                    name,
                    patronymic,
                    birth_date,
                    comment,
                    description,
                    locked_page,
                    hidden,
                    created_from_rupep,
                    mirror_hidden,
                    created_by_id,
                    created_when
                ) VALUES (
                    %(family_name)s,
                    %(name)s,
                    %(patronymic)s,
                    %(birth_date)s,
                    %(comment)s,
                    %(description)s,
                    false,       -- locked_page
                    false,       -- hidden
                    false,       -- created_from_rupep
                    false,       -- mirror_hidden
                    %(created_by_id)s,
                    now()
                )
                RETURNING id
            """, {
                "family_name": family_name,
                "name": name,
                "patronymic": patronymic,
                "birth_date": f"{dob_str}" if dob_str else None,
                "comment": "person created from MFU dataset the 14/04/26",
                "description": description,
                "created_by_id": CREATED_BY_ID,
            })
            new_person_id = cur.fetchone()[0]
            created += 1

            # LINK diplomat → new person
            cur.execute("""
                UPDATE jvirblis.diplomats
                   SET person_id  = %s,
                       updated_at = now()
                 WHERE id = %s
            """, (new_person_id, diplomat_id))
            linked += 1

            results.append({
                "diplomat_id": diplomat_id,
                "person_id": new_person_id,
                "family_name": family_name,
                "name": name,
                "patronymic": patronymic,
                "dob": row.get("dob", ""),
            })

            print(f"  diplomat {diplomat_id:>4d} → new person {new_person_id:>8d}"
                  f"  {family_name} {name} {patronymic}")

        # Write audit log
        if results:
            with open("created_persons.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=results[0].keys())
                w.writeheader()
                w.writerows(results)
            print(f"\n  → created_persons.csv (audit trail)")

        print(f"\n{'='*60}")
        print(f"  Created : {created}")
        print(f"  Linked  : {linked}")
        print(f"{'='*60}")

        if APPLY:
            conn.commit()
            print("✅ Changes committed.")
        else:
            conn.rollback()
            print("🔄 DRY RUN — rolled back. Use --apply to commit.")

        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
