def summarize(item_ids, votes):
    """votes : (identifiant de l'élément, identifiant du membre, score 0/1/2).
    Retourne, par élément, le total, les décomptes et l'ensemble des membres ayant dit Non."""
    stats = {i: {"total": 0, "yes": 0, "maybe": 0, "no": 0, "no_members": set()}
             for i in item_ids}
    for item, member, score in votes:
        entry = stats.get(item)
        if entry is None:
            continue
        entry["total"] += score
        if score == 2:
            entry["yes"] += 1
        elif score == 1:
            entry["maybe"] += 1
        else:
            entry["no"] += 1
            entry["no_members"].add(member)
    return stats


def top_pairs(dest_votes, date_votes, dest_order, date_order, limit=3):
    """dest_votes / date_votes : listes (membre, élément, score 0/1/2).
    Score d'une paire = somme, par votant, de min(score destination, score période).
    Seuls les votants ayant voté sur les deux dimensions comptent.
    À égalité : moins de personnes ayant dit Non (score combiné 0), puis plus de Oui
    (score combiné 2), puis ordre d'origine."""
    dest_by_member = {}
    for member, item, score in dest_votes:
        if item in dest_order:
            dest_by_member.setdefault(member, {})[item] = score
    date_by_member = {}
    for member, item, score in date_votes:
        if item in date_order:
            date_by_member.setdefault(member, {})[item] = score

    pairs = {(d, t): {"score": 0, "yes": 0, "no": 0, "no_members": set()}
             for d in dest_order for t in date_order}

    for member in set(dest_by_member) & set(date_by_member):
        for dest, ds in dest_by_member[member].items():
            for date, ts in date_by_member[member].items():
                entry = pairs[(dest, date)]
                s = min(ds, ts)
                entry["score"] += s
                if s == 2:
                    entry["yes"] += 1
                elif s == 0:
                    entry["no"] += 1
                    entry["no_members"].add(member)

    result = []
    for di, dest in enumerate(dest_order):
        for ti, date in enumerate(date_order):
            entry = pairs[(dest, date)]
            result.append({
                "dest": dest, "date": date,
                "score": entry["score"],
                "no": entry["no"],
                "no_members": set(entry["no_members"]),
                "yes": entry["yes"],
                "order": (di, ti),
            })
    result.sort(key=lambda p: (-p["score"], p["no"], -p["yes"], p["order"]))
    return result[:limit]
