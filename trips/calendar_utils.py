import calendar

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def build_calendar(options, max_months=12):
    """Retourne (mois, légende, tronqué) pour une liste de périodes (26 maximum)."""
    legend = list(zip(LETTERS, options))
    if not legend:
        return [], [], False
    first = min(o.start_date for _, o in legend)
    last = max(o.end_date for _, o in legend)
    cal = calendar.Calendar(firstweekday=0)
    months, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month) and len(months) < max_months:
        weeks = []
        for week in cal.monthdatescalendar(y, m):
            cells = []
            for d in week:
                if d.month != m:
                    cells.append(None)
                else:
                    letters = [l for l, o in legend if o.start_date <= d <= o.end_date]
                    cells.append({"day": d.day, "letters": letters})
            weeks.append(cells)
        months.append({"title": f"{MONTHS_FR[m - 1]} {y}", "weeks": weeks})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    truncated = (y, m) <= (last.year, last.month)
    return months, legend, truncated
