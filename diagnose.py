"""
Diagnostic: why is curious_coder/airbnb-scraper returning 0 listings?

Two things the run-sync slice hid from us:
  1. Whether the Actor returns data AT ALL in our setup (control query).
  2. The run's STATUS, STATS, and LOG -- did it find 0, get blocked, or fail?

We run a dead-simple control query (Los Angeles, no date/guest/bedroom filters)
via the async run API so we can inspect the run object and its log, instead of
only seeing an empty dataset.

Run:  python diagnose.py
"""

import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

APIFY_TOKEN = os.environ.get("APIFY_TOKEN")
ACTOR_ID = "curious_coder~airbnb-scraper"
BASE = "https://api.apify.com/v2"

# Dead-simple control: a single well-known location, no filters, just expand details.
CONTROL_INPUT = {
    "urls": ["https://www.airbnb.com/s/Los-Angeles--CA/homes"],
    "scrapeDetail": True,
    "count": 10,
}


def main():
    if not APIFY_TOKEN:
        sys.exit("ERROR: APIFY_TOKEN is not set.")

    params = {"token": APIFY_TOKEN}

    # 1. Start the run (async) so we get a run object back, not just dataset items.
    print(f"Starting control run for {ACTOR_ID} ...")
    start = requests.post(f"{BASE}/acts/{ACTOR_ID}/runs", params=params, json=CONTROL_INPUT, timeout=60)
    if start.status_code >= 400:
        sys.exit(f"ERROR starting run: HTTP {start.status_code}\n{start.text}")
    run = start.json()["data"]
    run_id = run["id"]
    dataset_id = run["defaultDatasetId"]
    print(f"  runId={run_id}  datasetId={dataset_id}\n")

    # 2. Poll until the run reaches a terminal state.
    terminal = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
    status = run["status"]
    waited = 0
    while status not in terminal and waited < 300:
        time.sleep(5)
        waited += 5
        r = requests.get(f"{BASE}/actor-runs/{run_id}", params=params, timeout=60)
        run = r.json()["data"]
        status = run["status"]
        print(f"  [{waited:3d}s] status={status}")

    # 3. Report the run's own accounting of what happened.
    stats = run.get("stats", {})
    print(f"\nFinal status : {status}")
    print(f"Stats        : requests finished={stats.get('requestsFinished')}, "
          f"failed={stats.get('requestsFailed')}, "
          f"runtime={stats.get('runTimeSecs')}s")

    # 4. How many items actually landed in the dataset?
    info = requests.get(f"{BASE}/datasets/{dataset_id}", params=params, timeout=60).json()["data"]
    print(f"Dataset items: {info.get('itemCount')}")

    # 5. Tail of the run log -- this is where 'blocked', 'captcha', '0 found' show up.
    log = requests.get(f"{BASE}/actor-runs/{run_id}/log", params=params, timeout=60).text
    tail = "\n".join(log.splitlines()[-40:])
    print("\n--- last 40 log lines ----------------------------------------------")
    print(tail)
    print("--------------------------------------------------------------------")


if __name__ == "__main__":
    main()
