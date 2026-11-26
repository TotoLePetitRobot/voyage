def is_visible(dest_id, date_id, dest_shortlist, date_shortlist):
    """dest_shortlist / date_shortlist : ensembles d'identifiants cochés ; un ensemble vide
    signifie « pas de filtre ». Un élément qui n'est lié à aucune destination (ou à aucune
    période) n'est jamais masqué sur ce critère."""
    if dest_shortlist and dest_id is not None and dest_id not in dest_shortlist:
        return False
    if date_shortlist and date_id is not None and date_id not in date_shortlist:
        return False
    return True
