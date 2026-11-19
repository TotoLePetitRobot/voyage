import re
import unicodedata
from decimal import Decimal, InvalidOperation

MASS = {"g": Decimal(1), "kg": Decimal(1000)}
VOLUME = {"ml": Decimal(1), "cl": Decimal(10), "dl": Decimal(100), "l": Decimal(1000)}
UNIT_ALIASES = {
    "g": "g", "gr": "g", "gramme": "g", "grammes": "g",
    "kg": "kg", "kilo": "kg", "kilos": "kg", "kilogramme": "kg", "kilogrammes": "kg",
    "ml": "ml", "millilitre": "ml", "millilitres": "ml",
    "cl": "cl", "centilitre": "cl", "centilitres": "cl",
    "dl": "dl", "décilitre": "dl", "décilitres": "dl",
    "l": "l", "litre": "l", "litres": "l",
}
QTY_RE = re.compile(r"^(?P<qty>[0-9]+(?:[.,][0-9]+)?(?:\s*/\s*[0-9]+)?)\s*(?P<rest>.*)$")
LEADING_WORDS = ("de ", "d'", "d’", "du ", "des ")
MAX_QTY = Decimal("1000000")
MAX_NAME = 120


def _to_decimal(text):
    text = text.replace(",", ".")
    if "/" in text:
        num, den = (Decimal(part.strip()) for part in text.split("/", 1))
        if den == 0:
            raise ValueError("division par zéro")
        return num / den
    return Decimal(text)


def _check_name(name):
    name = name.strip()
    if not name:
        raise ValueError("nom d’article manquant")
    if len(name) > MAX_NAME:
        raise ValueError(f"nom trop long ({MAX_NAME} caractères maximum)")
    return name


def parse_line(line):
    """Une ligne de texte -> {"name", "quantity", "unit"}, ou None si la ligne est vide.
    Lève ValueError (message en français) si la ligne est invalide."""
    text = " ".join(line.split())
    if not text:
        return None
    match = QTY_RE.match(text)
    if not match:
        return {"name": _check_name(text), "quantity": None, "unit": ""}
    try:
        quantity = _to_decimal(match["qty"])
    except InvalidOperation:
        raise ValueError("quantité invalide")
    if quantity <= 0 or quantity > MAX_QTY:
        raise ValueError("quantité hors limites")
    quantity = quantity.quantize(Decimal("0.001"))
    if quantity == 0:
        raise ValueError("quantité hors limites")
    rest, unit = match["rest"], ""
    first, _, after = rest.partition(" ")
    if first.casefold() in UNIT_ALIASES:
        unit = UNIT_ALIASES[first.casefold()]
        rest = after.strip()
        for prefix in LEADING_WORDS:
            if rest.casefold().startswith(prefix):
                rest = rest[len(prefix):].strip()
                break
    return {"name": _check_name(rest), "quantity": quantity, "unit": unit}


def _number(value):
    value = value.quantize(Decimal("0.001")).normalize()
    return format(value, "f").replace(".", ",")


def format_line(name, quantity, unit):
    """Inverse de parse_line, pour réafficher un article dans le formulaire."""
    if quantity is None:
        return name
    parts = [_number(quantity)]
    if unit:
        parts.append(unit)
    parts.append(name)
    return " ".join(parts)


def fold(name):
    """Clé de comparaison : sans accents ni majuscules, pluriels simples ramenés au singulier."""
    text = name.casefold().replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = []
    for word in re.findall(r"[a-z0-9]+", text):
        if len(word) > 3 and word[-1] in "sx":
            word = word[:-1]
        words.append(word)
    return " ".join(words) or name.casefold()


def _format_total(dim, total):
    if dim == "mass":
        return f"{_number(total / 1000)} kg" if total >= 1000 else f"{_number(total)} g"
    if dim == "volume":
        return f"{_number(total / 1000)} l" if total >= 1000 else f"{_number(total)} ml"
    if dim == "count":
        return _number(total)
    return ""


def aggregate(rows):
    """rows : (libellé du repas, nom, quantité ou None, unité).
    Additionne les quantités d'un même article (g et kg ensemble, ml/cl/dl/l ensemble)."""
    groups = {}
    for meal, name, quantity, unit in rows:
        if quantity is None:
            dim, base = "none", None
        elif unit in MASS:
            dim, base = "mass", quantity * MASS[unit]
        elif unit in VOLUME:
            dim, base = "volume", quantity * VOLUME[unit]
        else:
            dim, base = "count", quantity
        key = (fold(name), dim)
        group = groups.setdefault(key, {"name": name.strip(), "dim": dim,
                                        "total": Decimal(0), "meals": []})
        if base is not None:
            group["total"] += base
        if meal not in group["meals"]:
            group["meals"].append(meal)
    result = []
    for key in sorted(groups):
        group = groups[key]
        qty_text = _format_total(group["dim"], group["total"])
        result.append({
            "name": group["name"],
            "quantity_text": qty_text,
            "line": f"{qty_text} {group['name']}" if qty_text else group["name"],
            "meals": group["meals"],
        })
    return result
