"""
Normalize -> rank -> summarize a raw Airbnb scrape (the slice's output).

Stage 2 of the retreat tool, kept standalone (not wired into Flask yet):
  1. normalize  : raw Apify Airbnb JSON -> clean comparable schema
  2. rank       : score each listing against user preferences (transparent, deterministic)
  3. summarize  : a short pros/cons per listing via Claude

Reads the most recent data/airbnb_raw_*.json (or a path passed as argv[1]).
The raw file should be one produced with scrapeAvailability=true so the
availability calendar is present.

Run:  python rank.py [path/to/airbnb_raw_*.json]
Needs: ANTHROPIC_API_KEY in .env (summaries are skipped gracefully if absent).
"""

import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv("/Users/meghanmccanney/retreat-tool/.env")

CLAUDE_MODEL = "claude-sonnet-4-6"  # fast + cheap for per-listing summaries; swap to opus for max quality


# ---------------------------------------------------------------------------
# User preferences (hardcoded example for now -- the Flask layer will supply these)
# ---------------------------------------------------------------------------
@dataclass
class Preferences:
    guest_count: int
    must_have_amenities: list                 # e.g. ["pool", "hot tub", "wifi"]
    max_total_price: int | None               # max price for the ENTIRE stay, in dollars
    location_priorities: list                 # ordered, most-preferred first


EXAMPLE_PREFS = Preferences(
    guest_count=11,
    must_have_amenities=["pool", "hot tub", "wifi", "kitchen"],
    max_total_price=25000,
    location_priorities=["La Quinta", "Palm Springs", "Indian Wells"],
)

# Ranking weights (sum to 1.0). Tune freely.
WEIGHTS = {"amenities": 0.33, "price": 0.27, "availability": 0.15, "reviews": 0.10, "bedrooms": 0.10, "location": 0.05}
REVIEW_CONFIDENCE_CAP = 25  # reviews needed before rating earns full reviews-score credit

MIN_BEDROOMS_FLOOR = 10     # hard-exclude anything under this many bedrooms
PENALTY_MULTIPLIER = 0.25   # score multiplier for 3+ structure compounds / resorts / hotels
# Structure rule: 1-2 separate structures OK (2 gets an info note); 3+ or resort/hotel penalized.
MAX_OK_STRUCTURES = 2


def bedroom_credit(bedrooms):
    """Bedroom scoring (0-1): 10 BR earns nothing; 11->0.5, 12->0.75, 13+->1.0."""
    if bedrooms is None or bedrooms <= 10:
        return 0.0
    return min(0.5 + (bedrooms - 11) * 0.25, 1.0)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def parse_bed_info(s):
    """'11 bedrooms, 13 beds, 9.5 baths' -> (bedrooms, beds, bathrooms).

    Handles observed variants: missing beds ('12 bedrooms, 8 baths'),
    worded beds ('13 king beds'), and 'Half-bath' (-> 0.5).
    """
    bedrooms = beds = bathrooms = None
    if not s:
        return bedrooms, beds, bathrooms
    for part in s.split(","):
        p = part.strip().lower()
        num = re.search(r"(\d+(?:\.\d+)?)", p)
        if "bedroom" in p:
            bedrooms = int(float(num.group(1))) if num else None
        elif "bath" in p:
            bathrooms = float(num.group(1)) if num else (0.5 if "half" in p else None)
        elif "bed" in p:  # 'beds' / 'king beds' (bedroom already handled above)
            beds = int(float(num.group(1))) if num else None
    return bedrooms, beds, bathrooms


def parse_price(s):
    """'$7,472' -> 7472 (int), or None."""
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", str(s))
    return int(digits) if digits else None


def stay_nights(check_in, check_out):
    """List of YYYY-MM-DD night strings (check-out day is not a night)."""
    try:
        ci = date.fromisoformat(check_in)
        co = date.fromisoformat(check_out)
    except (TypeError, ValueError):
        return []
    return [(ci + timedelta(days=i)).isoformat() for i in range((co - ci).days)]


def stay_window_from_raw(items):
    """Recover check-in/check-out from a listing's inputUrl query params."""
    for it in items:
        q = parse_qs(urlparse(it.get("inputUrl", "")).query)
        if "checkin" in q and "checkout" in q:
            return q["checkin"][0], q["checkout"][0]
    return None, None


def availability_confirmed(listing, nights):
    """True/False if the calendar covers all stay nights as bookable; None if unknown."""
    cal = listing.get("availability")
    if not cal or not nights:
        return None
    bookable = {e["date"]: e.get("bookable") for e in cal if "date" in e}
    if not all(n in bookable for n in nights):
        return None  # calendar doesn't cover the window -> can't confirm
    return all(bookable.get(n) for n in nights)


def available_amenities(listing):
    """Lowercased set of amenity titles marked available."""
    out = set()
    for a in listing.get("amenities") or []:
        if a.get("available") and a.get("title"):
            out.add(a["title"].lower())
    return out


# ---------------------------------------------------------------------------
# Normalize
# ---------------------------------------------------------------------------
def normalize_listing(raw, nights):
    bedrooms, beds, bathrooms = parse_bed_info(raw.get("bedInfo"))
    loc = raw.get("location") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("title"),
        "property_type": raw.get("propertyType"),
        "room_type": raw.get("roomType"),
        "description": raw.get("description") or "",
        "capacity": raw.get("maxGuestCapacity"),
        "bedrooms": bedrooms,
        "beds": beds,
        "bathrooms": bathrooms,
        "amenities": sorted(available_amenities(raw)),
        "photos": [p["url"] for p in (raw.get("photos") or []) if p.get("url")][:3],  # up to 3 for the strip
        "from_price": parse_price(raw.get("price")),  # generic 'from' price, NOT an exact dated total
        "availability_confirmed": availability_confirmed(raw, nights),
        "rating": raw.get("starRating"),
        "reviews_count": raw.get("reviewsCount"),
        "location_text": loc.get("title") or loc.get("address") or "",
        "listing_url": raw.get("propertyUrl"),
    }


def normalize(raw_items, check_in, check_out):
    nights = stay_nights(check_in, check_out)
    return [normalize_listing(r, nights) for r in raw_items]


# ---------------------------------------------------------------------------
# Rank
# ---------------------------------------------------------------------------
def _location_score(text, priorities):
    if not priorities:
        return 1.0, None
    t = (text or "").lower()
    for i, p in enumerate(priorities):
        if p.lower() in t:
            return 1.0 - i / len(priorities), p
    return 0.2, None  # in-region but not a named priority


def _price_score(from_price, budget, max_in_set):
    if from_price is None:
        return 0.5  # unknown -> neutral
    if budget:
        if from_price <= budget:
            return 0.5 + 0.5 * (budget - from_price) / budget   # 0.5..1.0, cheaper is better
        return max(0.0, 0.5 - 0.5 * (from_price - budget) / budget)  # over budget -> below 0.5
    # no budget: rank cheaper higher relative to the most expensive in the set
    return 1.0 - (from_price / max_in_set) if max_in_set else 0.5


def score_listing(n, prefs, max_in_set):
    """Return (score_0_100, breakdown, missing_must_haves, matched_priority, disqualified_reason)."""
    # Hard requirement: must physically fit the group.
    if n["capacity"] is not None and n["capacity"] < prefs.guest_count:
        return 0.0, {}, [], None, f"capacity {n['capacity']} < {prefs.guest_count} guests"
    # Hard requirement: bedroom floor (unparseable counts excluded too -- can't confirm the floor).
    if n["bedrooms"] is None:
        return 0.0, {}, [], None, "bedroom count unknown"
    if n["bedrooms"] < MIN_BEDROOMS_FLOOR:
        return 0.0, {}, [], None, f"{n['bedrooms']} bedrooms (under {MIN_BEDROOMS_FLOOR} floor)"
    # Hard requirement: not a hotel / boutique hotel / hotel room.
    if "hotel" in (n.get("room_type") or "").lower() or "hotel" in (n.get("property_type") or "").lower():
        return 0.0, {}, [], None, "hotel / boutique hotel (not a single-family home)"

    musts = [m.lower() for m in prefs.must_have_amenities]
    have = set(n["amenities"])
    matched = [m for m in musts if any(m in a for a in have)]
    missing = [m for m in musts if m not in matched]
    amenity_score = (len(matched) / len(musts)) if musts else 1.0

    price_score = _price_score(n["from_price"], prefs.max_total_price, max_in_set)
    loc_score, matched_priority = _location_score(n["location_text"] + " " + (n["name"] or ""),
                                                   prefs.location_priorities)
    avail = n["availability_confirmed"]
    avail_score = 1.0 if avail is True else (0.0 if avail is False else 0.5)

    # Reviews: rating scaled by review-count confidence, so a perfect score from a
    # handful of reviews can't pose as proven quality (0 reviews -> 0).
    rating = n["rating"] or 0
    reviews_count = n["reviews_count"] or 0
    reviews_score = (rating / 5.0) * (min(reviews_count, REVIEW_CONFIDENCE_CAP) / REVIEW_CONFIDENCE_CAP)

    breakdown = {
        "amenities": round(amenity_score, 3),
        "price": round(price_score, 3),
        "availability": round(avail_score, 3),
        "reviews": round(reviews_score, 3),
        "bedrooms": round(bedroom_credit(n["bedrooms"]), 3),
        "location": round(loc_score, 3),
    }
    total = sum(WEIGHTS[k] * v for k, v in breakdown.items())
    return round(total * 100, 1), breakdown, missing, matched_priority, None


def rank(normalized, prefs):
    prices = [n["from_price"] for n in normalized if n["from_price"]]
    max_in_set = max(prices) if prices else None
    ranked, excluded = [], []
    for n in normalized:
        score, breakdown, missing, matched_priority, dq = score_listing(n, prefs, max_in_set)
        rec = {**n, "score": score, "score_breakdown": breakdown,
               "missing_must_haves": missing, "matched_priority": matched_priority}
        if dq:
            rec["excluded_reason"] = dq
            excluded.append(rec)
        else:
            ranked.append(rec)
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked, excluded


# ---------------------------------------------------------------------------
# Summarize (Claude)
# ---------------------------------------------------------------------------
def claude_annotate(ranked, prefs):
    """Return {id: {"pros":[...], "cons":[...], "structure": {count,is_resort_or_hotel,detail}}}.

    {} if no key / on total failure. Classifies each listing's structure (single home vs
    multi-structure compound vs hotel/resort). One-shot retry re-requests any listing Claude drops.
    """
    if not os.environ.get("ANTHROPIC_API_KEY") or not ranked:
        return {}
    try:
        from anthropic import Anthropic
    except ImportError:
        return {}

    musts = [m.lower() for m in prefs.must_have_amenities]

    def amenities_for_prompt(ams):
        # Always include amenities that satisfy a must-have so Claude never misses one,
        # then a sample of the rest (capped to control tokens).
        matched = [a for a in ams if any(m in a for m in musts)]
        other = [a for a in ams if a not in matched]
        return matched + other[:40]

    def build_prompt(items):
        facts = [{
            "id": r["id"], "name": r["name"], "capacity": r["capacity"],
            "bedrooms": r["bedrooms"], "bathrooms": r["bathrooms"],
            "from_price": r["from_price"], "availability_confirmed": r["availability_confirmed"],
            "location": r["location_text"], "rating": r["rating"], "reviews": r["reviews_count"],
            "missing_must_haves": r["missing_must_haves"], "amenities": amenities_for_prompt(r["amenities"]),
            "shared_bedroom_risk": (r["bedrooms"] == 10),
            "description": (r["description"] or "")[:2000],
        } for r in items]
        return (
            "You are helping plan a group retreat where everyone ideally gets their own bedroom in a "
            "single-family home. Given user preferences and listings, for EACH listing write a SHORT "
            "pros/cons AND classify its structure.\n\n"
            f"Preferences:\n{json.dumps(prefs.__dict__, indent=2)}\n\n"
            f"Listings:\n{json.dumps(facts, indent=2)}\n\n"
            "Rules for pros/cons:\n"
            "- 2-3 concise pros and 2-3 concise cons per listing, grounded ONLY in the data.\n"
            "- `from_price` is a GENERIC 'from' price, NOT an exact dated total -- never present it "
            "as the final stay cost; if it's near/over the user's max_total_price, flag that as a con.\n"
            "- `missing_must_haves` is AUTHORITATIVE: a must-have amenity is present unless it appears "
            "there. Never claim a present must-have is missing or 'not listed' (the amenity sample may "
            "be partial).\n"
            "- Call out missing must-have amenities (only those in missing_must_haves), capacity headroom "
            "vs guest_count, and whether the location matches the user's priorities.\n"
            "- If `shared_bedroom_risk` is true, you MUST include a con noting that at 10 bedrooms for a "
            "large group at least one room will likely need to be shared.\n\n"
            "Rules for structure classification (from title + description):\n"
            "- `count`: the number of SEPARATE sleeping buildings rented together. A single house (even "
            "large, even with internal wings) = 1. EACH detached casita, guest house, or separately-named "
            "residence counts as its OWN building: main house + 1 casita = 2; main house + 2 detached "
            "casitas = 3; two villas booked together = 2; three or more separate residences = 3+.\n"
            "- `is_resort_or_hotel`: true if the property is a hotel, resort, or hotel-STYLE operation -- "
            "EVEN IF it markets itself as a 'private villa', 'whole villa', or 'entire home', and even if it "
            "claims 'not a hotel'. Self-described privacy does NOT override hotel signals. Treat ANY of "
            "these as a hotel signal:\n"
            '    * named suites within the property (e.g. "Sofia\'s Suite", "The Cabana Suite");\n'
            "    * a named hospitality manager, concierge, or on-site guest-services staff;\n"
            "    * 'hotel-grade' branding, or guest rooms described as cabanas/suites opening onto a "
            "communal pool or courtyard;\n"
            "    * the same operator/brand running multiple listings.\n"
            '  (Generic marketing like "resort-style pool" or "estate" ALONE, with none of the above, is '
            "NOT enough -- don't flag a genuine single home for that.)\n"
            "- `detail`: when count >= 2 OR is_resort_or_hotel, a SHORT phrase citing the CONCRETE signal "
            "(e.g. 'boutique-hotel operation: named suites + hospitality manager', 'two villas booked "
            "together: Villa 16 + Villa 18', 'three separately-named residences'); otherwise null.\n"
            "- If count >= 3 OR is_resort_or_hotel, ALSO include a con stating it is a hotel / hotel-style "
            "operation (or multi-structure compound), not a single-family home, citing the detail.\n\n"
            "Respond with ONLY a JSON array, no prose, no code fences:\n"
            '[{"id": <id>, "pros": [..], "cons": [..], '
            '"structure": {"count": <int>, "is_resort_or_hotel": <bool>, "detail": <string or null>}}]'
        )

    client = Anthropic()

    def call(items):
        try:
            msg = client.messages.create(
                model=CLAUDE_MODEL, max_tokens=6000,
                messages=[{"role": "user", "content": build_prompt(items)}],
            )
            text = msg.content[0].text.strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
            arr = json.loads(text)
            return {item["id"]: {"pros": item.get("pros", []), "cons": item.get("cons", []),
                                 "structure": item.get("structure") or {}}
                    for item in arr}
        except Exception:  # API or parse failure -> degrade gracefully
            return {}

    result = call(ranked)
    missing = [r for r in ranked if r["id"] not in result]
    if missing and len(missing) < len(ranked):  # partial drop -> one-shot retry for the stragglers
        result.update(call(missing))
    return result


def annotate_and_adjust(ranked, prefs):
    """Attach Claude pros/cons + structure, apply the 3+/resort penalty, note 2-structure
    properties, guarantee mandatory cons, then re-sort. Mutates and returns `ranked`."""
    ann = claude_annotate(ranked, prefs)
    for r in ranked:
        a = ann.get(r["id"], {})
        cons = list(a.get("cons", []))
        structure = a.get("structure") or {}
        count = structure.get("count")
        is_rh = bool(structure.get("is_resort_or_hotel"))
        detail = structure.get("detail")

        # Penalty: 3+ separate structures, or an actual resort/hotel buyout. 1-2 structures are fine.
        if (isinstance(count, int) and count > MAX_OK_STRUCTURES) or is_rh:
            r["score"] = round(r["score"] * PENALTY_MULTIPLIER, 1)
            r["structure_flag"] = "resort/hotel" if is_rh else f"{count}-structure compound"
            r["structure_detail"] = detail
            if not any(w in c.lower() for c in cons
                       for w in ("compound", "resort", "single-family", "separate structure", "hotel")):
                what = "a resort / hotel buyout" if is_rh else f"a {count}-structure compound"
                why = f" — {detail}" if detail else ""
                cons.insert(0, f"Appears to be {what}, not a single-family home{why}.")
        # Allowed but worth noting: exactly 2 structures -> the group splits across two buildings.
        elif count == 2:
            r["two_structure_detail"] = detail
            if not any("split" in c.lower() and "structure" in c.lower() for c in cons):
                why = f" — {detail}" if detail else ""
                cons.append(f"Group would split across 2 separate structures{why}.")

        # Mandatory shared-room con for 10-BR listings (safety net if Claude omitted it).
        if r.get("bedrooms") == 10 and not any("shared" in c.lower() and "room" in c.lower() for c in cons):
            cons.append("At 10 bedrooms for a large group, at least one room will likely need to be shared.")

        # Fallback: if Claude omitted this listing entirely, don't leave the card bare.
        pros = a.get("pros", [])
        if r["id"] not in ann and not cons:
            cons = ["Detailed summary unavailable — review the listing directly."]

        r["summary"] = {"pros": pros, "cons": cons}

    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def latest_raw_file():
    files = sorted(glob.glob("/Users/meghanmccanney/retreat-tool/data/airbnb_raw_*.json"))
    return files[-1] if files else None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else latest_raw_file()
    if not path or not os.path.exists(path):
        sys.exit("ERROR: no raw scrape file found. Run search_slice.py first.")

    raw_items = json.load(open(path))
    check_in, check_out = stay_window_from_raw(raw_items)
    prefs = EXAMPLE_PREFS

    print(f"Source     : {os.path.basename(path)}  ({len(raw_items)} listings)")
    print(f"Stay window: {check_in} -> {check_out}")
    print(f"Preferences: {prefs.guest_count} guests, must-haves={prefs.must_have_amenities}, "
          f"max ${prefs.max_total_price:,}/stay, priorities={prefs.location_priorities}\n")

    normalized = normalize(raw_items, check_in, check_out)
    ranked, excluded = rank(normalized, prefs)
    ranked = annotate_and_adjust(ranked, prefs)

    print("NOTE: 'from price' is a starting price, not an exact dated total. "
          "Click through to Airbnb for the final quote.\n")

    for i, r in enumerate(ranked, 1):
        price = f"${r['from_price']:,}" if r["from_price"] else "n/a"
        avail = {True: "confirmed", False: "NOT avail", None: "unknown"}[r["availability_confirmed"]]
        flag = f"  [PENALIZED: {r['structure_flag']}]" if r.get("structure_flag") else ""
        print(f"#{i}  [{r['score']:5.1f}]  {r['name']}{flag}")
        print(f"      {r['capacity']} guests | {r['bedrooms']} BR / {r['bathrooms']} BA | "
              f"from {price} | avail: {avail} | {r['rating']}★ ({r['reviews_count']}) | {r['location_text']}")
        if r["missing_must_haves"]:
            print(f"      missing must-haves: {', '.join(r['missing_must_haves'])}")
        s = r.get("summary") or {}
        for p in s.get("pros", []):
            print(f"      + {p}")
        for c in s.get("cons", []):
            print(f"      - {c}")
        print(f"      {r['listing_url']}")
        print()

    if excluded:
        print(f"Excluded ({len(excluded)}):")
        for r in excluded:
            print(f"  - {r['name']}: {r['excluded_reason']}")

    out = {"stay": {"check_in": check_in, "check_out": check_out},
           "preferences": prefs.__dict__, "ranked": ranked, "excluded": excluded}
    out_path = "/Users/meghanmccanney/retreat-tool/data/ranked_output.json"
    json.dump(out, open(out_path, "w"), indent=2)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
