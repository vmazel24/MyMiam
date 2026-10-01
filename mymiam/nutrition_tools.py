"""Small allowlist of local, read-only nutrition tools for the Luna workflow."""
from .catalogue import matches_for
from .nutrition import finite_number, NUTRIENTS, totals


def object_schema(properties):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}


TOOLS = [{'type': 'namespace', 'name': 'nutrition',
          'description': 'Catalogue local Ciqual et produits Open Food Facts enregistrés ; calculs nutritionnels.',
          'tools': [
              {'type': 'function', 'name': 'search_foods', 'strict': True,
               'description': 'Recherche groupée de 1 à 16 aliments. Renvoie au plus 5 candidats par recherche, leurs identifiants, noms, sources et nutriments par 100 g.',
               'parameters': object_schema({'queries': {'type': 'array', 'items': {'type': 'string'}}})},
              {'type': 'function', 'name': 'calculate_portions', 'strict': True,
               'description': 'Calcule les nutriments de 1 à 40 portions avec des food_id déjà trouvés. Les nutriments absents restent null. Aucun enregistrement de repas.',
               'parameters': object_schema({'items': {'type': 'array', 'items': object_schema({
                   'food_id': {'type': 'string'}, 'grams': {'type': 'number'}})}})},
          ]}]


class NutritionTools:
    def __init__(self, store):
        self.store = store
        self.found = {}
        self.calls = []

    def execute(self, name, arguments, namespace=None):
        # No arbitrary URL, file, command, SQL or private account tool exists here.
        if namespace not in (None, 'nutrition'):
            raise ValueError('Espace d’outils indisponible')
        if name.startswith('nutrition.'):
            name = name[len('nutrition.'):]
        if name not in ('search_foods', 'calculate_portions'):
            raise ValueError('Outil indisponible')
        if not isinstance(arguments, dict):
            raise ValueError('Arguments invalides')
        self.calls.append(name)
        if name == 'search_foods':
            queries = arguments.get('queries')
            if not isinstance(queries, list) or not 1 <= len(queries) <= 16:
                raise ValueError('Fournir de 1 à 16 recherches')
            results = []
            for query in queries:
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
                    raise ValueError('Recherche invalide')
                foods = matches_for(self.store, query)
                self.found.update((food['id'], food) for food in foods)
                results.append({'query': query, 'foods': [
                    {'food_id': food['id'], 'name': food['name'], 'source': food['source'],
                     'per_100g': food['nutrients'], 'flags': food['flags']} for food in foods]})
            return {'results': results}
        inputs = arguments.get('items')
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 40:
            raise ValueError('Fournir de 1 à 40 portions')
        items = []
        for value in inputs:
            if not isinstance(value, dict) or not isinstance(value.get('food_id'), str) or value['food_id'] not in self.found:
                raise ValueError('Utiliser un aliment renvoyé par la recherche')
            grams = finite_number(value.get('grams'), 0.1, 10000, 'Portion')
            food = self.found[value['food_id']]
            items.append({'food_id': food['id'], 'grams': grams, 'nutrients': {
                key: None if food['nutrients'].get(key) is None else round(food['nutrients'][key] * grams / 100, 3)
                for key in NUTRIENTS}})
        return {'items': items, 'totals': totals(items)}

    def validate_selection(self, draft):
        if not isinstance(draft, dict):
            raise ValueError('Fiche repas invalide')
        values = list(draft.get('items', []))
        for group in draft.get('clarifications', []):
            values.extend(group.get('options', []))
        for value in values:
            food_id = value.get('food_id')
            if food_id is not None and (not isinstance(food_id, str) or food_id not in self.found):
                raise ValueError('Aliment non vérifié dans le catalogue')
