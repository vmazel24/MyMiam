"""Shared catalogue matching; no model or nutrition generation."""
from .nutrition import normalize, search_foods


def matches_for(store, label):
    words = normalize(label).split()
    # Ciqual ready-to-eat pizzas do not carry "cuite" in their names. Retain
    # topping words, but discard generic description/portion words from Luna.
    if words and words[0] in ('pizza', 'pizzas'):
        neutral = {'garniture', 'non', 'precisee', 'precise', 'cuit', 'cuite', 'cuites',
                   'entiere', 'entieres', 'moyenne', 'moyennes', 'petite', 'petites',
                   'grande', 'grandes', 'taille', 'standard', 'type', 'classique',
                   'ordinaire', 'generique', 'format'}
        query = ' '.join(['pizza'] + [word for word in words[1:] if word not in neutral])
        matches = [food for food in search_foods(store, query, 12)
                   if normalize(food['name']).startswith('pizza ')]
        if matches:
            return matches[:5]
        # An unnamed restaurant pizza still has a usable average composition.
        # Keep the user's topping label; this candidate is an approximation.
        average = search_foods(store, 'pizza aliment moyen', 5)
        if average:
            return average
    matches = search_foods(store, label, 5)
    if not matches:
        if words and words[0] in ('glace', 'glaces'):
            matches = search_foods(store, 'glace tout parfum aliment moyen', 5)
        elif words and words[0] in ('biere', 'bieres') and 'blonde' in words:
            matches = search_foods(store, 'biere coeur marche', 5)
    if not matches:
        words = [word for word in normalize(label).split() if len(word) > 2]
        cooking = [word for word in words if word in ('cuit', 'cru') and word not in words[:2]]
        matches = search_foods(store, ' '.join(words[:2] + cooking), 5) if words else []
    return matches
