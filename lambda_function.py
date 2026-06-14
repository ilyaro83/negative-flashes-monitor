import os
import json
import boto3
import psycopg2
import requests
from datetime import datetime, timezone

DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]
STATE_BUCKET = os.environ["STATE_BUCKET"]
STATE_KEY = "seen_negative_users.json"

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


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {msg}", flush=True)


def load_seen_users():
    s3 = boto3.client("s3")
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=STATE_KEY)
        return set(json.loads(obj["Body"].read()))
    except s3.exceptions.NoSuchKey:
        return set()


def save_seen_users(user_ids):
    s3 = boto3.client("s3")
    s3.put_object(Bucket=STATE_BUCKET, Key=STATE_KEY, Body=json.dumps(list(user_ids)))


def lambda_handler(event, context):
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
        log("Below threshold — no alert sent.")
        save_seen_users(seen_users | current_users)
        return {"status": "below_threshold", "new_users": len(new_users)}

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
    return {"status": "alert_sent", "new_users": len(new_users)}
