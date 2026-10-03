import tempfile
import unittest
from unittest.mock import patch, Mock

from mymiam.storage import Store
from mymiam.published_products import parse_table, import_published_product, fetch_product_page, parse_document
from mymiam.nutrition import resolve_items
from mymiam.references import lookup_reference
from mymiam.nutrition_tools import NutritionTools
from mymiam.captures import prepare_draft


TABLE = '''<html><h1>Repas lyophilisé - pâtes à la bolognaise - 120g</h1>
<script>Protéines : 999 g</script><script type="application/ld+json">{"@type":"Product","brand":{"name":"FORCLAZ"}}</script><h4>Tableau nutritionnel</h4><p>
Valeurs nutritionnelles : 100 g | 120 g (=1 plat)
Valeurs énergétiques : 1607 kJ - 381 kcal | 1928 kJ - 457 kcal
Matières grasses : 8,0 g | 10,0 g
dont acides gras saturés : 4,3 g | 5,2 g
Glucides : 56,0 g | 67,0 g
dont sucres : 12,0 g | 14,0 g
Protéines : 19,0 g | 23,0 g
Sel : 2,9 g | 3,5 g</p><h4>Fabricant</h4></html>'''


class PublishedProductTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.query = 'pâtes à la bolognaise lyophilisées Decathlon'
        self.evidence = {'found': True, 'exact_match': True, 'kind': 'product',
                         'name': 'Repas lyophilisé - pâtes à la bolognaise - 120g',
                         'canonical_query': self.query, 'barcode': None,
                         'source_url': 'https://www.decathlon.fr/p/pates/_/R-p-305685'}

    def test_table_import_uses_dry_product_values_caches_reference_without_unknown_zeroes(self):
        with patch('mymiam.published_products.fetch_product_page', return_value=TABLE) as fetch:
            tools = NutritionTools(self.store, Mock(return_value=self.evidence))
            tools.execute('search_foods', {'queries': [{'label': 'pâtes à la bolognaise lyophilisées',
                'brand': 'Decathlon', 'restaurant': None, 'city': None}]})
            result = tools.execute('research_food', {'query': self.query})
        food = result['food']
        self.assertEqual(food['per_100g']['kcal'], 381)
        self.assertIsNone(food['per_100g']['fiber'])
        self.assertEqual(food['reference']['weight_basis'], 'dry')
        self.assertEqual(food['reference']['portion_grams'], 120)
        self.assertIn('Fiche nutritionnelle', food['source'])
        item = resolve_items(self.store, [{'food_id': food['food_id'], 'grams': 120}])[0]
        self.assertAlmostEqual(item['nutrients']['kcal'], 457.2)
        self.assertFalse(item['composition_estimated'])
        self.assertEqual(item['source_url'], self.evidence['source_url'])
        prepared = prepare_draft(self.store, {'slot': 'lunch', 'items': [{'label': self.query,
            'food_id': food['food_id'], 'grams': 120, 'estimated': True, 'note': 'Sachet pesé'}]})
        self.assertEqual(prepared['items'][0]['matches'][0]['reference']['weight_basis'], 'dry')
        self.assertNotIn('pesé', prepared['items'][0]['note'])
        cached = lookup_reference(Store(self.directory.name), self.query)
        self.assertEqual(cached['id'], food['food_id'])
        fetch.assert_called_once()

    def test_ambiguous_tables_bounds_wrong_product_are_rejected(self):
        for html in [TABLE.replace('100 g', '100 ml'),
                     TABLE.replace('381 kcal', '< 381 kcal'),
                     TABLE.replace('381 kcal', '250 kJ'), '<p>450 kcal</p>']:
            with self.assertRaises(ValueError): parse_table(html)
        self.assertIsNone(parse_table(TABLE.replace('19,0 g', '< 19,0 g'))[1]['protein'])
        with patch('mymiam.published_products.fetch_product_page', return_value=TABLE):
            with self.assertRaises(ValueError):
                import_published_product(self.store, self.query, {**self.evidence, 'name': 'Purée pommes de terre'})

    def test_product_without_requested_format_is_importable_candidate_without_false_alias(self):
        evidence = {**self.evidence, 'exact_match': False,
                    'name': 'FORCLAZ Repas lyophilisé - pâtes à la bolognaise - 120 g',
                    'canonical_query': 'Pâtes à la bolognaise lyophilisées FORCLAZ 120 g'}
        tools = NutritionTools(self.store, Mock(return_value=evidence))
        tools.execute('search_foods', {'queries': [self.query]})
        with patch('mymiam.published_products.fetch_product_page', return_value=TABLE):
            result = tools.execute('research_food', {'query': self.query})
        self.assertIn('candidate_food', result)
        self.assertNotIn('food', result)
        self.assertEqual(result['candidate_food']['per_100g']['kcal'], 381)
        self.assertIsNone(lookup_reference(self.store, self.query))
        self.assertIsNotNone(lookup_reference(self.store, evidence['canonical_query']))

    def test_public_fetch_has_no_brand_allowlist_and_pins_validated_dns(self):
        from curl_cffi import CurlOpt
        response = Mock(status_code=200, headers={'content-type': 'text/html; charset=utf-8'})
        response.iter_content.return_value = [TABLE.encode()]
        with patch('mymiam.published_products.socket.getaddrinfo', return_value=[(None, None, None, None, ('93.184.216.34', 443))]), \
             patch('mymiam.published_products.browser_requests.Session') as session:
            session.return_value.__enter__.return_value.get.return_value = response
            self.assertEqual(fetch_product_page('https://brand.example/catalogue/food'), TABLE)
        self.assertEqual(session.call_args.kwargs['curl_options'][CurlOpt.RESOLVE], ['brand.example:443:93.184.216.34'])
        self.assertFalse(session.call_args.kwargs['trust_env'])
        response.close.assert_called_once()

    def test_private_urls_dns_and_redirects_are_rejected(self):
        with patch('mymiam.published_products.browser_requests.Session') as session:
            for url in ['https://127.0.0.1/p/menu', 'https://user:pass@brand.example/p',
                        'http://brand.example/product', 'https://192.168.1.1/']:
                with self.assertRaises(ValueError): fetch_product_page(url)
            session.assert_not_called()
            with patch('mymiam.published_products.socket.getaddrinfo', return_value=[(None, None, None, None, ('192.168.1.1', 443))]):
                with self.assertRaises(ValueError): fetch_product_page('https://brand.example/product')
            session.assert_not_called()
            response = Mock(status_code=308, headers={'location': 'https://127.0.0.1/private'})
            session.return_value.__enter__.return_value.get.return_value = response
            with patch('mymiam.published_products.socket.getaddrinfo', return_value=[(None, None, None, None, ('93.184.216.34', 443))]):
                with self.assertRaises(ValueError): fetch_product_page(self.evidence['source_url'])
            response.close.assert_called_once()

    def test_public_redirect_rechecks_dns_and_retains_final_source(self):
        first = Mock(status_code=308, headers={'location': 'https://cdn.other-brand.example/product'})
        final = Mock(status_code=200, headers={'content-type': 'text/html'})
        final.iter_content.return_value = [TABLE.encode()]
        with patch('mymiam.published_products.socket.getaddrinfo', return_value=[(None, None, None, None, ('93.184.216.34', 443))]) as dns, \
             patch('mymiam.published_products.browser_requests.Session') as session:
            session.return_value.__enter__.return_value.get.side_effect = [first, final]
            html, url = fetch_product_page(self.evidence['source_url'], with_url=True)
        self.assertEqual(html, TABLE)
        self.assertEqual(url, 'https://cdn.other-brand.example/product')
        self.assertEqual([call.args[0] for call in dns.call_args_list], ['www.decathlon.fr', 'cdn.other-brand.example'])
        self.assertEqual(session.call_count, 2)
        first.close.assert_called_once()
        final.close.assert_called_once()

    def test_tables_in_any_brand_page_select_100g_even_after_portion_column(self):
        html = '<h1>Chocolate bar Example</h1><table><tr><th>Nutrient</th><th>Per serving (30 g)</th><th>Per 100 g</th></tr>'
        for label, serving, hundred in [('Energy', '57 kcal', '190 kcal'), ('Protein', '3 g', '10 g'),
                                         ('Carbohydrates', '4 g', '13.3 g'), ('Total fat', '1 g', '3.3 g')]:
            html += f'<tr><td>{label}</td><td>{serving}</td><td>{hundred}</td></tr>'
        result = parse_document(html + '</table>')
        self.assertEqual(result['nutrients']['kcal'], 190)
        self.assertEqual(result['nutrients']['carbs'], 13.3)
        self.assertEqual(result['portion_grams'], 30)

    def test_structured_nutrition_per_portion_is_scaled_from_published_mass(self):
        import json
        data = {'@type': 'Product', 'name': 'Barre nature', 'brand': {'name': 'Une autre marque'},
                'nutrition': {'@type': 'NutritionInformation', 'servingSize': '1 bar (40 g)',
                    'calories': '160 kcal', 'proteinContent': '8 g', 'carbohydrateContent': '16 g', 'fatContent': '4 g'}}
        html = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        result = parse_document(html)
        self.assertEqual(result['title'], 'Barre nature')
        self.assertEqual(result['portion_grams'], 40)
        self.assertEqual(result['nutrients']['kcal'], 400)
        self.assertEqual(result['nutrients']['protein'], 20)
        self.assertIsNone(result['nutrients']['fiber'])
        self.assertIn('Une autre marque', result['identity'])
        data['nutrition']['servingSize'] = '1 bar'
        with self.assertRaises(ValueError):
            parse_document('<script type="application/ld+json">' + json.dumps(data) + '</script>')

    def test_text_definition_list_and_bounded_values_keep_unknowns(self):
        html = '<h1>Yaourt nature</h1><h2>Valeurs nutritionnelles pour 100 g</h2><dl>'
        for label, value in [('Énergie', '46 kcal'), ('Protéines', '8 g'), ('Glucides', '3,4 g'), ('Matières grasses', '< 0,5 g')]:
            html += f'<dt>{label}</dt><dd>{value}</dd>'
        result = parse_document(html + '</dl>')
        self.assertEqual(result['nutrients']['kcal'], 46)
        self.assertEqual(result['nutrients']['protein'], 8)
        self.assertIsNone(result['nutrients']['fat'])

    def test_ambiguous_products_prepared_bases_and_percentages_are_not_misread(self):
        html = '<h1>Produit</h1><table><tr><th>Nutriment</th><th>Pour 100 g préparés</th></tr>'
        for label, value in [('Énergie', '90 kcal'), ('Protéines', '4 g'), ('Glucides', '10 g'), ('Matières grasses', '2 g')]:
            html += f'<tr><td>{label}</td><td>{value}</td></tr>'
        html += '</table>'
        self.assertEqual(parse_document(html)['weight_basis'], 'prepared')
        with self.assertRaises(ValueError): parse_document(html + html.replace('90 kcal', '500 kcal'))
        from mymiam.published_products import numeric_value
        self.assertIsNone(numeric_value('5%', 'fat', 'Matières grasses'))
        for value in ['≤ 0,5 g', '≥ 5 g', '≈ 1 g', '3–5 g']:
            self.assertIsNone(numeric_value(value, 'fat'))
        with self.assertRaises(ValueError): parse_document(html.replace('100 g', '100 ml'))
