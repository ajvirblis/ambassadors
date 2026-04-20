"""
Link jvirblis.diplomats → declarations_person with frequency-aware matching.

With 1.5M+ persons in the DB, a single full-name match does NOT guarantee
correctness. Common names (Иванов Сергей Владимирович) can easily hit the
wrong person. Only name + DOB is truly safe for auto-linking.

Tiers
─────
T1  full ФИО + DOB, exactly 1 hit           → auto-link
T2  full ФИО, 1 hit, name is RARE           → auto-link (with frequency proof)
T3  full ФИО, 1 hit, name is COMMON         → export for review (likely right, but verify)
T4  partial / multiple candidates            → export for review (ambiguous)
T5  no match at all                          → export (new person to create)

"Rare" means: the (family_name, name) pair appears ≤ N times in the entire
declarations_person table.  Default threshold: 3.

Outputs
───────
  auto_linked.csv          – log of what was auto-linked (T1 + T2)
  review_likely.csv        – T3: single match but common name, needs human check
  review_ambiguous.csv     – T4: multiple candidates
  review_no_match.csv      – T5: no match found

Usage:
  python3 link_diplomats.py                    # dry-run
  python3 link_diplomats.py --apply            # commit to DB
  python3 link_diplomats.py --threshold 5      # adjust rarity cutoff
"""

import csv
import sys
from sshtunnel import SSHTunnelForwarder
import psycopg2
import psycopg2.extras
from config import (
    SSH_HOST, SSH_PORT, SSH_USER, SSH_KEY_PATH, SSH_KEY_PASSWORD,
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
)

APPLY = "--apply" in sys.argv

# Rarity threshold: max count of (family_name, name) pairs in the whole DB
# to consider a full-ФИО-only match safe for auto-linking.
RARE_THRESHOLD = 3
for i, arg in enumerate(sys.argv):
    if arg == "--threshold" and i + 1 < len(sys.argv):
        RARE_THRESHOLD = int(sys.argv[i + 1])


# ── helpers ────────────────────────────────────────────────────────────────

def get_unlinked(cur):
    """Return all diplomats where person_id IS NULL."""
    cur.execute("""
        SELECT
            d.id,
            d.body->>'family_name'          AS family_name,
            d.body->>'name'                 AS name,
            d.body->>'patronymic'           AS patronymic,
            d.body->>'dob'                  AS dob,
            d.body->>'department'           AS department,
            d.body->>'position'             AS position,
            d.body->>'diplomatic_rank'      AS rank,
            d.body->>'rank_acquisition_date' AS rank_date,
            d.body->>'source_url'           AS source_url
        FROM jvirblis.diplomats d
        WHERE d.person_id IS NULL
        ORDER BY d.body->>'family_name'
    """)
    cols = [desc[0] for desc in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_frequency_cache(cur) -> dict:
    """
    Pre-compute name frequencies across the entire declarations_person table.
    Returns dict keyed by (lower_family, lower_name) → count.
    """
    print("Building (family_name, name) frequency cache…")
    cur.execute("""
        SELECT lower(family_name), lower(name), count(*)
          FROM declarations_person
         WHERE family_name IS NOT NULL AND name IS NOT NULL
         GROUP BY lower(family_name), lower(name)
    """)
    freq = {}
    for fam, nam, cnt in cur.fetchall():
        freq[(fam, nam)] = cnt
    print(f"  {len(freq):,} distinct (family_name, name) pairs loaded")
    return freq


def build_fio_frequency_cache(cur) -> dict:
    """
    Count of exact (family_name, name, patronymic) triples across entire DB.
    """
    print("Building full ФИО frequency cache…")
    cur.execute("""
        SELECT lower(family_name), lower(name),
               lower(coalesce(patronymic, '')), count(*)
          FROM declarations_person
         WHERE family_name IS NOT NULL AND name IS NOT NULL
         GROUP BY lower(family_name), lower(name),
                  lower(coalesce(patronymic, ''))
    """)
    freq = {}
    for fam, nam, pat, cnt in cur.fetchall():
        freq[(fam, nam, pat)] = cnt
    print(f"  {len(freq):,} distinct ФИО triples loaded")
    return freq


def name_pair_freq(freq_cache: dict, family_name: str, name: str) -> int:
    return freq_cache.get((family_name.lower(), name.lower()), 0)


def fio_freq(fio_cache: dict, family_name: str, name: str, patronymic: str) -> int:
    return fio_cache.get(
        (family_name.lower(), name.lower(), (patronymic or '').lower()), 0
    )


def apply_links(cur, links: list[dict], tier: str):
    """UPDATE diplomats.person_id for a batch of confirmed links."""
    if not links:
        return
    for lnk in links:
        tag = " [unhiding]" if lnk.get('hidden') else ""
        print(f"  [{tier}] diplomat {lnk['diplomat_id']:>4d} → person {lnk['person_id']:>8d}"
              f"  {lnk['family_name']} {lnk['name']} {lnk['patronymic']}"
              f"  (fn_freq={lnk.get('fn_freq','?')}"
              f", fio_freq={lnk.get('fio_freq','?')}){tag}")
    if APPLY:
        psycopg2.extras.execute_batch(cur, """
            UPDATE jvirblis.diplomats
               SET person_id  = %(person_id)s,
                   updated_at = now()
             WHERE id = %(diplomat_id)s
        """, links)


ALL_HIDDEN_TO_UNHIDE = []  # accumulated across tiers

def report_hidden(person_ids: list[int]):
    """Collect hidden persons that need to be unhidden separately."""
    if not person_ids:
        return
    ALL_HIDDEN_TO_UNHIDE.extend(person_ids)
    print(f"  ⚠ {len(person_ids)} hidden persons found (will export to CSV)")


def export_hidden_csv():
    """Write all hidden person_ids to a CSV for separate processing."""
    if not ALL_HIDDEN_TO_UNHIDE:
        return
    unique = sorted(set(ALL_HIDDEN_TO_UNHIDE))
    write_csv('unhide_persons.csv', [{'person_id': pid} for pid in unique])
    print(f"\n  → unhide_persons.csv ({len(unique)} person_ids)")
    print(f"    Run with appropriate permissions:")
    print(f"    UPDATE declarations_person SET hidden = false"
          f" WHERE id IN ({','.join(str(x) for x in unique)});")


def write_csv(filename: str, rows: list[dict]):
    if not rows:
        return
    # Collect all keys across all rows (handles mixed schemas like T1+T2)
    fieldnames = list(dict.fromkeys(k for row in rows for k in row.keys()))
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


# ── Tier 1: full ФИО + DOB ────────────────────────────────────────────────

def tier1(cur, unlinked, freq_fn, freq_fio):
    """Full name + DOB, exactly 1 candidate → auto-link. The only truly safe tier."""
    linked_ids = set()
    links = []
    to_unhide = []
    log = []

    for d in unlinked:
        if not d['dob']:
            continue
        try:
            cur.execute("""
                SELECT p.id, p.hidden
                  FROM declarations_person p
                 WHERE lower(p.family_name) = lower(%s)
                   AND lower(p.name)        = lower(%s)
                   AND lower(p.patronymic)  = lower(%s)
                   AND p.birth_date = to_date(%s, 'DD.MM.YYYY')
            """, (d['family_name'], d['name'], d['patronymic'], d['dob']))
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT t1")
            continue
        rows = cur.fetchall()

        if len(rows) == 1:
            pid, hidden = rows[0]
            fn_f = name_pair_freq(freq_fn, d['family_name'], d['name'])
            fio_f = fio_freq(freq_fio, d['family_name'], d['name'], d['patronymic'])
            link = {
                'diplomat_id': d['id'],
                'person_id': pid,
                'family_name': d['family_name'],
                'name': d['name'],
                'patronymic': d['patronymic'],
                'fn_freq': fn_f,
                'fio_freq': fio_f,
                'hidden': hidden,
            }
            links.append(link)
            log.append({**link, 'tier': 'T1', 'dob': d['dob']})
            if hidden:
                to_unhide.append(pid)
            linked_ids.add(d['id'])

    print(f"\n── Tier 1: full ФИО + DOB → {len(links)} auto-links"
          f" ({len(to_unhide)} hidden to unhide) ──")
    report_hidden(to_unhide)
    apply_links(cur, links, "T1")
    return linked_ids, log


# ── Tier 2 & 3: full ФИО only, split by frequency ─────────────────────────

def tier2_and_3(cur, unlinked, already_done, freq_fn, freq_fio):
    """
    Full ФИО match, exactly 1 candidate, no DOB confirmation.
    Split into:
      T2 (rare name → auto-link)
      T3 (common name → export for review)
    """
    t2_links = []
    t2_unhide = []
    t2_log = []
    t3_review = []

    for d in unlinked:
        if d['id'] in already_done:
            continue

        cur.execute("""
            SELECT p.id, p.birth_date, p.hidden
              FROM declarations_person p
             WHERE lower(p.family_name) = lower(%s)
               AND lower(p.name)        = lower(%s)
               AND lower(p.patronymic)  = lower(%s)
        """, (d['family_name'], d['name'], d['patronymic']))
        rows = cur.fetchall()

        if len(rows) != 1:
            continue  # 0 or multiple → handled later

        pid, p_birth, hidden = rows[0]
        fn_f = name_pair_freq(freq_fn, d['family_name'], d['name'])
        fio_f = fio_freq(freq_fio, d['family_name'], d['name'], d['patronymic'])

        rec = {
            'diplomat_id': d['id'],
            'person_id': pid,
            'family_name': d['family_name'],
            'name': d['name'],
            'patronymic': d['patronymic'],
            'dob': d['dob'],
            'candidate_dob': str(p_birth) if p_birth else '',
            'fn_freq': fn_f,
            'fio_freq': fio_f,
            'hidden': hidden,
            'department': d['department'],
            'position': d['position'],
        }

        if fn_f <= RARE_THRESHOLD:
            # Rare name → confident enough to auto-link
            t2_links.append(rec)
            t2_log.append({**rec, 'tier': 'T2'})
            if hidden:
                t2_unhide.append(pid)
        else:
            # Common name → needs human eyes
            rec['link_person_id'] = ''   # volunteer fills
            rec['comment'] = ''
            t3_review.append(rec)

    # Apply T2
    t2_linked_ids = {r['diplomat_id'] for r in t2_links}
    print(f"\n── Tier 2: full ФИО, rare name (fn_freq ≤ {RARE_THRESHOLD})"
          f" → {len(t2_links)} auto-links"
          f" ({len(t2_unhide)} hidden to unhide) ──")
    report_hidden(t2_unhide)
    apply_links(cur, t2_links, "T2")

    # Export T3
    t3_diplomat_ids = {r['diplomat_id'] for r in t3_review}
    print(f"\n── Tier 3: full ФИО, common name (fn_freq > {RARE_THRESHOLD})"
          f" → {len(t3_review)} for review ──")
    if t3_review:
        write_csv('review_likely.csv', t3_review)
        print(f"  → review_likely.csv")

    return t2_linked_ids, t3_diplomat_ids, t2_log


# ── Tier 4: partial / multiple candidates ──────────────────────────────────

def tier4(cur, unlinked, already_done, freq_fn, freq_fio):
    """Partial matches or multiple full-ФИО candidates → export for review."""
    ambiguous = []
    handled_ids = set()

    for d in unlinked:
        if d['id'] in already_done:
            continue

        # Check if full ФИО had multiple hits (skipped in tier2_and_3)
        cur.execute("""
            SELECT p.id, p.family_name, p.name, p.patronymic,
                   p.birth_date, p.hidden
              FROM declarations_person p
             WHERE lower(p.family_name) = lower(%s)
               AND lower(p.name)        = lower(%s)
               AND lower(p.patronymic)  = lower(%s)
        """, (d['family_name'], d['name'], d['patronymic']))
        full_hits = cur.fetchall()

        if len(full_hits) > 1:
            for c in full_hits:
                ambiguous.append(
                    _candidate_row(d, c, 'full_match', freq_fn, freq_fio))
            handled_ids.add(d['id'])
            continue

        if len(full_hits) == 1:
            continue  # already handled in T2/T3

        # No full ФИО → try family + name (no patronymic)
        cur.execute("""
            SELECT p.id, p.family_name, p.name, p.patronymic,
                   p.birth_date, p.hidden
              FROM declarations_person p
             WHERE lower(p.family_name) = lower(%s)
               AND lower(p.name)        = lower(%s)
        """, (d['family_name'], d['name']))
        fn_hits = cur.fetchall()

        if fn_hits:
            for c in fn_hits:
                ambiguous.append(
                    _candidate_row(d, c, 'family_plus_name', freq_fn, freq_fio))
            handled_ids.add(d['id'])
            continue

        # Even broader: family + initials
        cur.execute("""
            SELECT p.id, p.family_name, p.name, p.patronymic,
                   p.birth_date, p.hidden
              FROM declarations_person p
             WHERE lower(p.family_name) = lower(%s)
               AND lower(left(p.name, 1)) = lower(left(%s, 1))
               AND (  %s = ''
                   OR lower(left(p.patronymic, 1)) = lower(left(%s, 1))
                   OR p.patronymic IS NULL
                   OR p.patronymic = '')
        """, (d['family_name'], d['name'], d['patronymic'], d['patronymic']))
        init_hits = cur.fetchall()

        if init_hits:
            for c in init_hits:
                ambiguous.append(
                    _candidate_row(d, c, 'family_plus_initials', freq_fn, freq_fio))
            handled_ids.add(d['id'])

    # Sort by quality then name
    quality_order = {'full_match': 1, 'family_plus_name': 2, 'family_plus_initials': 3}
    ambiguous.sort(key=lambda r: (
        quality_order[r['match_quality']], r['family_name'], r['name']))

    n_diplomats = len(set(r['diplomat_id'] for r in ambiguous))
    print(f"\n── Tier 4: ambiguous → {n_diplomats} diplomats,"
          f" {len(ambiguous)} candidate rows ──")
    if ambiguous:
        write_csv('review_ambiguous.csv', ambiguous)
        print(f"  → review_ambiguous.csv")

    return handled_ids


def _candidate_row(d, candidate, quality, freq_fn, freq_fio):
    pid, c_fam, c_name, c_pat, c_bd, c_hidden = candidate
    return {
        'diplomat_id': d['id'],
        'family_name': d['family_name'],
        'name': d['name'],
        'patronymic': d['patronymic'],
        'dob': d['dob'],
        'department': d['department'],
        'position': d['position'],
        'fn_freq': name_pair_freq(freq_fn, d['family_name'], d['name']),
        'fio_freq': fio_freq(freq_fio, d['family_name'], d['name'], d['patronymic']),
        'match_quality': quality,
        'candidate_person_id': pid,
        'candidate_name': f"{c_fam} {c_name} {c_pat or ''}".strip(),
        'candidate_dob': str(c_bd) if c_bd else '',
        'candidate_hidden': c_hidden,
        'link_person_id': '',   # volunteer fills
        'comment': '',
    }


# ── Tier 5: no match at all ───────────────────────────────────────────────

def tier5(unlinked, all_done):
    no_match = []
    for d in unlinked:
        if d['id'] in all_done:
            continue
        no_match.append({
            'diplomat_id': d['id'],
            'family_name': d['family_name'],
            'name': d['name'],
            'patronymic': d['patronymic'],
            'dob': d['dob'],
            'department': d['department'],
            'position': d['position'],
            'rank': d['rank'],
            'action': '',   # volunteer: "create" / person_id / "skip"
            'comment': '',
        })

    print(f"\n── Tier 5: no match at all → {len(no_match)} diplomats ──")
    if no_match:
        write_csv('review_no_match.csv', no_match)
        print(f"  → review_no_match.csv")

    return no_match


# ── main ───────────────────────────────────────────────────────────────────

def main():
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

        # Pre-compute frequencies across entire 1.5M+ person table
        freq_fn  = build_frequency_cache(cur)
        freq_fio = build_fio_frequency_cache(cur)

        unlinked = get_unlinked(cur)
        print(f"\nUnlinked diplomats: {len(unlinked)}")
        print(f"Rarity threshold (--threshold): fn_freq ≤ {RARE_THRESHOLD}\n")

        done = set()
        all_log = []

        # T1: safe — name + DOB
        t1_ids, t1_log = tier1(cur, unlinked, freq_fn, freq_fio)
        done |= t1_ids
        all_log.extend(t1_log)

        # T2/T3: name only, split by frequency
        t2_ids, t3_ids, t2_log = tier2_and_3(cur, unlinked, done, freq_fn, freq_fio)
        done |= t2_ids
        done |= t3_ids  # mark as handled (exported to review)
        all_log.extend(t2_log)

        # T4: ambiguous
        t4_ids = tier4(cur, unlinked, done, freq_fn, freq_fio)
        done |= t4_ids

        # T5: nothing
        tier5(unlinked, done)

        # Write auto-link log
        if all_log:
            write_csv('auto_linked.csv', all_log)
            print(f"\n  → auto_linked.csv (audit trail)")

        # Export hidden persons for separate unhiding
        export_hidden_csv()

        # Summary
        auto = len(t1_ids) + len(t2_ids)
        print(f"\n{'='*60}")
        print(f"  Auto-linked (T1+T2)   : {auto}")
        print(f"    T1 (ФИО + DOB)      : {len(t1_ids)}")
        print(f"    T2 (ФИО, rare name) : {len(t2_ids)}")
        print(f"  For review            : {len(unlinked) - auto}")
        print(f"    T3 (ФИО, common)    : {len(t3_ids)}")
        print(f"    T4 (ambiguous)      : {len(t4_ids)}")
        print(f"    T5 (no match)       : {len(unlinked) - len(done)}")
        print(f"{'='*60}")

        if APPLY:
            conn.commit()
            print("✅ Changes committed.")
        else:
            conn.rollback()
            print("🔄 DRY RUN — rolled back. Use --apply to commit.")
            print("   CSVs are still written for inspection.")

        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
