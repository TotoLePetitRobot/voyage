SPECTATOR_KEY = "spectator_trips"

# Pages visibles par un spectateur (lecture seule). Tout le reste lui répond 403 : pas de budget,
# de dépenses ni de votes personnels, pas de solidarité, pas de recherche (elle affiche les villes
# de départ et de retour), aucune page de modification.
SPECTATOR_VIEWS = {"trip_detail", "destination_list", "date_list", "lodgings", "legs",
                   "polls", "poll_detail", "trip_summary", "meals", "meal_shopping"}


class Spectator:
    """Visiteur sans compte : lecture seule, sans identité, sans aucune donnée personnelle."""
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
