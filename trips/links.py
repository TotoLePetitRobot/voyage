from urllib.parse import quote

# Gabarits modifiables : {dest}, {adults}, {checkin}, {checkout}, {origin}.
LODGING_PROVIDERS = [
    {
        "name": "Booking.com",
        "dated": "https://www.booking.com/searchresults.html?ss={dest}&checkin={checkin}"
                 "&checkout={checkout}&group_adults={adults}&no_rooms=1&selected_currency=EUR",
        "undated": "https://www.booking.com/searchresults.html?ss={dest}"
                   "&group_adults={adults}&no_rooms=1&selected_currency=EUR",
    },
    {
        "name": "Airbnb",
        "dated": "https://www.airbnb.fr/s/{dest}/homes?checkin={checkin}"
                 "&checkout={checkout}&adults={adults}",
        "undated": "https://www.airbnb.fr/s/{dest}/homes?adults={adults}",
    },
]

TRAIN_PROVIDERS = [
    {
        "name": "Google Maps (transports en commun)",
        "url": "https://www.google.com/maps/dir/?api=1&origin={origin}"
               "&destination={dest}&travelmode=transit",
    },
]


def _q(value):
    return quote(str(value), safe="")


def lodging_links(destination, start, end, adults):
    """Retourne (liens, avec_dates). Sans période valable (au moins une nuit),
    les liens sont générés sans dates."""
    adults = max(1, min(int(adults), 30))
    dated = start is not None and end is not None and (end - start).days >= 1
    values = {"dest": _q(destination), "adults": adults}
    if dated:
        values.update(checkin=start.isoformat(), checkout=end.isoformat())
    key = "dated" if dated else "undated"
    return [{"name": p["name"], "url": p[key].format(**values)}
            for p in LODGING_PROVIDERS], dated


def train_links(origin, destination):
    values = {"origin": _q(origin), "dest": _q(destination)}
    return [{"name": p["name"], "url": p["url"].format(**values)} for p in TRAIN_PROVIDERS]
