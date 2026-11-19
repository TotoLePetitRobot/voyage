import csv, io
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

NAME_KEYS = {"nom", "name", "destination"}
URL_KEYS = {"lien", "url"}
NOTE_KEYS = {"notes", "note"}
STATION_KEYS = {"gare", "station", "gare la plus proche"}
MAX_ROWS = 200
_url_validator = URLValidator(schemes=["http", "https"])


def _clean_url(value):
    if len(value) > 200:
        return ""
    try:
        _url_validator(value)
    except ValidationError:
        return ""
    return value


def parse_destinations(text):
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise ValueError("Le fichier est vide.")
    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [h for h in (reader.fieldnames or []) if h]
    norm = {h: h.strip().lower() for h in headers}
    name_col = next((h for h in headers if norm[h] in NAME_KEYS), None)
    if name_col is None:
        raise ValueError("Colonne « nom » introuvable dans la première ligne.")

    rows = []
    for raw in reader:
        name = (raw.get(name_col) or "").strip()[:120]
        if not name:
            continue
        if len(rows) >= MAX_ROWS:
            raise ValueError(f"Maximum {MAX_ROWS} destinations par fichier.")
        url, notes, station, extra = "", "", "", {}
        for h in headers:
            if h == name_col:
                continue
            value = (raw.get(h) or "").strip()
            if not value:
                continue
            col = norm[h]
            if col in URL_KEYS:
                url = _clean_url(value)
            elif col in NOTE_KEYS:
                notes = value
            elif col in STATION_KEYS:
                parts = [p.strip()[:120] for p in value.split("|") if p.strip()]
                station = "\n".join(parts[:5])
            else:
                extra[h.strip()] = value
        rows.append({"name": name, "url": url, "notes": notes, "station": station, "extra": extra})
    return rows
