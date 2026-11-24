from django import template

from ..models import TRIP_TABS, TRIP_TAB_LABELS, TRIP_TAB_URL_NAMES
from ..spectator import SPECTATOR_VIEWS

register = template.Library()

_GROUPS = {name: urls for name, _, _, urls in TRIP_TABS}


@register.simple_tag(takes_context=True)
def trip_tabs_for(context, trip):
    """Onglets visibles du voyage, dans l'ordre choisi. Aucune requête supplémentaire."""
    request = context.get("request")
    current = getattr(getattr(request, "resolver_match", None), "url_name", None)
    config = trip.tabs_config or {}
    order = [n for n in config.get("order", []) if n in TRIP_TAB_LABELS]
    names = order + [n for n, _, _, _ in TRIP_TABS if n not in order]
    hidden = set(config.get("hidden", []))
    names = [n for n in names if n not in hidden]
    if getattr(request, "spectator", False):
        names = [n for n in names if TRIP_TAB_URL_NAMES[n] in SPECTATOR_VIEWS]
    return [{"label": TRIP_TAB_LABELS[n], "url_name": TRIP_TAB_URL_NAMES[n],
             "current": current in _GROUPS[n]} for n in names]
