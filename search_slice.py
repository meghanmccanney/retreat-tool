"""
Airbnb search via the Apify curious_coder/airbnb-scraper Actor.

Exposes search_airbnb() for the Flask app to call live, and keeps a runnable
main() for standalone testing (writes raw JSON to data/ for inspection).

Actor: curious_coder/airbnb-scraper (5.0 rated, URL-based input model).
We build an Airbnb search URL (path-slug form, verified working) and hand it to
the Actor; scrapeDetail+scrapeAvailability expand each result into full details
plus a 365-day availability calendar.

Run standalone:  python search_slice.py
Needs: APIFY_TOKEN in .env
"""

import json
import os
import sys
from datetime import datetime
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

# --- Apify config -----------------------------------------------------------
APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
ACTOR_ID = "curious_coder~airbnb-scraper"  # store id curious_coder/airbnb-scraper, '/' -> '~' in the API
RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"

OUT_DIR = "data"


# Airbnb's bedroom filter tops out at "8+", so a higher value passed in the URL
# returns nothing. We cap the URL filter here; finer bedroom needs are handled downstream.
AIRBNB_MIN_BEDROOMS_CAP = 8


def build_airbnb_search_url(location, check_in, check_out, min_bedrooms=None, adults=None):
    """Build an Airbnb search URL in the path-slug form the Actor resolves.

    "La Quinta, CA" -> https://www.airbnb.com/s/La-Quinta--CA/homes?checkin=...
    The ?query= and percent-encoded path forms both returned 0 results.
    """
    slug = "--".join(part.strip().replace(" ", "-") for part in location.split(","))
    params = {"checkin": check_in, "checkout": check_out}
    if adults:
        params["adults"] = adults
    if min_bedrooms:
        params["min_bedrooms"] = min(int(min_bedrooms), AIRBNB_MIN_BEDROOMS_CAP)
    return f"https://www.airbnb.com/s/{slug}/homes?{urlencode(params)}"


def search_airbnb(location, check_in, check_out, min_bedrooms=None, adults=None, count=10, save=False):
    """Run the Actor synchronously and return (raw_items, search_url).

    Raises RuntimeError on missing token or an Apify error (caller handles it).
    """
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN is not set. Add it to your .env file.")

    search_url = build_airbnb_search_url(location, check_in, check_out, min_bedrooms, adults)
    payload = {
        "urls": [search_url],
        "scrapeDetail": True,
        "scrapeAvailability": True,   # adds the per-date availability calendar
        "scrapeReviews": False,
        "currency": "USD",
        "checkInDate": check_in,      # fallback if dates aren't read from the URL
        "checkOutDate": check_out,
        "count": count,               # schema minimum is 10
    }

    try:
        resp = requests.post(RUN_URL, params={"token": APIFY_TOKEN}, json=payload, timeout=300)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Request to Apify failed: {e}")

    if resp.status_code >= 400:
        raise RuntimeError(f"Apify returned HTTP {resp.status_code}: {resp.text}")

    items = resp.json()

    if save:
        os.makedirs(OUT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(OUT_DIR, f"airbnb_raw_{stamp}.json")
        with open(out_path, "w") as f:
            json.dump(items, f, indent=2)
        print(f"Wrote {len(items)} listings to {out_path}")

    return items, search_url


# --- Standalone test search -------------------------------------------------
def main():
    location, check_in, check_out, min_bedrooms = "La Quinta, CA", "2026-08-30", "2026-09-04", 11
    print(f"Searching {location}  {check_in} -> {check_out}  {min_bedrooms}+ BR (URL filter capped at 8)")
    print("Runs synchronously, may take 1-3 minutes...")
    try:
        items, url = search_airbnb(location, check_in, check_out, min_bedrooms=min_bedrooms, save=True)
    except RuntimeError as e:
        sys.exit(f"ERROR: {e}")

    print(f"URL: {url}\n")
    if items:
        for x in items:
            print(f"  cap={x.get('maxGuestCapacity')!s:>3}  price={x.get('price')!s:>10}  "
                  f"[{x.get('bedInfo')}]  {str(x.get('title'))[:45]}")
    else:
        print("WARNING: zero listings returned. Try different dates.")


if __name__ == "__main__":
    main()
