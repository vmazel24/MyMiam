import tempfile
import unittest
from unittest.mock import patch, Mock

from mymiam.storage import Store
from mymiam.published_products import parse_table, import_published_product, fetch_product_page
from mymiam.nutrition import resolve_items
from mymiam.references import lookup_reference
from mymiam.nutrition_tools import NutritionTools
from mymiam.captures import prepare_draft


TABLE = '''<html><h1>Repas lyophilisé - pâtes à la bolognaise - 120g</h1>
<script>Protéines : 999 g</script><h4>Tableau nutritionnel</h4><p>
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
        self.assertIn('fabricant', food['source'])
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
        for html in [TABLE.replace('100 g', '100 ml'), TABLE.replace('19,0 g', '< 19,0 g'),
                     TABLE.replace('381 kcal', '< 381 kcal'),
                     TABLE.replace('381 kcal', '250 kJ'), '<p>450 kcal</p>']:
            with self.assertRaises(ValueError): parse_table(html)
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

    def test_fetch_only_supported_manufacturer_and_rejects_private_redirects(self):
        with patch('mymiam.published_products.browser_requests.Session') as session:
            for url in ['https://127.0.0.1/p/menu', 'https://example.org/p/product',
                        'https://www.decathlon.fr/account', 'http://www.decathlon.fr/p/product']:
                with self.assertRaises(ValueError): fetch_product_page(url)
            session.assert_not_called()
            response = Mock(status_code=308, headers={'location': 'https://127.0.0.1/p/private'})
            session.return_value.__enter__.return_value.get.return_value = response
            with self.assertRaises(ValueError): fetch_product_page(self.evidence['source_url'])
            response.close.assert_called_once()
