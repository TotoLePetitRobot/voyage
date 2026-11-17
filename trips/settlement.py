from decimal import Decimal, ROUND_HALF_UP


def to_cents(value):
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def equal_split(total_cents, member_ids):
    ids = sorted(member_ids)
    base, rest = divmod(total_cents, len(ids))
    return {m: base + (1 if i < rest else 0) for i, m in enumerate(ids)}


def compute_balances(member_ids, paid, owed):
    """Solde en centimes : positif = doit recevoir, négatif = doit de l'argent."""
    return {m: paid.get(m, 0) - owed.get(m, 0) for m in member_ids}


def suggest_transfers(balances):
    """Retourne une liste de (payeur_id, bénéficiaire_id, centimes)."""
    creditors = sorted(([b, m] for m, b in balances.items() if b > 0), key=lambda x: (-x[0], x[1]))
    debtors = sorted(([-b, m] for m, b in balances.items() if b < 0), key=lambda x: (-x[0], x[1]))
    transfers, i, j = [], 0, 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][0], creditors[j][0])
        transfers.append((debtors[i][1], creditors[j][1], pay))
        debtors[i][0] -= pay
        creditors[j][0] -= pay
        if debtors[i][0] == 0:
            i += 1
        if creditors[j][0] == 0:
            j += 1
    return transfers

def to_milli(weight):
    """Poids décimal (ex. 1.2) -> entier en millièmes (1200)."""
    return int((Decimal(str(weight)) * 1000).to_integral_value())


def allocate(total_cents, weights):
    """Répartit total_cents au prorata de weights (id -> entier > 0).
    Méthode du plus fort reste : la somme est exacte, résultat déterministe."""
    ids = sorted(i for i, w in weights.items() if w > 0)
    if not ids or total_cents == 0:
        return {i: 0 for i in weights}
    w_sum = sum(weights[i] for i in ids)
    out, rems = {}, []
    for i in ids:
        q, r = divmod(total_cents * weights[i], w_sum)
        out[i] = q
        rems.append((-r, i))
    leftover = total_cents - sum(out.values())
    for _, i in sorted(rems)[:leftover]:
        out[i] += 1
    for i in weights:
        out.setdefault(i, 0)
    return out


def compute_owed(base, pools, caps, extras):
    """base   : {(catégorie, membre): centimes}  parts d'origine
    pools  : {catégorie: {membre: poids_en_millièmes}}  membres ayant accepté la règle
    caps   : {membre: plafond_centimes}  contribution maximale déclarée
    extras : {membre: centimes}  supplément maximal accepté
    Retourne (parts de base, parts après mise en commun, parts finales)."""
    base_owed, pooled, pool_totals = {}, {}, {}
    for (cat, m), cents in base.items():
        base_owed[m] = base_owed.get(m, 0) + cents
        if cat in pools and m in pools[cat]:
            pool_totals[cat] = pool_totals.get(cat, 0) + cents
        else:
            pooled[m] = pooled.get(m, 0) + cents
    for cat, total in pool_totals.items():
        for m, c in allocate(total, pools[cat]).items():
            pooled[m] = pooled.get(m, 0) + c

    final = dict(pooled)
    excess = {m: max(0, final.get(m, 0) - c) for m, c in caps.items()}
    excess = {m: e for m, e in excess.items() if e > 0 and m not in extras}
    contributors = {m: c for m, c in extras.items() if c > 0}
    surplus, offered = sum(excess.values()), sum(contributors.values())
    covered = min(surplus, offered)
    if covered > 0:
        for m, c in allocate(covered, excess).items():
            final[m] = final.get(m, 0) - c
        for m, c in allocate(covered, contributors).items():
            final[m] = final.get(m, 0) + c
    return base_owed, pooled, final
