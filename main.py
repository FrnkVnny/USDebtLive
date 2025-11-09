#!/usr/bin/env python3
"""
US Debt Auto Tweet Script

What it does (for each run):
1. Fetch latest U.S. public debt from FiscalData (Debt to the Penny, v2).
2. Fetch "yesterday" = latest record on or before today - 1 day.
3. Fetch "1 week ago" = latest record on or before today - 7 days.
4. Compute daily and weekly increases.
5. Build a formatted tweet.
6. If DRY_RUN=1 -> only print the tweet.
7. Otherwise -> post the tweet to X using X_BEARER_TOKEN.

Intended to be run via GitHub Actions once per day.
"""

import os
import sys
from datetime import date, timedelta

import requests
import certifi
from dateutil import parser as dateparser
from requests_oauthlib import OAuth1

# FiscalData API endpoint (v2)
BASE_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/"
    "fiscal_service/v2/accounting/od/debt_to_penny"
)

# How old the latest record is allowed to be before we refuse to tweet (safety)
MAX_STALE_DAYS = 3


# ---------- Low-level helpers ----------

def _request(params: dict) -> dict:
    """
    Perform a GET request to the FiscalData API with standard options.
    Raises on non-200 or empty data.
    """
    resp = requests.get(
        BASE_URL,
        params=params,
        timeout=20,
        verify=certifi.where(),
    )
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data", [])
    if not data:
        raise RuntimeError(f"No data returned for params={params!r}")
    return data[0]


def fetch_latest_debt_row() -> dict:
    """
    Get the most recent available record (by record_date).
    """
    params = {
        "fields": "record_date,tot_pub_debt_out_amt",
        "sort": "-record_date",
        "page[size]": 1,
    }
    return _request(params)


def fetch_debt_on_or_before(target_iso_date: str) -> dict:
    """
    Get the latest record with record_date <= target_iso_date.
    Handles weekends/holidays automatically by design.
    """
    params = {
        "fields": "record_date,tot_pub_debt_out_amt",
        "filter": f"record_date:lte:{target_iso_date}",
        "sort": "-record_date",
        "page[size]": 1,
    }
    return _request(params)


def parse_debt(row: dict):
    """
    Extract numeric amount + record_date string from an API row.
    """
    amt = float(row["tot_pub_debt_out_amt"])
    dstr = row["record_date"]
    return amt, dstr


def billions(x: float) -> float:
    return x / 1_000_000_000.0


def format_billions(delta: float) -> str:
    sign = "+" if delta >= 0 else "-"
    return f"{sign}${abs(billions(delta)):,.2f} B"


# ---------- Core logic: build tweet text ----------

def build_tweet_text() -> str:
    """
    Build the final tweet text from live FiscalData.
    Includes a stale-data safeguard.
    """
    # Latest available -> "Today"
    latest_row = fetch_latest_debt_row()
    today_debt, today_str = parse_debt(latest_row)
    today_record_date = dateparser.parse(today_str).date()

    # Safety: ensure data isn't unreasonably old
    days_old = (date.today() - today_record_date).days
    if days_old > MAX_STALE_DAYS:
        raise RuntimeError(
            f"Latest debt data is stale: {today_str} "
            f"({days_old} days old, max allowed {MAX_STALE_DAYS})."
        )

    # "Yesterday" -> latest record on or before (today_record_date - 1 day)
    y_target = today_record_date - timedelta(days=1)
    y_row = fetch_debt_on_or_before(y_target.isoformat())
    y_debt, y_str = parse_debt(y_row)

    # "1 Week Ago" -> latest record on or before (today_record_date - 7 days)
    w_target = today_record_date - timedelta(days=7)
    w_row = fetch_debt_on_or_before(w_target.isoformat())
    w_debt, w_str = parse_debt(w_row)

    daily_inc = today_debt - y_debt
    weekly_inc = today_debt - w_debt

    tweet = (
        "🇺🇸 U.S. Debt Update\n\n"
        f"💰 Today: ${today_debt:,.2f}\n"
        f"📅 Yesterday: ${y_debt:,.2f}\n"
        f"🗓️ 1 Week Ago: ${w_debt:,.2f}\n\n"
        f"📈 Daily Increase: {format_billions(daily_inc)}\n"
        f"📆 Weekly Increase: {format_billions(weekly_inc)}\n\n"
        "#USDebt #DebtCrisis #FiscalReality"
    )

    return tweet


# ---------- Posting to X ----------

def post_to_x(text: str) -> dict:
    """
    Post a tweet via X API v2 using OAuth 1.0a User Context.
    Requires these env vars:
      - X_API_KEY
      - X_API_SECRET
      - X_ACCESS_TOKEN
      - X_ACCESS_TOKEN_SECRET
    """
    api_key = os.getenv("X_API_KEY")
    api_secret = os.getenv("X_API_SECRET")
    access_token = os.getenv("X_ACCESS_TOKEN")
    access_token_secret = os.getenv("X_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        raise RuntimeError("Missing one or more X OAuth 1.0a credentials.")

    # X API endpoint for creating a tweet
    url = "https://api.twitter.com/2/tweets"

    auth = OAuth1(
        api_key,
        api_secret,
        access_token,
        access_token_secret
    )

    payload = {"text": text}

    resp = requests.post(
        url,
        json=payload,
        auth=auth,
        timeout=20,
        verify=certifi.where(),
    )

    try:
        resp.raise_for_status()
    except Exception:
        raise RuntimeError(
            f"Failed to post tweet. "
            f"Status={resp.status_code}, Body={resp.text}"
        )

    return resp.json()


# ---------- Entrypoint ----------

def main():
    # 1. Build tweet from live data
    try:
        tweet = build_tweet_text()
    except Exception as e:
        print(f"[ERROR] Failed to build tweet: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. Always show preview in logs
    print("=== Generated Tweet Preview ===")
    print(tweet)
    print("================================")

    # 3. Dry run mode (for local tests / first CI runs)
    dry_run = os.getenv("DRY_RUN", "0")
    if dry_run == "1":
        print("[INFO] DRY_RUN=1 → not posting to X.")
        return

    # 4. Real post
    try:
        resp = post_to_x(tweet)
    except Exception as e:
        print(f"[ERROR] Failed to post tweet: {e}", file=sys.stderr)
        sys.exit(1)

    print("[INFO] Tweet posted successfully.")
    print(resp)


if __name__ == "__main__":
    main()