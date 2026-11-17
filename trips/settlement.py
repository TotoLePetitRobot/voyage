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
