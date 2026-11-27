SPECTATOR_KEY = "spectator_trips"

# Pages visibles par un spectateur (lecture seule).
SPECTATOR_VIEWS = {"trip_detail", "destination_list", "date_list", "lodgings", "legs", "polls",
                   "poll_detail", "trip_summary", "meals", "meal_shopping"}
SPECTATOR_BUDGET_VIEWS = {"budget"}                                    # si l'option budget est active
SPECTATOR_ADD_VIEWS = {"destination_add", "lodging_add", "meal_add"}   # si les contributions sont ouvertes
SPECTATOR_POST_VIEWS = SPECTATOR_ADD_VIEWS | {"date_list", "comment_add"}


def spectator_views(trip):
    """Pages qu'un spectateur peut ouvrir (GET) sur ce voyage, selon les options choisies."""
    allowed = set(SPECTATOR_VIEWS)
    if trip.spectator_budget:
        allowed |= SPECTATOR_BUDGET_VIEWS
    if trip.spectator_contribute:
        allowed |= SPECTATOR_ADD_VIEWS
    return allowed


class Spectator:
    """Visiteur sans compte : sans identité ni donnée personnelle."""
    pk = -1                       # ne correspond à aucun membre
    is_spectator = True
    is_organizer = False
    is_virtual = False
    roles = ()
    display_name = "Spectateur"
    label = "Spectateur"
    can_manage = False
    is_chef = is_accountant = is_agent = False

    def can_edit_meal(self, meal):
        return False

    def can_edit_expense(self, expense):
        return False

    def can_edit_lodging(self, lodging):
        return False

    def assignable_roles(self):
        return set()
