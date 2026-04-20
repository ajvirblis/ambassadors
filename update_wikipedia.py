"""
Update declarations_person.wikipedia from cached Wikipedia diplomat data.

Reads wikipedia_diplomats.json (produced by export_for_review.py),
matches against jvirblis.diplomats by ФИО, and updates the wikipedia
field on declarations_person for matched records.

Usage:
  python3 update_wikipedia.py              # dry-run
  python3 update_wikipedia.py --apply      # commit
"""

import csv
import json
import os
import sys
from sshtunnel import SSHTunnelForwarder
import psycopg2
from config import (
    SSH_HOST, SSH_PORT, SSH_USER, SSH_KEY_PATH, SSH_KEY_PASSWORD,
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
)

APPLY = "--apply" in sys.argv
CACHE_FILE = "wikipedia_diplomats.json"


def load_wiki_cache() -> dict:
    """
    Load wikipedia_diplomats.json.
    Format: {"family|name|patronymic": "url", ...}
    Returns: {(lower_family, lower_name, lower_patronymic): url}
    """
    if not os.path.exists(CACHE_FILE):
        print(f"  {CACHE_FILE} not found! Run export_for_review.py first.")
        sys.exit(1)

    with open(CACHE_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    lookup = {}
    for key, url in raw.items():
        parts = key.split("|")
        if len(parts) == 3:
            lookup[(parts[0], parts[1], parts[2])] = url

    print(f"  Loaded {len(lookup)} Wikipedia entries from {CACHE_FILE}")
    return lookup


def find_wiki_url(family_name, name, patronymic, lookup) -> str:
    """Match diplomat to Wikipedia. Full ФИО first, then family+name for empty patronymic."""
    fam = family_name.lower()
    nam = name.lower()
    pat = (patronymic or "").lower()

    key = (fam, nam, pat)
    if key in lookup:
        return lookup[key]

    # For diplomats with empty patronymic, try any match on family+name
    if not pat:
        for k, url in lookup.items():
            if k[0] == fam and k[1] == nam:
                return url

    return ""


def main():
    print("=== Step 1: load Wikipedia cache ===\n")
    lookup = load_wiki_cache()

    print(f"\n=== Step 2: match & update ===\n")

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
                d.id                        AS diplomat_id,
                d.person_id,
                d.body->>'family_name'      AS family_name,
                d.body->>'name'             AS name,
                d.body->>'patronymic'       AS patronymic,
                p.wikipedia                 AS current_wiki
            FROM jvirblis.diplomats d
            JOIN declarations_person p ON p.id = d.person_id
            WHERE d.person_id IS NOT NULL
            ORDER BY d.body->>'family_name'
        """)
        cols = [desc[0] for desc in cur.description]
        diplomats = [dict(zip(cols, row)) for row in cur.fetchall()]

        print(f"  Linked diplomats: {len(diplomats)}")

        updates = []
        already_set = 0
        not_found = 0

        for d in diplomats:
            wiki_url = find_wiki_url(
                d["family_name"], d["name"], d["patronymic"], lookup)

            if not wiki_url:
                not_found += 1
                continue

            if d["current_wiki"] and d["current_wiki"].strip():
                already_set += 1
                continue

            updates.append({
                "person_id": d["person_id"],
                "diplomat_id": d["diplomat_id"],
                "family_name": d["family_name"],
                "name": d["name"],
                "patronymic": d["patronymic"] or "",
                "wiki_url": wiki_url,
            })

        print(f"  Matched & need update : {len(updates)}")
        print(f"  Already have wikipedia: {already_set}")
        print(f"  Not found on Wikipedia: {not_found}")

        if updates:
            print(f"\n── Updates ──")
            for u in updates:
                print(f"  person {u['person_id']:>8d}"
                      f"  {u['family_name']} {u['name']} {u['patronymic']}"
                      f"  → {u['wiki_url']}")

            if APPLY:
                for u in updates:
                    cur.execute("""
                        UPDATE declarations_person
                           SET wikipedia = %s,
                               modified_when = now(),
                               modified_by_id = 73
                         WHERE id = %s
                           AND (wikipedia IS NULL OR wikipedia = '')
                    """, (u["wiki_url"], u["person_id"]))

            with open("wikipedia_updates.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=updates[0].keys())
                w.writeheader()
                w.writerows(updates)
            print(f"\n  → wikipedia_updates.csv")

        print(f"\n{'='*60}")
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
