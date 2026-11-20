import csv
import io
from datetime import datetime, timedelta

START_KEYS = {"début", "debut", "arrivée", "arrivee", "start", "du"}
END_KEYS = {"fin", "départ", "depart", "end", "au"}
MAX_FILE_ROWS = 100
DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y")


def parse_date(text):
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"date illisible « {text} » (format attendu : jj/mm/aaaa)")


def check_period(start, end, today):
    """Retourne un message d'erreur en français, ou None si la période est valable."""
    if end < start:
        return "La date de départ doit être après l’arrivée."
    if (end - start).days > 60:
        return "Une période ne peut pas dépasser 60 nuits."
    if start < today:
        return "La période ne peut pas commencer dans le passé."
    if start > today + timedelta(days=730):
        return "Choisissez une date dans les deux prochaines années."
    return None


def parse_periods(text, today):
    """Lit un CSV « début;fin ». Retourne (périodes valables, messages d'erreur par ligne)."""
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        raise ValueError("Le fichier est vide.")
    delimiter = ";" if lines[0].count(";") >= lines[0].count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    names = [h.strip().lower() for h in next(reader)]
    start_col = next((i for i, n in enumerate(names) if n in START_KEYS), None)
    end_col = next((i for i, n in enumerate(names) if n in END_KEYS), None)
    if start_col is None or end_col is None:
        raise ValueError("En-tête attendu dans la première ligne : début;fin")
    periods, errors, seen = [], [], set()
    for number, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        if number - 1 > MAX_FILE_ROWS:
            raise ValueError(f"Maximum {MAX_FILE_ROWS} lignes par fichier.")
        try:
            start, end = parse_date(row[start_col]), parse_date(row[end_col])
        except IndexError:
            errors.append(f"Ligne {number} : colonnes manquantes.")
            continue
        except ValueError as error:
            errors.append(f"Ligne {number} : {error}.")
            continue
        problem = check_period(start, end, today)
        if problem:
            errors.append(f"Ligne {number} : {problem}")
        elif (start, end) not in seen:
            seen.add((start, end))
            periods.append((start, end))
    return periods, errors
