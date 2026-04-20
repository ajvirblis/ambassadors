import json
import psycopg2
from sshtunnel import SSHTunnelForwarder
from config import (
    SSH_HOST, SSH_PORT, SSH_USER, SSH_KEY_PATH, SSH_KEY_PASSWORD,
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
)

JSON_FILE = 'ambassadors_w_urls.json'
ACTUALITY_DATE = '2026-04-06'

with open(JSON_FILE, encoding='utf-8') as f:
    records = json.load(f)

with SSHTunnelForwarder(
    (SSH_HOST, SSH_PORT),
    ssh_username=SSH_USER,
    ssh_pkey=SSH_KEY_PATH,
    ssh_private_key_password=SSH_KEY_PASSWORD,
    remote_bind_address=(DB_HOST, DB_PORT)
) as tunnel:

    conn = psycopg2.connect(
        host='localhost',
        port=tunnel.local_bind_port,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

    with conn.cursor() as cur:
        for rec in records:
            cur.execute(
                """
                INSERT INTO jvirblis.diplomats (body, actuality_date)
                VALUES (%s::jsonb, %s)
                """,
                (json.dumps(rec, ensure_ascii=False), ACTUALITY_DATE)
            )
        print(f"Inserted {len(records)} records")

    conn.commit()
    conn.close()