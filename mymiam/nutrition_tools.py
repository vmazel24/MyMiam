"""Bounded nutrition tools, with internal catalogue before public research."""
import requests

from .catalogue import matches_for
from .nutrition import finite_number, NUTRIENTS, totals, search_foods
from .references import lookup_reference, identity_key, recipe_from_components, reference_food
from .food_research import import_product


def object_schema(properties):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}


TOOLS = [{'type': 'namespace', 'name': 'nutrition',
          'description': 'Catalogue personnel prioritaire, Ciqual, recherche publique et calculs nutritionnels.',
          'tools': [
              {'type': 'function', 'name': 'search_foods', 'strict': True,
               'description': 'Recherche interne groupée. Extraire obligatoirement la marque et le restaurant/ville nommés dans le récit. Une référence nommée absente doit passer par research_food avant toute substitution générique.',
               'parameters': object_schema({'queries': {'type': 'array', 'items': object_schema({
                   'label': {'type': 'string'}, 'brand': {'type': ['string', 'null']},
                   'restaurant': {'type': ['string', 'null']}, 'city': {'type': ['string', 'null']}})}})},
              {'type': 'function', 'name': 'calculate_portions', 'strict': True,
               'description': 'Calcule les nutriments de 1 à 40 portions avec des food_id déjà trouvés. Les nutriments absents restent null. Aucun enregistrement de repas.',
               'parameters': object_schema({'items': {'type': 'array', 'items': object_schema({
                   'food_id': {'type': 'string'}, 'grams': {'type': 'number'}})}})},
              {'type': 'function', 'name': 'research_food', 'strict': True,
               'description': 'Seulement après search_foods avec la même recherche et sans référence sûre : Luna consulte le web pour vérifier marque/restaurant/ville et ingrédients. Aucun nutriment inventé. Réutiliser la référence locale si sûre.',
               'parameters': object_schema({'query': {'type': 'string'}})},
              {'type': 'function', 'name': 'save_recipe', 'strict': True,
               'description': 'Mémorise une recette dont l’identité a été vérifiée par research_food, avec des composants déjà trouvés dans Ciqual. Poids et composition nutritionnelle toujours estimés. Le serveur calcule les valeurs, aucun repas enregistré.',
               'parameters': object_schema({'query': {'type': 'string'}, 'components': {'type': 'array', 'items': object_schema({
                   'food_id': {'type': 'string'}, 'grams': {'type': 'number'}})}, 'note': {'type': 'string'}})},
          ]}]


class NutritionTools:
    def __init__(self, store, researcher=None):
        self.store = store
        self.found = {}
        self.calls = []
        self.researcher = researcher
        self.searched = {}
        self.researched = {}
        self.external_count = 0
        self.required_research = {}
        self.fallbacks = {}

    @staticmethod
    def public_food(food):
        result = {'food_id': food['id'], 'name': food['name'], 'source': food['source'],
                  'per_100g': food['nutrients'], 'flags': food['flags']}
        if food.get('reference'):
            result['reference'] = food['reference']
        return result

    def execute(self, name, arguments, namespace=None):
        # No arbitrary URL fetch, file, shell, SQL or private account tool exists here.
        if namespace not in (None, 'nutrition'):
            raise ValueError('Espace d’outils indisponible')
        if name.startswith('nutrition.'):
            name = name[len('nutrition.'):]
        if name not in ('search_foods', 'calculate_portions', 'research_food', 'save_recipe'):
            raise ValueError('Outil indisponible')
        if not isinstance(arguments, dict):
            raise ValueError('Arguments invalides')
        self.calls.append(name)
        if name == 'search_foods':
            queries = arguments.get('queries')
            if not isinstance(queries, list) or not 1 <= len(queries) <= 16:
                raise ValueError('Fournir de 1 à 16 recherches')
            results = []
            for value in queries:
                named = False
                if isinstance(value, dict):
                    label = value.get('label')
                    if not isinstance(label, str) or not label.strip():
                        raise ValueError('Libellé alimentaire requis')
                    qualifiers = [value.get(field) for field in ('brand', 'restaurant', 'city')]
                    if any(field is not None and (not isinstance(field, str) or len(field) > 80) for field in qualifiers):
                        raise ValueError('Identité alimentaire invalide')
                    query = ' '.join([label] + [field for field in qualifiers if field])
                    named = bool(value.get('brand') or value.get('restaurant'))
                else:
                    query = value  # Compatibility with earlier in-process drafts.
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
                    raise ValueError('Recherche invalide')
                reference = lookup_reference(self.store, query)
                direct = [] if reference else search_foods(self.store, query, 5)
                foods = [reference] if reference else direct or matches_for(self.store, query)
                if not reference:
                    with self.store.connect() as db:
                        foods = [reference_food(db, db.execute('SELECT * FROM foods WHERE id=?', (food['id'],)).fetchone())
                                 for food in foods]
                key = identity_key(query)
                # Exact product-name searches may find an earlier barcode import
                # which predates the reference metadata/alias tables.
                certain = bool(reference) or bool(named and len(direct) == 1 and direct[0]['id'].startswith('off:'))
                if named and not certain:
                    self.required_research[key] = query
                    self.fallbacks[key] = foods
                    foods = []  # Expose generic fallbacks only after verification.
                self.found.update((food['id'], food) for food in foods)
                # Partial/generic fallback candidates must never imply exact identity.
                self.searched[key] = certain
                results.append({'query': query, 'reference_match': certain,
                                'external_required': named and not certain,
                                'relaxed_search': not reference and not direct,
                                'foods': [self.public_food(food) for food in foods]})
            return {'results': results}
        if name == 'research_food':
            query = arguments.get('query')
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 150:
                raise ValueError('Recherche invalide')
            key = identity_key(query)
            if key not in self.searched:
                raise ValueError('Rechercher cette référence dans le catalogue interne d’abord')
            if self.searched[key]:
                raise ValueError('Référence déjà connue : réutiliser le catalogue interne')
            if key in self.researched:
                return self.researched[key]
            if self.researcher is None or self.external_count >= 2:
                result = {'found': False, 'exact_match': False,
                          'note': 'Recherche externe indisponible ou limite atteinte ; retenir une approximation signalée.'}
            else:
                self.external_count += 1
                result = self.researcher(query)
            self.researched[key] = result
            fallback = self.fallbacks.get(key, [])
            self.found.update((food['id'], food) for food in fallback)
            result['fallback_foods'] = [self.public_food(food) for food in fallback]
            if result.get('found') and result.get('exact_match') and result.get('barcode'):
                try:
                    food = import_product(self.store, result['barcode'], query)
                    self.found[food['id']] = food
                    result['food'] = self.public_food(food)
                except (ValueError, requests.RequestException):
                    result['nutrition_note'] = 'Fiche produit non importable ; nutriments exacts non vérifiés.'
            return result
        if name == 'save_recipe':
            query = arguments.get('query')
            if not isinstance(query, str):
                raise ValueError('Identité invalide')
            evidence = self.researched.get(identity_key(query), {})
            if not evidence.get('found') or not evidence.get('exact_match') or evidence.get('kind') != 'restaurant':
                raise ValueError('Identité exacte et carte de restaurant vérifiées requises pour mémoriser')
            food = recipe_from_components(self.store, query, evidence['canonical_query'], evidence['source_url'],
                                          evidence['ingredients'], arguments.get('components'), self.found,
                                          arguments.get('note', ''))
            self.found[food['id']] = food
            return {'food': self.public_food(food)}
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

    def pending_research(self):
        return [query for key, query in self.required_research.items() if key not in self.researched]
