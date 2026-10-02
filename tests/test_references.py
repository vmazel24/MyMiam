import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from mymiam.storage import Store
from mymiam.nutrition import normalize, resolve_items, search_foods
from mymiam.nutrition_tools import NutritionTools
from mymiam.references import public_source, lookup_reference, identity_key
from mymiam.food_research import research, import_product


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name)
        self.query = 'pizza Prosciutto e Funghi Tripletta Bordeaux'
        self.evidence = {'found': True, 'exact_match': True, 'kind': 'restaurant',
                         'name': 'Prosciutto e Funghi', 'canonical_query': self.query,
                         'source_url': 'https://example.org/tripletta-bordeaux/menu',
                         'ingredients': ['tomate', 'mozzarella', 'jambon', 'champignons'], 'barcode': None, 'note': ''}
        self.researcher = Mock(return_value=self.evidence)
        self.tools = NutritionTools(self.store, self.researcher)
        with self.store.connect() as db:
            for food_id, name, nutrients in [
                ('base', 'Pâte à pizza cuite', {'kcal': 250, 'protein': 8, 'carbs': 45, 'fat': 4, 'fiber': None}),
                ('cheese', 'Mozzarella', {'kcal': 200, 'protein': 20, 'carbs': 2, 'fat': 15, 'fiber': 0}),
            ]:
                db.execute('INSERT INTO foods VALUES (?,?,?,?,?,?)',
                    (food_id, name, normalize(name), 'Ciqual', json.dumps(nutrients), '{}'))

    def save(self):
        self.tools.execute('search_foods', {'queries': [self.query]})
        self.tools.execute('research_food', {'query': self.query})
        self.tools.execute('search_foods', {'queries': ['pâte pizza cuite', 'mozzarella']})
        return self.tools.execute('save_recipe', {'query': self.query,
             'components': [{'food_id': 'base', 'grams': 300}, {'food_id': 'cheese', 'grams': 100}],
             'note': 'Poids estimés, carte sans grammage'})['food']

    def test_internal_lookup_required_and_known_reference_blocks_external_research(self):
        with self.assertRaises(ValueError): self.tools.execute('research_food', {'query': self.query})
        self.researcher.assert_not_called()
        food = self.save()
        tools = NutritionTools(self.store, self.researcher)
        result = tools.execute('search_foods', {'queries': ['Prosciutto e Funghi chez Tripletta à Bordeaux']})['results'][0]
        self.assertTrue(result['reference_match'])
        self.assertEqual(result['foods'][0]['food_id'], food['food_id'])
        self.assertEqual(result['foods'][0]['reference']['portion_grams'], 400)
        with self.assertRaises(ValueError): tools.execute('research_food', {'query': self.query})
        self.assertEqual(self.researcher.call_count, 1)
        self.assertIsNotNone(lookup_reference(self.store, 'Prosciutto e Funghi Tripletta Bordeaux pizza entière restaurant Bordeaux deux pizzas'))
        self.assertIsNotNone(lookup_reference(self.store, '2 pizzas Prosciutto e Funghi Tripletta Bordeaux'))
        self.assertNotEqual(identity_key('Lait entier Auchan'), identity_key('Lait écrémé Auchan'))
        self.assertNotEqual(identity_key('Fromage blanc Les Deux Vaches'), identity_key('Fromage blanc Vaches'))

    def test_named_identity_requires_verification_before_exposing_generic_fallback(self):
        result = self.tools.execute('search_foods', {'queries': [
            {'label': 'pizza Prosciutto e Funghi', 'brand': None, 'restaurant': 'Tripletta', 'city': 'Bordeaux'}]})['results'][0]
        self.assertTrue(result['external_required'])
        self.assertEqual(result['foods'], [])
        self.assertEqual(self.tools.pending_research(), [result['query']])
        self.tools.execute('research_food', {'query': result['query']})
        self.assertEqual(self.tools.pending_research(), [])

    def test_external_limit_still_returns_internal_approximations(self):
        self.tools.external_count = 2
        result = self.tools.execute('search_foods', {'queries': [
            {'label': 'pâte pizza', 'brand': 'Auchan', 'restaurant': None, 'city': None}]})['results'][0]
        reference = self.tools.execute('research_food', {'query': result['query']})
        self.assertFalse(reference['found'])
        self.assertTrue(reference['fallback_foods'])
        self.assertEqual(self.tools.pending_research(), [])
        self.researcher.assert_not_called()

    def test_recipe_computation_scaling_provenance_and_missing_nutrients(self):
        food = self.save()
        self.assertEqual(food['per_100g']['kcal'], 237.5)
        self.assertIsNone(food['per_100g']['fiber'])
        items = resolve_items(self.store, [{'food_id': food['food_id'], 'grams': 800, 'estimated': True}])
        self.assertEqual(items[0]['nutrients']['kcal'], 1900)
        self.assertTrue(items[0]['composition_estimated'])
        self.assertEqual(items[0]['source_url'], self.evidence['source_url'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM meals').fetchone()[0], 0)

    def test_brand_city_and_recipe_names_do_not_collide(self):
        self.save()
        for query in ['Prosciutto e Funghi Tripletta Paris', 'Regina Tripletta Bordeaux',
                      'Prosciutto e Funghi autre-pizzeria Bordeaux', 'Prosciutto e Funghi Tripletta']:
            self.assertIsNone(lookup_reference(self.store, query))

    def test_generic_fallback_and_nonexact_web_result_never_create_exact_alias(self):
        query = 'pizza Regina Tripletta Bordeaux'
        result = self.tools.execute('search_foods', {'queries': [query]})['results'][0]
        self.assertFalse(result['reference_match'])
        self.assertTrue(result['relaxed_search'])
        self.researcher.return_value = dict(self.evidence, exact_match=False)
        self.tools.execute('research_food', {'query': query})
        with self.assertRaises(ValueError): self.tools.execute('save_recipe', {'query': query, 'components': [], 'note': ''})
        self.assertIsNone(lookup_reference(self.store, query))

    def test_only_found_component_ids_accepted_and_queries_deduplicated_bounded(self):
        self.tools.execute('search_foods', {'queries': [self.query]})
        self.tools.execute('research_food', {'query': self.query})
        self.tools.execute('research_food', {'query': self.query})
        self.assertEqual(self.researcher.call_count, 1)
        with self.assertRaises(ValueError):
            self.tools.execute('save_recipe', {'query': self.query, 'components': [{'food_id': 'base', 'grams': 400}], 'note': ''})
        for query in ['pizza autre restaurant Paris', 'pizza autre restaurant Lyon']:
            self.tools.execute('search_foods', {'queries': [query]})
            self.tools.execute('research_food', {'query': query})
        self.assertEqual(self.researcher.call_count, 2)

    def test_web_source_must_be_returned_by_real_search(self):
        output = [{'type': 'web_search_call', 'action': {'sources': [{'url': self.evidence['source_url']}]}}]
        responder = Mock(return_value=({'usage': {'input_tokens': 20}}, output, json.dumps(self.evidence)))
        usage = {}
        result = research(responder, 'test-token', 'gpt-5.6-luna', self.query, usage)
        self.assertEqual(result['source_url'], self.evidence['source_url'])
        self.assertEqual(usage['web_search_calls'], 1)
        responder.return_value = ({}, output, json.dumps(dict(self.evidence, source_url='https://invented.example/menu')))
        with self.assertRaises(ValueError): research(responder, 'test-token', 'gpt-5.6-luna', self.query, {})
        responder.return_value = ({}, [], json.dumps(self.evidence))
        with self.assertRaises(ValueError): research(responder, 'test-token', 'gpt-5.6-luna', self.query, {})

    def test_source_url_parameters_use_actual_search_link_and_alias_keeps_portion_words_out(self):
        url = 'https://example.org/Tripletta-Bordeaux/Menu?time=ASAP'
        output = [{'type': 'web_search_call', 'action': {'sources': [{'url': url}]}}]
        responder = Mock(return_value=({}, output, json.dumps(self.evidence)))
        result = research(responder, 'test-token', 'gpt-5.6-luna', self.query, {})
        self.assertEqual(result['source_url'], url)

    def test_product_import_values_come_from_off_bounds_stay_unknown(self):
        response = Mock(status_code=200)
        response.json.return_value = {'product': {'product_name': 'Fromage blanc 0%', 'brands': 'Auchan',
             'nutriments': {'energy-kcal_100g': 46, 'proteins_100g': 7.5, 'carbohydrates_100g': 4,
                            'fat_100g': 0, 'fat_modifier': '<'}}}
        with patch('mymiam.food_research.requests.get', return_value=response):
            food = import_product(self.store, '3596710402380', 'Fromage blanc 0 Auchan')
            with self.assertRaises(ValueError): import_product(self.store, '3596710402380', 'Fromage blanc 0 Danone')
        self.assertEqual(food['nutrients']['kcal'], 46)
        self.assertIsNone(food['nutrients']['fat'])
        self.assertIsNone(food['nutrients']['fiber'])
        self.assertFalse(food['reference']['composition_estimated'])
        self.assertIsNotNone(lookup_reference(self.store, 'Auchan fromage blanc 0'))

    def test_off_concatenated_label_is_still_matched_and_cached_by_brand(self):
        response = Mock(status_code=200)
        response.json.return_value = {'product': {'product_name': 'Fromage blancDoux et Onctueux0%',
            'brands': 'Auchan', 'nutriments': {'energy-kcal_100g': 46}}}
        with patch('mymiam.food_research.requests.get', return_value=response):
            food = import_product(self.store, '3596710402380', 'fromage blanc 0 Auchan')
        self.assertIn('blanc Doux', food['name'])
        self.assertIsNotNone(lookup_reference(self.store, 'Auchan fromage blanc 0'))

    def test_reference_survives_reopen_without_overwriting_previous_estimation(self):
        first = self.save()
        second = self.tools.execute('save_recipe', {'query': self.query, 'components': [
                    {'food_id': 'base', 'grams': 100}, {'food_id': 'cheese', 'grams': 300}], 'note': 'Different estimate'})['food']
        self.assertEqual(second['per_100g'], first['per_100g'])
        reopened = lookup_reference(Store(self.directory.name), self.query)
        self.assertEqual(reopened['reference']['portion_grams'], 400)

    def test_private_and_nonhttps_sources_are_rejected(self):
        for url in ['http://example.org/menu', 'https://127.0.0.1/menu', 'https://localhost/menu',
                    'https://192.168.1.1/menu', 'https://user:secret@example.org/menu', 'file:///tmp/menu']:
            with self.assertRaises(ValueError): public_source(url)

    def test_jambon_blanc_is_found_under_its_catalogue_name_jambon_cuit(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO foods VALUES (?,?,?,?,?,?)',
                ('ham', 'Jambon cuit, supérieur', normalize('Jambon cuit, supérieur'), 'Ciqual', '{}', '{}'))
        self.assertEqual(search_foods(self.store, 'jambon blanc')[0]['id'], 'ham')


if __name__ == '__main__':
    unittest.main()
