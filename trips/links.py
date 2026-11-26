from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .calendar_utils import MONTHS_FR

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

SNCF_CONNECT_URL = "https://www.sncf-connect.com/"

INTERRAIL_TZ = ZoneInfo("Europe/Paris")     # fuseau de « 6 h 00 » : à changer si besoin
INTERRAIL_URL = ("https://www.interrail.com/en/book-reservations?t={t}&ol={origin}&dl={dest}"
                 "&vl=&vv=&ar=false&tv=1&mct=1")
INTERRAIL_URL_UNDATED = ("https://www.interrail.com/en/book-reservations?ol={origin}&dl={dest}"
                         "&vl=&vv=&ar=false&tv=1&mct=1")


def interrail_timestamp_ms(day):
    """6 h 00 (heure de Paris) du jour donné, en nanosecondes depuis le 1er janvier 1970 UTC."""
    moment = datetime(day.year, day.month, day.day, 6, 0, tzinfo=INTERRAIL_TZ)
    return int(moment.timestamp()) * 1_000


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


def train_links(origin, destination, station="", day=None):
    target = station.strip() or destination
    values = {"origin": _q(origin), "dest": _q(target)}
    links = [{"name": p["name"], "url": p["url"].format(**values)} for p in TRAIN_PROVIDERS]
    if day is None:       # sans période, on ne peut pas calculer la date : lien sans « t= »
        url = INTERRAIL_URL_UNDATED.format(**values)
    else:
        url = INTERRAIL_URL.format(t=interrail_timestamp_ms(day), **values)
    links.append({"name": "Interrail", "url": url})
    return links


def sncf_phrase(origin, destination, station="", day=None):
    """Phrase à copier dans la recherche de SNCF Connect."""
    target = station.strip() or destination
    text = f"{origin} {target}"
    if day:
        text += f" le {day.day} {MONTHS_FR[day.month - 1]}"
    return text
