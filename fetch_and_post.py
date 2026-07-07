#!/usr/bin/env python3
"""
NYC FLF Hourly — fetch median FLF per starting point from Snowflake
and post a formatted summary to #temp-nyc-flf-hourly.
"""

import os
import sys
from datetime import datetime
import zoneinfo

import snowflake.connector
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ── Config ──────────────────────────────────────────────────────────────────
SLACK_CHANNEL_ID = "C0BEV9JK4LT"

QUERY = """
ALTER SESSION SET QUERY_TAG = 'agentskills:nyc-flf-hourly';
with hrly_flf as (
  select
    n.sp_name as starting_point_name
    , n.submarket_name
    , n.sp_id
    , convert_timezone('UTC','US/Eastern',sdd.CREATED_AT) sp_local_min
    , sdd.flf
  from IGUAZU.SERVER_EVENTS_PRODUCTION.supply_demand_data sdd
    join proddb.eugenepang.ep_nyc_sps n on n.sp_id=sdd.STARTING_POINT_ID
  where
    sdd.created_at >= dateadd('hour', -1, current_timestamp())
    and horizon='current'
)
select
  starting_point_name
  , submarket_name
  , round(median(flf), 3) as median_flf
  , count(*) as n_obs
from hrly_flf
group by 1, 2
order by submarket_name, starting_point_name
"""

# ── Helpers ──────────────────────────────────────────────────────────────────

def flf_emoji(flf: float) -> str:
    if flf <= 2.0:
        return "🟢"
    elif flf < 2.5:
        return "🟡"
    else:
        return "🔴"


def format_message(rows: list[tuple], now: datetime) -> str:
    timestamp = now.strftime("%a %b %-d @ %-I:%M %p ET")
    header = f"*NYC Live FLF — {timestamp}*"

    if not rows:
        return f"{header}\n_No data available for the last hour._"

    # Build aligned table
    col_width = max(len(row[0]) for row in rows) + 2  # sp name + padding
    sep = "─" * (col_width + 10)
    lines = [f"{'SP Name':<{col_width}}| FLF", sep]
    for sp_name, _submarket, median_flf, _n in rows:
        emoji = flf_emoji(float(median_flf))
        lines.append(f"{emoji} {sp_name:<{col_width - 2}}| {float(median_flf):.3f}")

    n_total = len(rows)
    n_green = sum(1 for r in rows if float(r[2]) <= 2.0)
    n_yellow = sum(1 for r in rows if 2.0 < float(r[2]) < 2.5)
    n_red = sum(1 for r in rows if float(r[2]) >= 2.5)
    summary = f"*{n_total} SPs | 🟢 {n_green} · 🟡 {n_yellow} · 🔴 {n_red}*"

    table = "\n".join(lines)
    return f"{header}\n\n```\n{table}\n```\n{summary}"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Snowflake connection — supports both password and private-key auth
    sf_account = os.environ["SNOWFLAKE_ACCOUNT"]          # e.g. doordash-doordash
    sf_user = os.environ["SNOWFLAKE_USER"]                # e.g. JEEHAE.LEE
    sf_warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", "ADHOC")
    slack_token = os.environ["SLACK_BOT_TOKEN"]

    # Auth: prefer private key, fall back to password
    sf_private_key_b64 = os.environ.get("SNOWFLAKE_PRIVATE_KEY_B64")
    sf_password = os.environ.get("SNOWFLAKE_PASSWORD")

    connect_kwargs: dict = dict(
        account=sf_account,
        user=sf_user,
        warehouse=sf_warehouse,
        session_parameters={"QUERY_TAG": "agentskills:nyc-flf-hourly"},
    )

    if sf_private_key_b64:
        import base64
        from cryptography.hazmat.primitives.serialization import (
            load_der_private_key,
            Encoding,
            PrivateFormat,
            NoEncryption,
        )
        key_bytes = base64.b64decode(sf_private_key_b64)
        private_key = load_der_private_key(key_bytes, password=None)
        connect_kwargs["private_key"] = private_key.private_bytes(
            Encoding.DER, PrivateFormat.PKCS8, NoEncryption()
        )
    elif sf_password:
        connect_kwargs["password"] = sf_password
    else:
        print("ERROR: set SNOWFLAKE_PRIVATE_KEY_B64 or SNOWFLAKE_PASSWORD", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(zoneinfo.ZoneInfo("America/New_York"))

    # Query Snowflake
    try:
        conn = snowflake.connector.connect(**connect_kwargs)
        cur = conn.cursor()
        # Run the two statements — ALTER SESSION then SELECT
        for stmt in QUERY.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as exc:
        print(f"Snowflake error (skipping post): {exc}", file=sys.stderr)
        sys.exit(0)  # silent skip on query error

    message = format_message(rows, now)

    # Post to Slack
    client = WebClient(token=slack_token)
    try:
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=message)
        print(f"Posted to {SLACK_CHANNEL_ID} at {now.isoformat()}")
    except SlackApiError as exc:
        print(f"Slack error: {exc.response['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
