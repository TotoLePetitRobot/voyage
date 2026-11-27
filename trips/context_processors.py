def spectator_flags(request):
    spectator = bool(getattr(request, "spectator", False))
    contribute = bool(getattr(request, "spectator_contribute", False))
    user = getattr(request, "user", None)
    return {
        # liens et formulaires d'ajout : tous les membres, et les spectateurs si c'est ouvert
        "can_add": (not spectator) or contribute,
        # pied de page de suggestions
        "show_feedback": (spectator and contribute)
                         or (not spectator and bool(user and user.is_authenticated)),
    }
