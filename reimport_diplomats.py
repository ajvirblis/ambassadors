"""
Reimport diplomats JSON with source_url field.

Strategy:
  1. UPDATE existing rows by matching on (family_name, name, patronymic, dob)
     — this preserves person_id
  2. INSERT rows that don't match any existing record (genuinely new diplomats)
  3. Optionally mark rows in DB that are NOT in the new JSON (removed from MFA site)

Usage:
  python3 reimport_diplomats.py ambassadors_v2.json          # dry-run (default)
  python3 reimport_diplomats.py ambassadors_v2.json --apply   # actually write
"""

import json
import sys
from sshtunnel import SSHTunnelForwarder
import psycopg2
import psycopg2.extras
from config import (
    SSH_HOST, SSH_PORT, SSH_USER, SSH_KEY_PATH, SSH_KEY_PASSWORD,
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
)


def load_json(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def reimport(conn, records: list[dict], *, apply: bool = False):
    cur = conn.cursor()

    # --- Step 1: UPDATE existing rows (match on natural key inside body) ---
    update_sql = """
        UPDATE jvirblis.diplomats d
           SET body       = %(new_body)s::jsonb,
               updated_at = now()
         WHERE d.body->>'family_name' = %(family_name)s
           AND d.body->>'name'        = %(name)s
           AND d.body->>'patronymic'  = %(patronymic)s
           AND d.body->>'dob'         = %(dob)s
        RETURNING d.id, d.person_id
    """

    updated = 0
    inserted = 0
    to_insert = []

    for rec in records:
        cur.execute(update_sql, {
            "new_body": json.dumps(rec, ensure_ascii=False),
            "family_name": rec["family_name"],
            "name": rec["name"],
            "patronymic": rec["patronymic"],
            "dob": rec["dob"],
        })
        rows = cur.fetchall()
        if rows:
            updated += len(rows)
            for row_id, pid in rows:
                if pid is not None:
                    print(f"  updated id={row_id} (person_id={pid} preserved) — {rec['family_name']}")
        else:
            to_insert.append(rec)

    # --- Step 2: INSERT new records ---
    if to_insert:
        insert_sql = """
            INSERT INTO jvirblis.diplomats (body)
            VALUES %s
        """
        psycopg2.extras.execute_values(
            cur, insert_sql,
            [(psycopg2.extras.Json(rec),) for rec in to_insert],
            page_size=500,
        )
        inserted = len(to_insert)

    # --- Step 3: Report rows in DB not present in new JSON ---
    cur.execute("SELECT count(*) FROM jvirblis.diplomats")
    total_after = cur.fetchone()[0]

    print(f"\n--- Summary ---")
    print(f"  Records in new JSON : {len(records)}")
    print(f"  Updated (matched)   : {updated}")
    print(f"  Inserted (new)      : {inserted}")
    print(f"  Total rows in table : {total_after}")
    print(f"  Rows not in new JSON: {total_after - updated} (kept as-is)")

    if apply:
        conn.commit()
        print("\n  ✅ Changes committed.")
    else:
        conn.rollback()
        print("\n  🔄 DRY RUN — rolled back. Use --apply to commit.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 reimport_diplomats.py <json_file> [--apply]")
        sys.exit(1)

    json_path = sys.argv[1]
    apply = "--apply" in sys.argv
    records = load_json(json_path)
    print(f"Loaded {len(records)} records from {json_path}")

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
        try:
            reimport(conn, records, apply=apply)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
