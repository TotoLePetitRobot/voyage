import calendar

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _add_months(year, month, count):
    index = year * 12 + (month - 1) + count
    return index // 12, index % 12 + 1

def build_calendar(options, today, months_count=6, offset=0):
    """Retourne (mois, légende). La fenêtre commence `offset` mois après le mois courant
    et couvre `months_count` mois. La légende contient toutes les périodes."""
    legend = list(zip(LETTERS, options))
    cal = calendar.Calendar(firstweekday=0)
    months, (y, m) = [], _add_months(today.year, today.month, offset)
    for _ in range(months_count):
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
    return months, legend
