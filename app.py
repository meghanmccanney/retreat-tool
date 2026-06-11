"""
Retreat tool — Flask app wiring stage 1 (Airbnb search) + stage 2 (rank/summarize).

Live, synchronous search: submitting the form runs a real Apify scrape (1-3 min),
then normalizes, ranks, and summarizes results against the entered preferences.

Run:  venv/bin/python app.py   (then open http://127.0.0.1:5000)
Needs: APIFY_TOKEN and ANTHROPIC_API_KEY in .env
"""

from flask import Flask, render_template, request

from rank import Preferences, annotate_and_adjust, normalize, rank
from search_slice import search_airbnb

app = Flask(__name__)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    # --- read + validate the form ---
    location = (request.form.get("location") or "").strip()
    check_in = (request.form.get("check_in") or "").strip()
    check_out = (request.form.get("check_out") or "").strip()
    guests_raw = (request.form.get("guests") or "").strip()
    min_bedrooms_raw = (request.form.get("min_bedrooms") or "").strip()
    amenities_raw = request.form.get("amenities") or ""
    max_price_raw = (request.form.get("max_price") or "").strip()

    errors = []
    if not location:
        errors.append("Location is required.")
    if not check_in or not check_out:
        errors.append("Check-in and check-out dates are required.")
    if check_in and check_out and check_out <= check_in:
        errors.append("Check-out must be after check-in.")
    try:
        guests = int(guests_raw)
        if guests < 1:
            raise ValueError
    except ValueError:
        guests = None
        errors.append("Guests must be a whole number of 1 or more.")
    try:
        min_bedrooms = int(min_bedrooms_raw)
        if min_bedrooms < 1:
            raise ValueError
    except ValueError:
        min_bedrooms = None
        errors.append("Minimum bedrooms must be a whole number of 1 or more.")
    max_price = None
    if max_price_raw:
        try:
            max_price = int(max_price_raw.replace(",", "").replace("$", ""))
        except ValueError:
            errors.append("Max price must be a number.")

    form = {"location": location, "check_in": check_in, "check_out": check_out,
            "guests": guests_raw, "min_bedrooms": min_bedrooms_raw,
            "amenities": amenities_raw, "max_price": max_price_raw}
    if errors:
        return render_template("index.html", errors=errors, form=form)

    must_haves = [a.strip() for a in amenities_raw.split(",") if a.strip()]
    prefs = Preferences(
        guest_count=guests,
        must_have_amenities=must_haves,
        max_total_price=max_price,
        # single search location -> use the city token as the lone priority
        location_priorities=[location.split(",")[0].strip()],
    )

    # --- live search -> rank -> summarize (the 1-3 min step) ---
    # min_bedrooms drives the Airbnb search filter (capped at 8 in the URL); guest count
    # is NOT a search filter -- it only feeds the ranker/exclusion via prefs above.
    try:
        raw_items, search_url = search_airbnb(location, check_in, check_out, min_bedrooms=min_bedrooms)
    except RuntimeError as e:
        return render_template("index.html", errors=[f"Search failed: {e}"], form=form)

    normalized = normalize(raw_items, check_in, check_out)
    ranked, excluded = rank(normalized, prefs)
    ranked = annotate_and_adjust(ranked, prefs)

    return render_template(
        "results.html",
        ranked=ranked, excluded=excluded, prefs=prefs,
        check_in=check_in, check_out=check_out, location=location,
        search_url=search_url,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
