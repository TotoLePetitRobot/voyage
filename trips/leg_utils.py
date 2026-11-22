import re
import unicodedata


def normalize_place(text):
    """Compare des lieux sans tenir compte des majuscules, accents, tirets ni espaces multiples."""
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", text))


def leg_key(direction, transport, departure_place, departure_at, arrival_place, arrival_at):
    """Deux trajets sont identiques s'ils ont la même clé. Le nom et le statut n'y comptent pas."""
    return (direction, transport, normalize_place(departure_place), departure_at,
            normalize_place(arrival_place), arrival_at)


def sort_key(leg):
    """Tri par heure de départ ; les trajets sans heure à la fin, dans l'ordre de création."""
    if leg.departure_at is None:
        return (1, 0.0, leg.pk or 0)
    return (0, leg.departure_at.timestamp(), leg.pk or 0)
