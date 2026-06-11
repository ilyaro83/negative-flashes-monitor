import os
import json
import psycopg2
import requests
from datetime import datetime, timezone


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}", flush=True)


def _load_dotenv():
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_file):
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]

SEEN_USERS_FILE = os.path.join(os.path.dirname(__file__), "seen_negative_users.json")

QUERY = """
SELECT
    au.id AS userid,
    COALESCE(p.total_purchased, 0) AS total_purchased,
    COALESCE(um.media_cost, 0) + COALESCE(ua.avatar_cost, 0) AS total_spent,
    COALESCE(p.total_purchased, 0) - COALESCE(um.media_cost, 0) - COALESCE(ua.avatar_cost, 0) AS net_flashes
FROM dbo.aspnetusers au
LEFT JOIN (
    SELECT userid, SUM(amount) AS total_purchased FROM dbo.purchases GROUP BY userid
) p ON p.userid = au.id
LEFT JOIN (
    SELECT userid, COUNT(*) AS media_cost FROM dbo.usermedia GROUP BY userid
) um ON um.userid = au.id
LEFT JOIN (
    SELECT userid, COUNT(*) AS avatar_cost FROM dbo.useravatar GROUP BY userid
) ua ON ua.userid = au.id
WHERE COALESCE(p.total_purchased, 0) - COALESCE(um.media_cost, 0) - COALESCE(ua.avatar_cost, 0) < 0
ORDER BY net_flashes ASC;
"""

def load_seen_users():
    if os.path.exists(SEEN_USERS_FILE):
        with open(SEEN_USERS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen_users(user_ids):
    with open(SEEN_USERS_FILE, "w") as f:
        json.dump(list(user_ids), f)

def run():
    log("Run started")
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )
    cur = conn.cursor()
    cur.execute(QUERY)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    seen_users = load_seen_users()
    rows_by_user = {row[0]: row for row in rows}
    current_users = set(rows_by_user.keys())

    new_users = current_users - seen_users
    new_rows = [rows_by_user[uid] for uid in new_users]
    new_rows.sort(key=lambda r: r[3])

    log(f"Total with negative balance: {len(current_users)} | New this run: {len(new_users)} | Threshold: 3")

    if len(new_users) <= 3:
        log(f"Below threshold — no alert sent.")
        save_seen_users(seen_users | current_users)
        return

    lines = [f":rotating_light: *Negative Flashes Report* — {len(new_users)} new user(s) with negative balance\n"]
    lines.append("```")
    lines.append(f"{'user_id':<40} {'purchased':>10} {'viewed':>10} {'net':>10}")
    lines.append("-" * 72)
    for userid, purchased, viewed, net in new_rows:
        lines.append(f"{str(userid):<40} {purchased:>10} {viewed:>10} {net:>10}")
    lines.append("```")
    message = "\n".join(lines)

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": SLACK_CHANNEL_ID, "text": message},
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")
    log(f"Slack alert sent — {len(new_users)} new users with negative flashes.")

    save_seen_users(seen_users | current_users)

if __name__ == "__main__":
    run()
