"""Personal food references: conservative identities, source evidence and recipes."""
import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .nutrition import STOP, NUTRIENTS, normalize, finite_number, food_record, nutrient_bounds, total_bounds


def identity_key(query):
    # Word order/articles can differ; restaurant, city, brand and qualifiers remain.
    normalized = normalize(query)
    normalized = re.sub(r'\b\d+(?: \d+)? (?:g|grammes|gramme|kg)\b', '', normalized)
    neutral = STOP | {'chez', 'pizzeria', 'restaurant', 'pizza', 'pizzas'}
    if set(normalized.split()) & {'pizza', 'pizzas'}:
        # Capture models sometimes append portion descriptors to an identity.
        # Apply these only to pizzas: "entier" matters for milk, "Deux Vaches"
        # can be a brand, and 0% is a product qualifier.
        normalized = re.sub(r'\b\d+ pizzas?\b', 'pizza', normalized)
        neutral |= {'entier', 'entiere', 'entiers', 'entieres', 'deux', 'trois',
                    'quatre', 'portion', 'portions', 'taille', 'petite', 'petites',
                    'moyenne', 'moyennes', 'grande', 'grandes'}
    return ' '.join(sorted(set(normalized.split()) - neutral))


def public_source(url):
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError('Source invalide')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Source HTTPS publique requise')
    host = parsed.hostname.lower()
    if host == 'localhost' or '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
        raise ValueError('Source privée interdite')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError('Source privée interdite')
    # Source provenance comes from hosted web search. Fetching additionally checks
    # DNS addresses and pins a public IP, including for every redirect.
    return url


def reference_food(db, row):
    food = food_record(row)
    ref = db.execute('SELECT data FROM food_references WHERE food_id=?', (food['id'],)).fetchone()
    if ref:
        food['reference'] = json.loads(ref[0])
    return food


def lookup_reference(store, query):
    key = identity_key(query)
    if not key:
        return None
    with store.connect() as db:
        row = db.execute('SELECT f.* FROM food_aliases a JOIN foods f ON f.id=a.food_id WHERE a.alias=?', (key,)).fetchone()
        return reference_food(db, row) if row else None


def remember(store, query, name, nutrients, reference, food_id=None, flags=None, aliases=(), register_alias=True):
    key = identity_key(query)
    if (not key and register_alias) or not isinstance(name, str) or not 1 <= len(name.strip()) <= 300:
        raise ValueError('Identité alimentaire invalide')
    food_id = food_id or 'reference:' + hashlib.sha256(key.encode()).hexdigest()[:24]
    reference = dict(reference, saved_at=datetime.now(timezone.utc).isoformat())
    source = {'recipe': 'Recette estimée · catalogue personnel',
              'estimate': 'Composition estimée · ingrédients du catalogue',
              'published_product': 'Fiche nutritionnelle du produit'}.get(reference['kind'], 'Open Food Facts · ODbL')
    with store.connect() as db:
        # Never silently change an existing reference's composition.
        db.execute('''INSERT INTO foods VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, normalized=excluded.normalized, source=excluded.source,
            nutrients=excluded.nutrients, flags=excluded.flags
            WHERE NOT EXISTS (SELECT 1 FROM food_references WHERE food_id=excluded.id)''',
                   (food_id, name, normalize(name), source, json.dumps(nutrients), json.dumps(flags or {})))
        db.execute('INSERT OR IGNORE INTO food_references VALUES (?,?)', (food_id, json.dumps(reference, ensure_ascii=False)))
        for alias in (query, *aliases) if register_alias else ():
            alias_key = identity_key(alias)
            if alias_key:
                db.execute('INSERT OR IGNORE INTO food_aliases VALUES (?,?)', (alias_key, food_id))
        return reference_food(db, db.execute('SELECT * FROM foods WHERE id=?', (food_id,)).fetchone())


def recipe_from_components(store, query, name, source_url, ingredients, components, found, note):
    if not isinstance(components, list) or not 1 <= len(components) <= 20:
        raise ValueError('Recette de 1 à 20 composants requise')
    checked = []
    for part in components:
        if not isinstance(part, dict) or part.get('food_id') not in found:
            raise ValueError('Composant non recherché dans le catalogue')
        food = found[part['food_id']]
        grams = finite_number(part.get('grams'), 0.1, 5000, 'Poids du composant')
        checked.append({'food_id': food['id'], 'name': food['name'], 'grams': grams})
    mass = finite_number(sum(part['grams'] for part in checked), 1, 10000, 'Poids du plat')
    nutrients = {key: None if any(found[part['food_id']]['nutrients'].get(key) is None for part in checked)
                 else round(sum(found[part['food_id']]['nutrients'][key] * part['grams'] for part in checked) / mass, 3)
                 for key in NUTRIENTS}
    reference = {'kind': 'recipe', 'source_url': public_source(source_url), 'ingredients': ingredients,
                 'portion_grams': mass, 'composition_estimated': True, 'components': checked, 'note': str(note)[:300],
                 'nutrient_bounds': composition_bounds(checked, found, mass)}
    return remember(store, query, name, nutrients, reference,
                    flags={key: 'Valeur absente dans un composant' for key in NUTRIENTS if nutrients[key] is None})


def composition_bounds(components, found, mass):
    portions = []
    for part in components:
        food = found[part['food_id']]
        portions.append({'nutrients': {key: None if food['nutrients'].get(key) is None
            else food['nutrients'][key] * part['grams'] / 100 for key in NUTRIENTS},
            'nutrient_bounds': nutrient_bounds(dict(food, grams=part['grams']))})
    return {key: dict(interval, lower=interval['lower'] * 100 / mass,
                      upper=interval['upper'] * 100 / mass)
            for key, interval in total_bounds(portions).items()}


def estimate_from_components(store, label, components, prepared_grams, found, note):
    """A transparent generic estimate, never an exact product/restaurant alias."""
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 150:
        raise ValueError('Libellé alimentaire requis')
    if not isinstance(note, str) or not note.strip() or len(note) > 500:
        raise ValueError('Décrire les hypothèses de recette et de cuisson')
    if not isinstance(components, list) or not 1 <= len(components) <= 20:
        raise ValueError('Recette de 1 à 20 composants requise')
    mass = finite_number(prepared_grams, 1, 10000, 'Poids final comestible')
    checked = []
    for part in components:
        if not isinstance(part, dict) or part.get('food_id') not in found:
            raise ValueError('Composant non recherché dans le catalogue')
        food = found[part['food_id']]
        # Published detection limits remain intervals through the recipe.
        limits = nutrient_bounds(dict(food, grams=100))
        if any(food['nutrients'].get(key) is None and key not in limits for key in ('kcal', 'protein', 'carbs', 'fat')):
            raise ValueError('Choisir des composants avec énergie et P/G/L chiffrés')
        checked.append({'food_id': food['id'], 'name': food['name'], 'ingredient': part.get('ingredient'),
                        'grams': finite_number(part.get('grams'), 0.1, 5000, 'Poids du composant')})
    initial_mass = finite_number(sum(part['grams'] for part in checked), 1, 10000, 'Poids des ingrédients')
    if not 0.2 <= mass / initial_mass <= 8:
        raise ValueError('Rendement de cuisson incohérent')
    nutrients = {key: None if any(found[p['food_id']]['nutrients'].get(key) is None for p in checked)
                 else round(sum(found[p['food_id']]['nutrients'][key] * p['grams'] for p in checked) / mass, 3)
                 for key in NUTRIENTS}
    if ((nutrients['kcal'] is not None and nutrients['kcal'] > 1000) or
            sum(nutrients[key] or 0 for key in ('protein', 'carbs', 'fat')) > 105):
        raise ValueError('Composition incompatible avec le poids final ; revoir le rendement de cuisson')
    reference = {'kind': 'estimate', 'composition_estimated': True, 'portion_grams': mass,
                 'components': checked, 'note': note[:300], 'prepared_grams': mass,
                 'nutrient_bounds': composition_bounds(checked, found, mass)}
    identity = json.dumps([normalize(label), checked, mass, note], sort_keys=True, ensure_ascii=False)
    food_id = 'estimate:' + hashlib.sha256(identity.encode()).hexdigest()[:24]
    return remember(store, label, label, nutrients, reference, food_id=food_id, register_alias=False,
                    flags={key: 'Valeur absente dans un composant' for key in NUTRIENTS if nutrients[key] is None})
