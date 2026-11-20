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


def top_pairs(dest_stats, date_stats, dest_order, date_order, limit=3):
    """Classe les couples (destination, période). Score = score de la destination + score de la
    période. À égalité : moins de personnes ayant dit Non, puis plus de Oui, puis ordre d'origine."""
    pairs = []
    for di, dest in enumerate(dest_order):
        for ti, date in enumerate(date_order):
            d, t = dest_stats[dest], date_stats[date]
            pairs.append({
                "dest": dest, "date": date,
                "score": d["total"] + t["total"],
                "no": len(d["no_members"] | t["no_members"]),
                "yes": d["yes"] + t["yes"],
                "order": (di, ti),
            })
    pairs.sort(key=lambda p: (-p["score"], p["no"], -p["yes"], p["order"]))
    return pairs[:limit]
