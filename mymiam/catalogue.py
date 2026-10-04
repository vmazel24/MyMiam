"""Shared catalogue matching; no model or nutrition generation."""
from .nutrition import normalize, search_foods, STOP


def dish_family(label):
    """Recognize a whole dish, never an ingredient such as 'pain pour burger'."""
    words = normalize(label).split()
    if not words:
        return None
    head = words[0].rstrip('s')
    if head in ('burger', 'hamburger', 'cheeseburger'):
        return 'burger'
    if head in ('pizza', 'sandwich', 'wrap', 'taco', 'quiche', 'tarte',
                'lasagne', 'risotto', 'curry'):
        return head
    return None


def matches_for(store, label):
    words = normalize(label).split()
    if words and (words[0] in ('bun', 'buns') or
                  words[0] == 'pain' and set(words) & {'burger', 'burgers', 'hamburger', 'hamburgers'}):
        direct = search_foods(store, label, 12)
        if direct:
            return direct[:5]
        # A bun is the bread component, not an entire hamburger. Keep explicit
        # whole-grain/exclusion qualifiers; any shortened match is an estimate.
        qualifiers = [word for word in words if word in ('complet', 'complete', 'sans', 'gluten', 'sel')]
        query = ' '.join(['pain burger'] + qualifiers)
        return [food for food in search_foods(store, query, 12)
                if normalize(food['name']).startswith('pain ')][:5]
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
    if dish_family(label) == 'burger':
        matches = [food for food in search_foods(store, label, 12)
                   if dish_family(food['name']) == 'burger']
        if matches:
            return matches[:5]
        # Resolve a whole chicken burger even when the label lists fillings.
        # Do not discard dietary exclusions or substitute beef for another
        # protein. More specific recipes must be built from their components.
        tokens = set(words)
        if 'sans' not in tokens and not tokens & {'vegetal', 'vegetale', 'vegetarien', 'vegetarienne',
                                                 'vegan', 'veggie', 'tofu', 'falafel'}:
            if tokens & {'poulet', 'nugget', 'nuggets'} and not tokens & {'poisson', 'boeuf', 'porc'}:
                query = 'burger poulet'
            elif tokens & {'poisson'} and not tokens & {'poulet', 'nugget', 'nuggets', 'boeuf', 'porc'}:
                query = 'burger poisson'
            else:
                query = None
            if query:
                return [food for food in search_foods(store, query, 12)
                        if dish_family(food['name']) == 'burger'][:5]
        return []
    matches = search_foods(store, label, 5)
    if not matches:
        if words and words[0] in ('glace', 'glaces'):
            matches = search_foods(store, 'glace tout parfum aliment moyen', 5)
        elif words and words[0] in ('biere', 'bieres') and 'blonde' in words:
            matches = search_foods(store, 'biere coeur marche', 5)
    if not matches:
        # Drop descriptive wording, never the defining ingredients or cooking
        # method. "Salade composée de pâtes" must still contain pasta;
        # "viande hachée de boeuf" must not become a dehydrated prepared meal.
        neutral = STOP | {'composee', 'compose', 'facon', 'style', 'type',
                          'portion', 'assiette', 'part', 'standard', 'classique',
                          'entierement', 'restaurant', 'maison', 'libanais', 'libanaise'}
        words = [word for word in words if word not in neutral and not word.isdigit()]
        matches = search_foods(store, ' '.join(words), 5) if words else []
    return matches
