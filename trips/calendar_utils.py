import calendar

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _add_months(year, month, count):
    index = year * 12 + (month - 1) + count
    return index // 12, index % 12 + 1


def build_calendar(options, today, min_months=12, max_months=24):
    """Retourne (mois, légende, tronqué). Les mois vont d'aujourd'hui (ou de la première période,
    si elle est plus ancienne) jusqu'à `min_months` mois plus tard (ou la fin de la dernière période)."""
    legend = list(zip(LETTERS, options))
    first = (today.year, today.month)
    last = _add_months(today.year, today.month, min_months - 1)
    if legend:
        earliest = min(o.start_date for _, o in legend)
        latest = max(o.end_date for _, o in legend)
        first = min(first, (earliest.year, earliest.month))
        last = max(last, (latest.year, latest.month))
    cal = calendar.Calendar(firstweekday=0)
    months, (y, m) = [], first
    while (y, m) <= last and len(months) < max_months:
        weeks = []
        for week in cal.monthdatescalendar(y, m):
            cells = []
            for d in week:
                if d.month != m:
                    cells.append(None)
                else:
                    cells.append({
                        "day": d.day,
                        "iso": d.isoformat(),
                        "label": f"{d.day} {MONTHS_FR[d.month - 1]} {d.year}",
                        "past": d < today,
                        "letters": [l for l, o in legend if o.start_date <= d <= o.end_date],
                    })
            weeks.append(cells)
        months.append({"title": f"{MONTHS_FR[m - 1]} {y}", "weeks": weeks})
        y, m = _add_months(y, m, 1)
    return months, legend, (y, m) <= last
