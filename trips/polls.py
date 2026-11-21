def _int(value):
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 9 else None


def parse_options(text, minimum=2, maximum=15, max_len=120):
    """Une option par ligne. Les doublons (sans tenir compte de la casse) sont ignorés."""
    seen, options = set(), []
    for line in text.splitlines():
        option = " ".join(line.split())
        if not option:
            continue
        if len(option) > max_len:
            raise ValueError(f"« {option[:20]}… » dépasse {max_len} caractères.")
        if option.casefold() in seen:
            continue
        seen.add(option.casefold())
        options.append(option)
    if len(options) < minimum:
        raise ValueError(f"Indiquez au moins {minimum} options différentes (une par ligne).")
    if len(options) > maximum:
        raise ValueError(f"{maximum} options au maximum.")
    return options


def votes_from_post(kind, data, option_ids):
    """Retourne {option: score} à enregistrer. Tout ce qui n'y figure pas est retiré."""
    valid = set(option_ids)
    if kind == "single":
        pick = _int(data.get("choice"))
        return {pick: 1} if pick in valid else {}
    if kind == "multiple":
        return {pk: 1 for pk in {_int(v) for v in data.getlist("choice")} if pk in valid}
    votes = {}
    for pk in option_ids:
        if data.get(f"opt_{pk}", "") in ("0", "1", "2"):
            votes[pk] = int(data.get(f"opt_{pk}"))
    return votes


def tally(kind, option_ids, votes):
    """votes : (option, membre, score). Retourne (résultats triés, nombre de votants)."""
    rows = {i: {"id": i, "votes": 0, "yes": 0, "maybe": 0, "no": 0, "total": 0}
            for i in option_ids}
    voters = set()
    for option, member, score in votes:
        row = rows.get(option)
        if row is None:
            continue
        voters.add(member)
        row["votes"] += 1
        if kind == "scale":
            row["total"] += score
            row["yes" if score == 2 else "maybe" if score == 1 else "no"] += 1
    position = {i: n for n, i in enumerate(option_ids)}
    if kind == "scale":
        def key(r):
            return (-r["total"], -r["yes"], position[r["id"]])
        best = max((r["total"] for r in rows.values()), default=0)
    else:
        def key(r):
            return (-r["votes"], position[r["id"]])
        best = max((r["votes"] for r in rows.values()), default=0)
    ordered = sorted(rows.values(), key=key)
    for r in ordered:
        r["percent"] = (round(100 * r["votes"] / len(voters))
                        if voters and kind != "scale" else None)
        value = r["total"] if kind == "scale" else r["votes"]
        r["leader"] = best > 0 and value == best
    return ordered, len(voters)
