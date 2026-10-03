"""Personal food references: conservative identities, source evidence and recipes."""
import hashlib
import ipaddress
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .nutrition import STOP, NUTRIENTS, normalize, finite_number, food_record


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


def remember(store, query, name, nutrients, reference, food_id=None, flags=None, aliases=()):
    key = identity_key(query)
    if not key or not isinstance(name, str) or not 1 <= len(name.strip()) <= 300:
        raise ValueError('Identité alimentaire invalide')
    food_id = food_id or 'reference:' + hashlib.sha256(key.encode()).hexdigest()[:24]
    reference = dict(reference, saved_at=datetime.now(timezone.utc).isoformat())
    source = {'recipe': 'Recette estimée · catalogue personnel',
              'published_product': 'Fiche nutritionnelle du produit'}.get(reference['kind'], 'Open Food Facts · ODbL')
    with store.connect() as db:
        # Never silently change an existing reference's composition.
        db.execute('''INSERT INTO foods VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, normalized=excluded.normalized, source=excluded.source,
            nutrients=excluded.nutrients, flags=excluded.flags
            WHERE NOT EXISTS (SELECT 1 FROM food_references WHERE food_id=excluded.id)''',
                   (food_id, name, normalize(name), source, json.dumps(nutrients), json.dumps(flags or {})))
        db.execute('INSERT OR IGNORE INTO food_references VALUES (?,?)', (food_id, json.dumps(reference, ensure_ascii=False)))
        for alias in (query, *aliases):
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
                 'portion_grams': mass, 'composition_estimated': True, 'components': checked, 'note': str(note)[:300]}
    return remember(store, query, name, nutrients, reference,
                    flags={key: 'Valeur absente dans un composant' for key in NUTRIENTS if nutrients[key] is None})
