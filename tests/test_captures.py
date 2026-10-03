import unittest
import json
from unittest.mock import Mock

import test_core
from mymiam.captures import CaptureWorker, prepare_draft, matches_for
from mymiam.dashboard import summary, trends
from mymiam.openai_plan import PlanError
from mymiam.nutrition import normalize


class CaptureTests(unittest.TestCase):
    renfo = test_core.CoreTests.renfo
    # Reuse the authenticated, isolated fixture; baseline tests run separately.
    def setUp(self):
        test_core.CoreTests.setUp(self)
        self.plan = self.app.extensions['plan']
        self.plan.status = Mock(return_value={'connected': True})
        self.plan.parse = Mock(return_value=self.draft())
        self.worker = CaptureWorker(self.store, self.plan)

    @staticmethod
    def draft():
        return {'title': 'Mon dîner', 'slot': 'dinner', 'items': [
            {'label': 'Riz blanc cuit', 'grams': 200, 'estimated': True, 'note': 'Portion estimée'}],
            'clarifications': [{'item_index': 0, 'label': 'Taille de la portion', 'selected': 0,
                'options': [{'label': label, 'food_label': 'Riz blanc cuit', 'grams': grams}
                            for label, grams in [('Moyenne', 200), ('Petite', 100), ('Grande', 300)]]}]}

    def capture(self, **changes):
        return self.client.post('/api/captures', json={
            'day': self.day, 'text': 'Ce soir du riz', 'slot_hint': 'lunch',
            'request_id': 'capture_request_12345', **changes}, headers=self.headers)

    def job(self, capture_id):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM captures WHERE id=?', (capture_id,)).fetchone())

    def test_queue_is_immediate_idempotent_and_does_not_call_luna(self):
        first = self.capture()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.capture().json['id'], first.json['id'])
        self.assertEqual(self.capture(text='autre contenu').status_code, 409)
        self.plan.parse.assert_not_called()
        self.assertEqual(self.job(first.json['id'])['status'], 'queued')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], [])

    def test_worker_saves_guess_inferred_slot_and_refinement_recalculates(self):
        capture_id = self.capture().json['id']
        self.assertTrue(self.worker.process_one())
        self.assertFalse(self.worker.process_one())
        self.assertEqual(self.job(capture_id)['status'], 'done')
        day = summary(self.store, self.user['id'], self.day)
        self.assertEqual(day['meals'][0]['slot'], 'dinner')
        self.assertEqual(day['intake']['kcal'], 260)
        before = trends(self.store, self.user['id'], self.day)['cumulative_deficit']
        response = self.client.post('/api/meals/' + capture_id + '/refine',
            json={'group': 0, 'option': 2, 'grams': 1, 'nutrients': {'kcal': 1}}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(summary(self.store, self.user['id'], self.day)['intake']['kcal'], 390)
        self.assertEqual(trends(self.store, self.user['id'], self.day)['cumulative_deficit'], before - 130)
        self.plan.parse.assert_called_once()

    def test_failed_capture_preserves_text_and_retry_succeeds(self):
        capture_id = self.capture().json['id']
        self.plan.parse.side_effect = PlanError('Quota épuisé')
        self.worker.process_one()
        self.assertEqual(self.job(capture_id)['status'], 'failed')
        self.assertEqual(self.job(capture_id)['text'], 'Ce soir du riz')
        self.plan.parse.side_effect = None
        self.assertEqual(self.client.post('/api/captures/' + capture_id + '/retry', json={}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post('/api/captures/' + capture_id + '/retry', json={}, headers=self.headers).status_code, 409)
        self.worker.process_one()
        self.assertEqual(self.job(capture_id)['status'], 'done')

    def test_cancel_during_inference_prevents_save(self):
        capture_id = self.capture().json['id']
        def cancelled(*args):
            self.assertEqual(self.client.delete('/api/captures/' + capture_id, json={}, headers=self.headers).status_code, 200)
            return self.draft()
        self.plan.parse.side_effect = cancelled
        self.worker.process_one()
        self.assertEqual(self.job(capture_id)['status'], 'cancelled')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], [])

    def test_owner_isolation_for_jobs_and_refinements(self):
        capture_id = self.capture().json['id']
        self.worker.process_one()
        with self.store.connect() as db:
            db.execute("UPDATE captures SET user_id='other' WHERE id=?", (capture_id,))
            db.execute("UPDATE meals SET user_id='other' WHERE id=?", (capture_id,))
        self.assertEqual(self.client.get('/api/captures?day=' + self.day).json['jobs'], [])
        for method, suffix in [(self.client.delete, ''), (self.client.post, '/retry')]:
            self.assertEqual(method('/api/captures/' + capture_id + suffix, json={}, headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post('/api/meals/' + capture_id + '/refine', json={'group': 0, 'option': 1}, headers=self.headers).status_code, 409)

    def test_queue_limit_and_validation(self):
        for i in range(3):
            self.assertEqual(self.capture(request_id='pending_capture_1234' + str(i)).status_code, 202)
        self.assertEqual(self.capture(request_id='fourth_capture_123456').status_code, 429)
        for payload in [{'text': 'a'}, {'slot_hint': 'invalid'}, {'slot_hint': ['lunch']}, {'request_id': 'short'}, {'day': '2099-01-01'}]:
            self.assertEqual(self.capture(**payload).status_code, 400)

    def test_explicit_mass_cannot_be_changed_by_size_options(self):
        draft = self.draft()
        draft['items'][0]['estimated'] = False
        prepared = prepare_draft(self.store, draft)
        self.assertEqual(prepared['items'][0]['grams'], 200)
        self.assertEqual(prepared['clarifications'], [])
        self.assertEqual(prepared['questions'], [])

    def test_unknown_composition_does_not_invent_calories(self):
        self.plan.parse.return_value = {'slot': 'dinner', 'title': 'Inconnu', 'items': [
            {'label': 'xyzzyunknown', 'grams': 150, 'estimated': True}], 'clarifications': []}
        self.capture()
        self.worker.process_one()
        self.assertIsNone(summary(self.store, self.user['id'], self.day)['intake']['kcal'])
        self.assertEqual(trends(self.store, self.user['id'], self.day)['covered_days'], 0)

    def test_generic_cooked_pizza_matches_ready_to_eat_pizza_not_dough(self):
        with self.store.connect() as db:
            for food_id, name in [('pizza', 'Pizza (aliment moyen)'), ('dough', 'Pâte à pizza, cuite')]:
                db.execute('INSERT INTO foods VALUES (?,?,?,?,?,?)',
                    (food_id, name, normalize(name), 'Ciqual', json.dumps({'kcal': 234}), '{}'))
        self.assertEqual(matches_for(self.store, 'pizza garniture non précisée, cuite')[0]['id'], 'pizza')
        self.assertEqual(matches_for(self.store, 'pizza cuite')[0]['id'], 'pizza')
        self.assertEqual(matches_for(self.store, 'pizza cuite type standard')[0]['id'], 'pizza')

    def test_invalid_luna_quantity_fails_without_saving(self):
        self.plan.parse.return_value['items'][0]['grams'] = None
        capture_id = self.capture().json['id']
        self.worker.process_one()
        self.assertEqual(self.job(capture_id)['status'], 'failed')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], [])

    def test_manual_edit_discards_stale_refinements(self):
        capture_id = self.capture().json['id']
        self.worker.process_one()
        meal = summary(self.store, self.user['id'], self.day)['meals'][0]
        response = self.client.put('/api/meals/' + capture_id, json={**meal,
            'items': [{'food_id': 'ciqual:rice', 'grams': 180}]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post('/api/meals/' + capture_id + '/refine',
            json={'group': 0, 'option': 1}, headers=self.headers).status_code, 409)

    def test_one_narrative_splits_periods_preserves_foods_and_remaps_options(self):
        draft = self.draft()
        draft['items'] = [{**draft['items'][0], 'slot': slot, 'label': label}
                          for slot, label in [('lunch', 'Riz blanc cuit'), ('dinner', 'Riz blanc cuit')]]
        draft['clarifications'][0]['item_index'] = 1
        self.plan.parse.return_value = draft
        capture_id = self.capture(text='A midi du riz et ce soir du riz').json['id']
        self.worker.process_one()
        meals = summary(self.store, self.user['id'], self.day)['meals']
        by_slot = {meal['slot']: meal for meal in meals}
        self.assertEqual(set(by_slot), {'lunch', 'dinner'})
        self.assertEqual(sum(len(meal['items']) for meal in meals), 2)
        self.assertEqual(by_slot['lunch']['clarifications'], [])
        self.assertEqual(by_slot['dinner']['clarifications'][0]['item_index'], 0)
        self.assertEqual(summary(self.store, self.user['id'], self.day)['intake']['kcal'], 520)
        self.assertEqual(self.client.post('/api/meals/' + by_slot['dinner']['id'] + '/refine',
            json={'group': 0, 'option': 2}, headers=self.headers).status_code, 200)
        # A recovered job cannot duplicate periods or overwrite edited portions.
        with self.store.connect() as db:
            db.execute("UPDATE captures SET status='queued' WHERE id=?", (capture_id,))
        self.worker.process_one()
        day = summary(self.store, self.user['id'], self.day)
        self.assertEqual(len(day['meals']), 2)
        self.assertEqual(day['intake']['kcal'], 650)

    def test_cancelled_multi_period_narrative_saves_nothing(self):
        capture_id = self.capture().json['id']
        def cancelled(*args):
            self.client.delete('/api/captures/' + capture_id, json={}, headers=self.headers)
            draft = self.draft()
            draft['items'] = [{**draft['items'][0], 'slot': slot} for slot in ('lunch', 'dinner')]
            return draft
        self.plan.parse.side_effect = cancelled
        self.worker.process_one()
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], [])

    def test_multi_period_failure_rolls_back_the_whole_journal_write(self):
        draft = self.draft()
        draft['items'] = [{**draft['items'][0], 'slot': slot} for slot in ('lunch', 'dinner')]
        self.plan.parse.return_value = draft
        capture_id = self.capture().json['id']
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_test_dinner BEFORE INSERT ON meals WHEN NEW.slot='dinner' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        self.worker.process_one()
        self.assertEqual(self.job(capture_id)['status'], 'failed')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM meal_insights').fetchone()[0], 0)

    def reanalyse(self, meal_id):
        return self.client.post('/api/meals/' + meal_id + '/reanalyse', json={}, headers=self.headers)

    def test_reanalysis_is_immediate_idempotent_and_replaces_without_duplicate(self):
        original = self.capture().json['id']
        self.worker.process_one()
        result = self.reanalyse(original)
        self.assertEqual(result.status_code, 202)
        replacement = result.json['id']
        self.assertEqual(self.reanalyse(original).json['id'], replacement)
        self.assertEqual(summary(self.store, self.user['id'], self.day)['intake']['kcal'], 260)
        self.assertEqual(self.plan.parse.call_count, 1)
        self.plan.parse.return_value['items'][0]['grams'] = 300
        self.worker.process_one()
        meals = summary(self.store, self.user['id'], self.day)['meals']
        self.assertEqual(len(meals), 1)
        self.assertEqual(meals[0]['id'], replacement)
        self.assertEqual(meals[0]['text'], 'Ce soir du riz')
        self.assertEqual(meals[0]['totals']['kcal'], 390)

    def test_reanalysis_failure_retry_and_cancel_preserve_old_meal(self):
        original = self.capture().json['id']
        self.worker.process_one()
        before = summary(self.store, self.user['id'], self.day)['meals']
        replacement = self.reanalyse(original).json['id']
        self.plan.parse.side_effect = PlanError('Quota épuisé')
        self.worker.process_one()
        self.assertEqual(self.job(replacement)['status'], 'failed')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], before)
        self.client.post('/api/captures/' + replacement + '/retry', json={}, headers=self.headers)
        def cancelled(*args):
            self.client.delete('/api/captures/' + replacement, json={}, headers=self.headers)
            return self.draft()
        self.plan.parse.side_effect = cancelled
        self.worker.process_one()
        self.assertEqual(self.job(replacement)['status'], 'cancelled')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], before)

    def test_reanalysis_does_not_overwrite_edits_or_restore_deleted_meals(self):
        original = self.capture().json['id']
        self.worker.process_one()
        replacement = self.reanalyse(original).json['id']
        def modified(*args):
            meal = summary(self.store, self.user['id'], self.day)['meals'][0]
            self.client.put('/api/meals/' + original, json={**meal,
                'items': [{'food_id': 'ciqual:rice', 'grams': 180}]}, headers=self.headers)
            return self.draft()
        self.plan.parse.side_effect = modified
        self.worker.process_one()
        self.assertEqual(self.job(replacement)['status'], 'failed')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['intake']['kcal'], 234)
        replacement2 = self.reanalyse(original).json['id']
        def deleted(*args):
            self.client.delete('/api/meals/' + original, json={}, headers=self.headers)
            return self.draft()
        self.plan.parse.side_effect = deleted
        self.worker.process_one()
        self.assertEqual(self.job(replacement2)['status'], 'failed')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], [])

    def test_reanalysing_split_narrative_replaces_all_siblings_once(self):
        original = self.capture(text='A midi du riz et ce soir du riz').json['id']
        self.worker.process_one()  # Original legacy analysis had one evening meal.
        draft = self.draft()
        draft['items'] = [{**draft['items'][0], 'slot': slot} for slot in ('lunch', 'dinner')]
        self.plan.parse.return_value = draft
        self.reanalyse(original)
        self.worker.process_one()
        meals = summary(self.store, self.user['id'], self.day)['meals']
        self.assertEqual(len(meals), 2)
        self.assertEqual({m['slot'] for m in meals}, {'lunch', 'dinner'})
        # Clicking either sibling reanalyses the original capture, not an extra
        # copy of its other period. Even concurrent clicks share the same job.
        first = self.reanalyse(meals[0]['id']).json['id']
        self.assertEqual(self.reanalyse(meals[1]['id']).json['id'], first)
        self.worker.process_one()
        day = summary(self.store, self.user['id'], self.day)
        self.assertEqual(len(day['meals']), 2)
        self.assertEqual(day['intake']['kcal'], 520)

    def test_reanalysis_owner_isolation_missing_text_and_manual_period_scope(self):
        original = self.capture(text='Ce midi du riz et ce soir du riz').json['id']
        self.worker.process_one()
        with self.store.connect() as db:
            db.execute("UPDATE meals SET request_id='manual_request_12345' WHERE id=?", (original,))
        draft = self.draft()
        draft['items'] = [{**draft['items'][0], 'slot': slot} for slot in ('lunch', 'dinner')]
        self.plan.parse.return_value = draft
        self.reanalyse(original)
        self.worker.process_one()
        day = summary(self.store, self.user['id'], self.day)
        self.assertEqual(len(day['meals']), 1)
        self.assertEqual(day['meals'][0]['slot'], 'dinner')
        meal_id = day['meals'][0]['id']
        with self.store.connect() as db:
            db.execute("UPDATE meals SET text='' WHERE id=?", (meal_id,))
        self.assertEqual(self.reanalyse(meal_id).status_code, 400)
        with self.store.connect() as db:
            db.execute("UPDATE meals SET user_id='other' WHERE id=?", (meal_id,))
        self.assertEqual(self.reanalyse(meal_id).status_code, 404)

    def test_migration_restores_origin_links_without_altering_journal(self):
        from mymiam.storage import Store
        original = self.capture().json['id']
        self.worker.process_one()
        before = summary(self.store, self.user['id'], self.day)['meals']
        with self.store.connect() as db:
            db.execute('DELETE FROM meal_origins')
        reopened = Store(self.store.directory)
        self.assertEqual(summary(reopened, self.user['id'], self.day)['meals'], before)
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT group_id FROM meal_origins WHERE meal_id=?', (original,)).fetchone()[0], original)

    def test_reanalysis_preserves_explicitly_corrected_quantities(self):
        original = self.capture().json['id']
        self.worker.process_one()
        meal = summary(self.store, self.user['id'], self.day)['meals'][0]
        self.client.put('/api/meals/' + original, json={**meal,
            'items': [{'food_id': 'ciqual:rice', 'grams': 180, 'estimated': False}]}, headers=self.headers)
        self.reanalyse(original)
        self.worker.process_one()  # Model mock still guesses 200 g.
        day = summary(self.store, self.user['id'], self.day)
        self.assertEqual(day['intake']['kcal'], 234)
        self.assertEqual(day['meals'][0]['items'][0]['grams'], 180)
        self.assertEqual(day['meals'][0]['clarifications'], [])
        self.assertEqual(self.plan.parse.call_args[0][2]['reviewed_items'][0]['grams'], 180)

    def test_failed_split_replacement_rolls_back_original_meal(self):
        original = self.capture().json['id']
        self.worker.process_one()
        before = summary(self.store, self.user['id'], self.day)['meals']
        draft = self.draft()
        draft['items'] = [{**draft['items'][0], 'slot': slot} for slot in ('lunch', 'dinner')]
        self.plan.parse.return_value = draft
        replacement = self.reanalyse(original).json['id']
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER reject_reanalysis_dinner BEFORE INSERT ON meals WHEN NEW.slot='dinner' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        self.worker.process_one()
        self.assertEqual(self.job(replacement)['status'], 'failed')
        self.assertEqual(summary(self.store, self.user['id'], self.day)['meals'], before)

    def test_reanalysis_does_not_resurrect_previously_deleted_period(self):
        draft = self.draft()
        draft['items'] = [{**draft['items'][0], 'slot': slot} for slot in ('lunch', 'dinner')]
        self.plan.parse.return_value = draft
        self.capture(text='A midi du riz et ce soir du riz')
        self.worker.process_one()
        by_slot = {m['slot']: m for m in summary(self.store, self.user['id'], self.day)['meals']}
        self.client.delete('/api/meals/' + by_slot['lunch']['id'], json={}, headers=self.headers)
        self.reanalyse(by_slot['dinner']['id'])
        self.worker.process_one()
        day = summary(self.store, self.user['id'], self.day)
        self.assertEqual(len(day['meals']), 1)
        self.assertEqual(day['meals'][0]['slot'], 'dinner')
        self.assertEqual(day['intake']['kcal'], 260)
