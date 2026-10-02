"""Import published product tables; models never supply nutritional numbers.

Manufacturer adapters are deliberately bounded to supported public sites. Other
products continue through Open Food Facts or an explicitly generic estimate.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin

from curl_cffi import requests as browser_requests

from .nutrition import NUTRIENTS, finite_number, normalize
from .references import public_source, remember


class ProductText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.title, self.hidden, self.in_title = [], [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag == 'h1':
            self.in_title = True
        if tag in ('p', 'br', 'div', 'tr', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'h1':
            self.in_title = False
        if tag in ('p', 'div', 'tr', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
            if self.in_title:
                self.title.append(data)


def parse_table(html):
    page = ProductText()
    page.feed(html)
    text = ''.join(page.parts).replace('\xa0', ' ')
    # Use the product's table section, never an unrelated marketing number.
    section = re.search(r'Tableau nutritionnel\s*\n(.*?)(?:\n\s*\n|\Z)', text, re.I | re.S)
    if not section:
        raise ValueError('Tableau nutritionnel du fabricant introuvable')
    table = section[1]
    header = re.search(r'Valeurs nutritionnelles\s*:\s*100\s*g\s*(?:\|\s*(\d+(?:[.,]\d+)?)\s*g)?', table, re.I)
    if not header:
        raise ValueError('Base nutritionnelle par 100 g non vérifiée')
    patterns = {'kcal': r'Valeurs énergétiques\s*:\s*(?:[\d.,]+\s*kJ\s*[-–]\s*)?([\d.,]+)\s*kcal',
                'protein': r'Protéines\s*:\s*([\d.,]+)\s*g',
                'carbs': r'Glucides\s*:\s*([\d.,]+)\s*g',
                'fat': r'Matières grasses\s*:\s*([\d.,]+)\s*g',
                'fiber': r'Fibres(?: alimentaires)?\s*:\s*([\d.,]+)\s*g'}
    nutrients = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, table, re.I)
        nutrients[key] = finite_number(match[1].replace(',', '.'), 0, 1000 if key == 'kcal' else 100, key) if match else None
    if any(nutrients[key] is None for key in ('kcal', 'protein', 'carbs', 'fat')):
        raise ValueError('Tableau nutritionnel incomplet ou borné')
    portion = finite_number(header[1].replace(',', '.'), 1, 10000, 'Portion fabricant') if header[1] else None
    return ''.join(page.title).strip(), nutrients, portion, table.strip()


def fetch_product_page(url):
    parsed = urlsplit(public_source(url))
    # A fixed manufacturer allowlist prevents arbitrary URL/private network fetches.
    if parsed.hostname != 'www.decathlon.fr' or not parsed.path.startswith('/p/'):
        raise ValueError('Ce fabricant ne dispose pas encore d’un import de tableau')
    with browser_requests.Session(impersonate='chrome') as session:
        for attempt in range(3):
            parsed = urlsplit(public_source(url))
            if parsed.hostname != 'www.decathlon.fr' or not parsed.path.startswith('/p/'):
                raise ValueError('Redirection fabricant non autorisée')
            response = session.get(url, timeout=15, allow_redirects=False, stream=True)
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get('location')
            response.close()
            if not location or attempt == 2:
                raise ValueError('Redirection fabricant invalide')
            url = urljoin(url, location)
        try:
            if response.status_code != 200 or 'text/html' not in response.headers.get('content-type', ''):
                raise ValueError('Fiche fabricant indisponible')
            chunks, size = [], 0
            for chunk in response.iter_content():
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError('Fiche fabricant trop volumineuse')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8')
        finally:
            response.close()


def import_published_product(store, query, evidence):
    if evidence.get('kind') != 'product' or not evidence.get('found'):
        raise ValueError('Identité du produit non vérifiée')
    url = public_source(evidence['source_url'])
    title, nutrients, portion, table = parse_table(fetch_product_page(url))
    # The retrieved page must agree with the identity returned by public research.
    def tokens(value):
        return set(re.sub(r'(\d)g\b', r'\1 g', normalize(value)).split())
    words = tokens(evidence['name']) - {'de', 'du', 'des', 'a', 'la', 'le', 'et', 'decathlon', 'forclaz'}
    if not words or not words.issubset(tokens(title)):
        raise ValueError('La page fabricant ne correspond pas au produit trouvé')
    dry = 'lyophilis' in normalize(title) or 'deshydrat' in normalize(title)
    reference = {'kind': 'published_product', 'source_url': url, 'composition_estimated': False,
                 'portion_grams': portion, 'weight_basis': 'dry' if dry else 'as_sold',
                 'published_table': table, 'note': 'Valeurs du fabricant pour le produit sec.' if dry else 'Valeurs du fabricant par 100 g.'}
    # An equivalent product is a candidate, never an exact alias of the request.
    identity = query if evidence.get('exact_match') else evidence['canonical_query']
    return remember(store, identity, title, nutrients, reference,
                    flags={k: 'Non publié par le fabricant' for k in NUTRIENTS if nutrients[k] is None},
                    aliases=[evidence['canonical_query']])
