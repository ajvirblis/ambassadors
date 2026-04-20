"""
Process the reviewed diplomats_review.csv.

Reads the 'action' column:
  - "create"          → INSERT into declarations_person, link back to diplomat
  - integer (pid)     → link diplomat to that existing person_id
  - "skip" or empty   → ignore

Usage:
  python3 process_review.py diplomats_review.csv             # dry-run
  python3 process_review.py diplomats_review.csv --apply      # commit
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
CREATED_BY_ID = 73


def initcap(s: str) -> str:
    """Title-case, handles hyphens."""
    if not s:
        return s
    return "-".join(part.capitalize() for part in s.lower().split("-"))


def parse_dob(dob_str: str):
    """DD.MM.YYYY → YYYY-MM-DD or None."""
    if not dob_str or not dob_str.strip():
        return None
    parts = dob_str.strip().split(".")
    if len(parts) == 3:
        dd, mm, yyyy = parts
        return f"{yyyy}-{mm}-{dd}"
    return None


def load_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def create_person(cur, row: dict) -> int:
    """INSERT new person, return new id."""
    family_name = row["family_name"].strip()
    name = row["name"].strip()
    patronymic = row.get("patronymic", "").strip()
    birth_date = parse_dob(row.get("dob", ""))

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

    wikipedia = (row.get("wikipedia", "") or "").strip() or None

    cur.execute("""
        INSERT INTO declarations_person (
            family_name, name, patronymic, birth_date,
            comment, description, wikipedia,
            locked_page, hidden, created_from_rupep, mirror_hidden,
            created_by_id, created_when
        ) VALUES (
            %(family_name)s, %(name)s, %(patronymic)s, %(birth_date)s,
            %(comment)s, %(description)s, %(wikipedia)s,
            false, false, false, false,
            %(created_by_id)s, now()
        )
        RETURNING id
    """, {
        "family_name": family_name,
        "name": name,
        "patronymic": patronymic,
        "birth_date": birth_date,
        "comment": "person created from MFU dataset the 14/04/26",
        "description": description,
        "wikipedia": wikipedia,
        "created_by_id": CREATED_BY_ID,
    })
    return cur.fetchone()[0]


def link_diplomat(cur, diplomat_id: int, person_id: int):
    """SET person_id on the diplomat record."""
    cur.execute("""
        UPDATE jvirblis.diplomats
           SET person_id  = %s,
               updated_at = now()
         WHERE id = %s
    """, (person_id, diplomat_id))


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 process_review.py <diplomats_review.csv> [--apply]")
        sys.exit(1)

    csv_path = sys.argv[1]
    rows = load_csv(csv_path)
    print(f"Loaded {len(rows)} rows from {csv_path}")

    # Classify actions
    to_create = []
    to_link = []
    skipped = 0

    for row in rows:
        action = (row.get("action", "") or "").strip()
        if not action or action.lower() == "skip":
            skipped += 1
            continue
        elif action.lower() == "create":
            to_create.append(row)
        elif action.isdigit():
            to_link.append((row, int(action)))
        else:
            print(f"  ⚠ Unknown action '{action}' for diplomat {row['diplomat_id']}, skipping")
            skipped += 1

    print(f"\n  To create : {len(to_create)}")
    print(f"  To link   : {len(to_link)}")
    print(f"  Skipped   : {skipped}\n")

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

        results = []

        # Process creates
        for row in to_create:
            diplomat_id = int(row["diplomat_id"])
            new_pid = create_person(cur, row)
            link_diplomat(cur, diplomat_id, new_pid)
            print(f"  [create] diplomat {diplomat_id:>4d} → new person {new_pid:>8d}"
                  f"  {row['family_name']} {row['name']} {row.get('patronymic','')}")
            results.append({
                "diplomat_id": diplomat_id,
                "action": "create",
                "person_id": new_pid,
                "family_name": row["family_name"],
                "name": row["name"],
                "patronymic": row.get("patronymic", ""),
                "wikipedia": row.get("wikipedia", ""),
            })

        # Process links
        for row, pid in to_link:
            diplomat_id = int(row["diplomat_id"])
            link_diplomat(cur, diplomat_id, pid)
            print(f"  [link]   diplomat {diplomat_id:>4d} → person {pid:>8d}"
                  f"  {row['family_name']} {row['name']} {row.get('patronymic','')}")
            results.append({
                "diplomat_id": diplomat_id,
                "action": "link",
                "person_id": pid,
                "family_name": row["family_name"],
                "name": row["name"],
                "patronymic": row.get("patronymic", ""),
            })

        # Audit log
        if results:
            with open("review_processed.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=results[0].keys())
                w.writeheader()
                w.writerows(results)
            print(f"\n  → review_processed.csv (audit trail)")

        print(f"\n{'='*60}")
        print(f"  Created : {len(to_create)}")
        print(f"  Linked  : {len(to_link)}")
        print(f"  Total   : {len(results)}")
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
