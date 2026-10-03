"""Brand-independent imports of nutrition published on verified product pages.

Read source tables/structured data, validate their units and calculate per 100 g.
No model-generated nutritional numbers, private URLs or unverified redirects.
"""
import ipaddress
import json
import re
import socket
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin

from curl_cffi import CurlOpt, requests as browser_requests

from .nutrition import NUTRIENTS, finite_number, normalize
from .references import public_source, remember


def plain(value):
    value = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'\s+', ' ', value).strip()


class ProductText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.title, self.head_title = [], [], []
        self.hidden, self.in_title, self.in_head_title = 0, False, False
        self.tables, self.table_stack = [], []
        self.row, self.cell = None, None
        self.documents, self.script = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.hidden += 1
            if tag == 'script' and attrs.get('type', '').lower() == 'application/ld+json':
                self.script = []
        if self.hidden:
            return
        if tag == 'h1':
            self.in_title = True
        if tag == 'title':
            self.in_head_title = True
        if tag == 'table':
            self.table_stack.append([])
        if tag == 'tr':
            self.row = []
        if tag in ('td', 'th'):
            self.cell = []
        if tag in ('p', 'br', 'div', 'tr', 'li', 'dt', 'dd', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            try:
                self.documents.append(json.loads(''.join(self.script)))
            except (ValueError, RecursionError):
                pass
            self.script = None
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if self.hidden:
            return
        if tag == 'h1':
            self.in_title = False
        if tag == 'title':
            self.in_head_title = False
        if tag in ('td', 'th') and self.cell is not None:
            if self.row is not None:
                self.row.append(' '.join(self.cell).strip())
            self.cell = None
            self.parts.append(' | ')
        if tag == 'tr' and self.row is not None:
            if self.table_stack:
                self.table_stack[-1].append(self.row)
            self.row = None
        if tag == 'table' and self.table_stack:
            self.tables.append(self.table_stack.pop())
        if tag in ('p', 'div', 'tr', 'li', 'dt', 'dd', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if not self.hidden:
            self.parts.append(data + ' ')
            if self.in_title:
                self.title.append(data)
            if self.in_head_title:
                self.head_title.append(data)
            if self.cell is not None:
                self.cell.append(data)


def product_nodes(documents):
    def walk(node, depth=0):
        if depth > 20:
            return
        if isinstance(node, list):
            for value in node:
                yield from walk(value, depth + 1)
        elif isinstance(node, dict):
            kind = node.get('@type', [])
            if isinstance(kind, str):
                kind = [kind]
            if set(kind) & {'Product', 'ProductGroup'}:
                yield node
            # Do not mistake a recipe, review or recommended product's nutrition
            # for the product. Only top-level/schema graph product declarations.
            if '@graph' in node:
                yield from walk(node['@graph'], depth + 1)
    for document in documents:
        yield from walk(document)


LABELS = {
    'kcal': r'(?:valeurs? energetiques?|energie|energy|calories?)',
    'protein': r'(?:proteines?|proteins?|protein content)',
    'carbs': r'(?:glucides?|carbohydrates?|carbohydrate content|total carbohydrates?)',
    'fat': r'(?:matieres grasses|lipides?|total fat|fat content|fat)',
    'fiber': r'(?:fibres?(?: alimentaires)?|dietary fib(?:er|re)|fib(?:er|re) content)',
}


def nutrient_key(label):
    label = plain(label).strip(' :|')
    for key, pattern in LABELS.items():
        if re.match(r'^' + pattern + r'\b', label):
            return key
    return None


def numeric_value(value, key, label=''):
    if re.search(r'[<>~≤≥≈]|\d\s*[-–]\s*\d', str(value)):
        return None
    value, label = plain(value), plain(label)
    if re.search(r'[<>~≤≥]|\b(?:traces?|environ|approx|less than)\b', value):
        return None
    # Quantities need published units, in either the cell or its row heading.
    unit = 'kcal' if key == 'kcal' else 'g'
    match = re.search(r'(?<![\d.,])([\d]+(?:[.,][\d]+)?)\s*' + unit + r'\b', value)
    if match is None and re.search(r'\b' + unit + r'\b', label):
        numbers = re.findall(r'\d+(?:[.,]\d+)?', value)
        if key == 'kcal' and 'kj' in label and 'kcal' in label and len(numbers) == 2:
            number = numbers[1] if label.index('kj') < label.index('kcal') else numbers[0]
        elif len(numbers) == 1:
            number = numbers[0]
        else:
            return None
    elif match:
        number = match[1]
    else:
        return None
    return finite_number(number.replace(',', '.'), 0, 100000 if key == 'kcal' else 10000, key)


def per_100g(values, basis):
    return {key: None if value is None else round(finite_number(value * 100 / basis,
            0, 1000 if key == 'kcal' else 100, key), 6) for key, value in values.items()}


def gram_basis(header):
    if re.search(r'[<>~≤≥≈]|\b(?:environ|approx)\b', str(header), re.I):
        return None
    header = plain(header)
    # A dual g|ml heading is common in supermarket templates. Requiring explicit
    # g still avoids treating a ml-only or % daily-value column as a mass basis.
    matches = re.findall(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*g\b', header)
    if len(matches) != 1:
        return None
    return finite_number(matches[0].replace(',', '.'), 1, 10000, 'Base nutritionnelle')


def accepted(nutrients):
    # Preserve partial published data, never silently turn missing/bounded values
    # into zero. An energy-only marketing claim is not a nutrition table.
    return nutrients.get('kcal') is not None and sum(nutrients.get(k) is not None for k in ('protein', 'carbs', 'fat')) >= 1


def from_rows(rows, surrounding=''):
    headers = [row for row in rows if row and nutrient_key(row[0]) is None and
               (not row[0].strip() or re.search(r'nutri|valeur|typical|pour|per|par|portion|serving|100\s*g', plain(row[0]))) and
               any(gram_basis(cell) is not None for cell in row)]
    choices = []
    for header in headers:
        for index, cell in enumerate(header):
            basis = gram_basis(cell)
            if basis is not None:
                choices.append((basis != 100, bool(re.search(r'prepare|prepared|reconstitue', plain(cell))), index, basis, cell))
    if not choices:
        mass = gram_basis(surrounding)
        if mass is None:
            return None
        choices = [(mass != 100, False, 1, mass, surrounding)]
    choices.sort(key=lambda choice: choice[:2])
    best = choices[0]
    # Multiple equally preferred columns cannot be selected confidently.
    same = [choice for choice in choices if choice[:2] == best[:2]]
    if len({(choice[2], choice[3]) for choice in same}) > 1:
        return None
    _, prepared, index, basis, _ = best
    values = {key: None for key in NUTRIENTS}
    seen = set()
    for row in rows:
        key = nutrient_key(row[0]) if row else None
        if key is None or len(row) <= index:
            continue
        value = numeric_value(row[index], key, row[0])
        if key == 'kcal' and 'kj' in plain(row[0]) and 'kcal' not in plain(row[0]) and 'kcal' not in plain(row[index]):
            continue
        if key in seen and values[key] != value:
            return None
        seen.add(key)
        values[key] = value
    if not accepted(values):
        return None
    portions = [choice[3] for choice in choices if choice[3] != 100 and choice[1] == prepared]
    return {'nutrients': per_100g(values, basis),
            'portion_grams': portions[0] if len(set(portions)) == 1 else None,
            'weight_basis': 'prepared' if prepared else 'as_sold',
            'published_table': '\n'.join(' | '.join(row) for row in rows)[:8000]}


def from_text(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    marker = re.compile(r'(?:nutrition(?:al)?|valeurs?\s+nutrit|informations?\s+nutrit)', re.I)
    sections = [index for index, line in enumerate(lines) if marker.search(line)]
    candidates = []
    for start in sections:
        section = lines[start:start + 25]
        header = next((line for line in section[:4] if gram_basis(line.split('|')[0]) is not None), None)
        if not header:
            continue
        columns = header.split('|')
        # A text block can have "100 g | 120 g" or a single stated basis.
        row_headers = ['Nutriment'] + columns
        rows = [row_headers]
        for index, line in enumerate(section):
            if nutrient_key(line):
                parts = line.split('|')
                first = parts[0]
                if len(parts) >= 2 and not re.search(r'\d', first):
                    rows.append(parts)
                    continue
                match = re.search(r'[:]|(?<!\w)\d', first)
                if not match:
                    # Definition lists and CSS rows may put the label and its
                    # value on successive lines rather than in a HTML table.
                    following = []
                    for next_line in section[index + 1:index + 4]:
                        if nutrient_key(next_line) or not re.match(r'^[<>~\s]*\d', next_line):
                            break
                        following.append(next_line)
                    if following:
                        rows.append([first, ' '.join(following)])
                    continue
                label = first[:match.start()]
                value = first[match.end():] if first[match.start()] == ':' else first[match.start():]
                rows.append([label, value] + parts[1:])
        result = from_rows(rows)
        if result:
            candidates.append(result)
    return unique_table(candidates)


def from_structured(node):
    raw = node.get('nutrition')
    if not isinstance(raw, dict):
        return None
    basis = gram_basis(raw.get('servingSize', ''))
    if basis is None:
        return None
    fields = {'kcal': 'calories', 'protein': 'proteinContent', 'carbs': 'carbohydrateContent',
              'fat': 'fatContent', 'fiber': 'fiberContent'}
    values = {key: numeric_value(raw.get(field, ''), key) for key, field in fields.items()}
    if not accepted(values):
        return None
    return {'nutrients': per_100g(values, basis),
            'portion_grams': basis if basis != 100 else None, 'weight_basis': 'as_sold',
            'published_table': json.dumps(raw, ensure_ascii=False)[:8000]}


def unique_table(candidates):
    if not candidates:
        return None
    first = candidates[0]
    if any(result['nutrients'] != first['nutrients'] or result['weight_basis'] != first['weight_basis'] for result in candidates[1:]):
        raise ValueError('Plusieurs compositions nutritionnelles différentes : fiche ambiguë')
    return first


def parse_document(html):
    page = ProductText()
    page.feed(html)
    text = ''.join(page.parts).replace('\xa0', ' ')
    nodes = list(product_nodes(page.documents))
    candidates = [result for rows in page.tables if (result := from_rows(rows, ''))]
    block = from_text(text)
    if block:
        candidates.append(block)
    candidates += [result for node in nodes if (result := from_structured(node))]
    result = unique_table(candidates)
    if result is None:
        raise ValueError('Tableau nutritionnel et base en grammes non vérifiés')
    names = [' '.join(page.title).strip()] + [str(node.get('name', '')) for node in nodes]
    result['title'] = next((name for name in names if name), ' '.join(page.head_title).strip())
    identity = names + [' '.join(page.head_title)]
    for node in nodes:
        brand = node.get('brand', '')
        identity.append(str(brand.get('name', '')) if isinstance(brand, dict) else str(brand))
    result['identity'] = ' '.join(identity)
    if result['weight_basis'] == 'as_sold' and re.search(r'lyophilis|deshydrat|freeze.dried|dehydrated', plain(result['title'])):
        result['weight_basis'] = 'dry'
    return result


def parse_table(html):
    """Compatibility for callers expecting the original four table fields."""
    result = parse_document(html)
    return result['title'], result['nutrients'], result['portion_grams'], result['published_table']


def public_addresses(url):
    host = urlsplit(public_source(url)).hostname.encode('idna').decode()
    try:
        addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except socket.gaierror as error:
        raise ValueError('Adresse de la fiche produit indisponible') from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('Source privée ou adresse réseau non publique interdite')
    # Pin a validated address in libcurl: a second DNS resolution cannot redirect
    # a public hostname to a private address between validation and fetching.
    address = next((a for a in addresses if ':' not in a), addresses[0])
    return host, '[' + address + ']' if ':' in address else address


def fetch_product_page(url, with_url=False):
    for attempt in range(3):
        host, address = public_addresses(url)
        with browser_requests.Session(impersonate='chrome', trust_env=False,
                curl_options={CurlOpt.RESOLVE: [host + ':443:' + address], CurlOpt.PROXY: ''}) as session:
            response = session.get(url, timeout=15, allow_redirects=False, stream=True)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('location')
                    if not location or attempt == 2:
                        raise ValueError('Redirection de la fiche produit invalide')
                    url = urljoin(url, location)
                    continue
                content_type = response.headers.get('content-type', '').lower()
                if response.status_code != 200 or not any(t in content_type for t in ('text/html', 'application/xhtml+xml')):
                    raise ValueError('Fiche produit HTML indisponible')
                chunks, size = [], 0
                for chunk in response.iter_content():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ValueError('Fiche produit trop volumineuse')
                    chunks.append(chunk)
                charset = re.search(r'charset=([\w-]+)', content_type)
                try:
                    html = b''.join(chunks).decode(charset[1] if charset else 'utf-8', errors='replace')
                except LookupError:
                    html = b''.join(chunks).decode('utf-8', errors='replace')
                return (html, url) if with_url else html
            finally:
                response.close()
    raise ValueError('Fiche produit indisponible')


def import_published_product(store, query, evidence):
    if evidence.get('kind') != 'product' or not evidence.get('found'):
        raise ValueError('Identité du produit non vérifiée')
    url = public_source(evidence['source_url'])
    fetched = fetch_product_page(url, with_url=True)
    html, final_url = fetched if isinstance(fetched, tuple) else (fetched, url)
    result = parse_document(html)
    def tokens(value):
        return set(re.sub(r'(\d)\s*(g|kg|ml)\b', r'\1 \2', normalize(value)).split())
    words = tokens(evidence['name']) - {'de', 'du', 'des', 'a', 'la', 'le', 'et'}
    identity = result['identity'] + ' ' + urlsplit(final_url).hostname.replace('.', ' ')
    if not words or not words.issubset(tokens(identity)):
        raise ValueError('La page ne correspond pas au produit trouvé')
    reference = {key: result[key] for key in ('portion_grams', 'weight_basis', 'published_table')}
    reference.update(kind='published_product', source_url=final_url, researched_source_url=url, composition_estimated=False,
                     note='Valeurs publiées pour le produit sec.' if result['weight_basis'] == 'dry' else
                          'Valeurs nutritionnelles publiées sur la fiche produit.')
    identity = query if evidence.get('exact_match') else evidence['canonical_query']
    return remember(store, identity, result['title'], result['nutrients'], reference,
                    flags={k: 'Non chiffré sur la fiche produit' for k in NUTRIENTS if result['nutrients'][k] is None},
                    aliases=[evidence['canonical_query']])
