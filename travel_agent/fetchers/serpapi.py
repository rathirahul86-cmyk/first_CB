"""
Serpapi Google Flights wrapper.

Docs: https://serpapi.com/google-flights-api
"""

import os
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Map cabin name → Serpapi travel_class integer
_CABIN_MAP = {
    "economy":          1,
    "premium_economy":  2,
    "business":         3,
    "first":            4,
}

# Map sort_by name → Serpapi sort_by integer (None = omit param, default = best/price)
_SORT_MAP = {
    "price":    None,   # default Serpapi ordering — omit the param
    "duration": 1,
}

SERPAPI_BASE = "https://serpapi.com/search"


def _airline_code_from_flight_number(flight_number: str) -> str:
    """Extract 2-letter IATA airline code from flight number like 'EK 203'."""
    m = re.match(r"([A-Z]{2})\s*\d", flight_number or "")
    return m.group(1) if m else ""


def _extract_flights(raw_list: list, trip: dict, outbound_date: str,
                     return_date: Optional[str]) -> list[dict]:
    preferred = set(trip.get("preferred_airlines", []))
    max_stops = trip.get("max_stops", 99)
    travelers = trip.get("travelers", 1)
    results = []

    for item in (raw_list or []):
        stops = len(item.get("layovers", []))
        if stops > max_stops:
            continue

        # Price is per person for the full itinerary
        price = item.get("price")
        if price is None:
            continue

        # Duration: Serpapi gives total_duration in minutes at top level
        duration_minutes = item.get("total_duration")

        # Flight legs for airline info
        flights_legs = item.get("flights", [])
        if not flights_legs:
            continue
        first_leg = flights_legs[0]
        flight_number = first_leg.get("flight_number", "")
        airline_name = first_leg.get("airline", "")
        airline_code = _airline_code_from_flight_number(flight_number)

        is_preferred = airline_code in preferred

        # Build a stable ID
        flight_id = f"serpapi::{trip['id']}::{outbound_date}::{airline_code}::{flight_number.replace(' ', '')}"
        if return_date:
            flight_id += f"::{return_date}"

        # URL from Serpapi (booking_token or carbon_emissions link as fallback)
        booking_token = item.get("booking_token", "")
        url = (
            f"https://www.google.com/travel/flights?tfs={booking_token}"
            if booking_token
            else "https://www.google.com/travel/flights"
        )

        results.append({
            "id":               flight_id,
            "trip_id":          trip["id"],
            "origin":           trip["origin"],
            "destination":      trip["destination"],
            "price_per_person": float(price),
            "total_price":      float(price) * travelers,
            "outbound_date":    outbound_date,
            "return_date":      return_date,
            "duration_minutes": duration_minutes,
            "stops":            stops,
            "airline":          airline_name,
            "airline_code":     airline_code,
            "flight_number":    flight_number,
            "url":              url,
            "source":           "google_flights",
            "scanned_at":       datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "is_preferred":     is_preferred,
        })

    return results


def fetch_serpapi_flights(trip: dict, outbound_date: str,
                          return_date: Optional[str] = None) -> list[dict]:
    """
    Fetch flights from Serpapi Google Flights API for a single date pair.
    Returns a list of normalized flight dicts.
    """
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        raise EnvironmentError("SERPAPI_KEY environment variable is not set.")

    cabin_int = _CABIN_MAP.get(trip.get("cabin", "economy"), 1)
    sort_int  = _SORT_MAP.get(trip.get("sort_by", "price"), 0)
    trip_type = 2 if trip.get("one_way", True) else 1  # 1=round, 2=one-way

    params: dict = {
        "engine":         "google_flights",
        "api_key":        api_key,
        "departure_id":   trip["origin"],
        "arrival_id":     trip["destination"],
        "outbound_date":  outbound_date,
        "adults":         trip.get("travelers", 1),
        "travel_class":   cabin_int,
        "type":           trip_type,
        "currency":       "USD",
        "hl":             "en",
    }
    if sort_int is not None:
        params["sort_by"] = sort_int

    if not trip.get("one_way", True) and return_date:
        params["return_date"] = return_date

    logger.debug("Serpapi request: %s → %s on %s (return: %s)",
                 trip["origin"], trip["destination"], outbound_date, return_date)

    resp = requests.get(SERPAPI_BASE, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"Serpapi error: {data['error']}")

    best_flights  = data.get("best_flights", [])
    other_flights = data.get("other_flights", [])

    return _extract_flights(best_flights + other_flights, trip, outbound_date, return_date)
