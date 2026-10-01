"""Durable capture queue and optional, server-validated meal refinements."""
import json
import time
import uuid
from datetime import datetime, timezone

import requests

from .nutrition import NUTRIENTS, finite_number, normalize, resolve_items, food_record
from .openai_plan import PlanError
from .catalogue import matches_for

ACTIVE = ('queued', 'analysing')
SLOTS = {'breakfast', 'lunch', 'dinner', 'snack'}


def selected_matches(store, value, label):
    if 'food_id' not in value:
        return matches_for(store, label)  # Compatibility for pre-tool drafts.
    if value['food_id'] is None:
        return []
    with store.connect() as db:
        row = db.execute('SELECT * FROM foods WHERE id=?', (str(value['food_id']),)).fetchone()
    if not row:
        raise ValueError('Aliment du catalogue introuvable')
    return [food_record(row)]


def prepare_draft(store, draft, context=None):
    if not isinstance(draft, dict) or not isinstance(draft.get('items'), list) or not 1 <= len(draft['items']) <= 40:
        raise PlanError("Luna n'a pas fourni un repas exploitable. Tu peux réessayer ou saisir le texte.")
    for item in draft['items']:
        if not isinstance(item, dict):
            raise PlanError("Luna n'a pas fourni un aliment exploitable.")
        label = str(item.get('label', '')).strip()[:150]
        if not label:
            raise PlanError("Un aliment n'a pas été identifié.")
        matches = selected_matches(store, item, label)
        if not matches:
            # Save unknown composition as unknown, rather than inventing nutrients.
            food_id = 'unresolved:' + uuid.uuid4().hex
            food = {'id': food_id, 'name': label, 'source': 'Composition à préciser',
                    'nutrients': {key: None for key in NUTRIENTS}, 'flags': {'composition': 'Inconnue'}}
            with store.connect() as db:
                db.execute('INSERT INTO foods VALUES (?,?,?,?,?,?)',
                    (food_id, label, normalize(label), food['source'], json.dumps(food['nutrients']), json.dumps(food['flags'])))
            matches = [food]
        item.update(label=label, matches=matches, food_id=matches[0]['id'],
                    grams=finite_number(item.get('grams'), 0.1, 10000, 'Quantité estimée'),
                    note=str(item.get('note', ''))[:300], estimated=bool(item.get('estimated')))
    hint = (context or {}).get('slot_hint')
    draft['slot'] = draft.get('slot') if draft.get('slot') in SLOTS else hint if hint in SLOTS else 'lunch'
    draft['questions'] = []
    refinements = []
    refined_items = set()
    groups = draft.get('clarifications', [])
    for group in (groups if isinstance(groups, list) else [])[:2]:
        if not isinstance(group, dict):
            continue
        index = group.get('item_index')
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(draft['items']) or index in refined_items:
            continue
        item = draft['items'][index]
        # Preserve an explicit mass; allow alternatives for food identity only.
        options = []
        choices = group.get('options', [])
        for option in (choices if isinstance(choices, list) else [])[:3]:
            try:
                grams = finite_number(option.get('grams'), 0.1, 10000, 'Portion')
                if not item['estimated'] and grams != item['grams']:
                    continue
                foods = selected_matches(store, option, str(option.get('food_label', ''))[:150])
                if not foods:
                    continue
                value = {'label': str(option.get('label', 'Option'))[:80], 'food_id': foods[0]['id'], 'grams': grams}
                if not any((v['food_id'], v['grams']) == (value['food_id'], grams) for v in options):
                    options.append(value)
            except (ValueError, AttributeError):
                continue
        # Ensure ambiguous model labels still give clickable, distinct sizes.
        labels = [normalize(value['label']) for value in options]
        for value in options:
            if labels.count(normalize(value['label'])) > 1:
                value['label'] = value['label'][:55] + f" · {value['grams']:g} g"
        default = next((v for v in options if v['food_id'] == item['food_id'] and v['grams'] == item['grams']),
                       {'label': 'Estimation retenue', 'food_id': item['food_id'], 'grams': item['grams']})
        others = [v for v in options if (v['food_id'], v['grams']) != (default['food_id'], default['grams'])]
        if others:
            refined_items.add(index)
            refinements.append({'item_index': index, 'label': str(group.get('label', 'Préciser cette estimation'))[:120],
                                'selected': 0, 'options': [default] + others[:2]})
    draft['clarifications'] = refinements
    return draft


def favorite_context(store, user_id):
    with store.connect() as db:
        return [{'name': r['title'], 'items': [{'label': i['name'], 'grams': i['grams']} for i in json.loads(r['items'])]}
                for r in db.execute('SELECT * FROM favorites WHERE user_id=? LIMIT 20', (user_id,))]


class CaptureWorker:
    def __init__(self, store, plan):
        self.store, self.plan = store, plan

    def process_one(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM captures WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return False
            job = dict(row)
            db.execute('UPDATE captures SET status=?,updated=? WHERE id=?',
                       ('analysing', time.time(), job['id']))
        try:
            text = job['text']
            context = {'day': job['day'], 'slot_hint': job['slot_hint'], 'local_hour': datetime.now().hour}
            draft = prepare_draft(self.store, self.plan.parse(text, favorite_context(self.store, job['user_id']), context), context)
            items = resolve_items(self.store, draft['items'])
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT status FROM captures WHERE id=?', (job['id'],)).fetchone()
                if not row or row[0] == 'cancelled':
                    return True
                db.execute('INSERT OR IGNORE INTO meals VALUES (?,?,?,?,?,?,?,?,?)',
                    (job['id'], job['user_id'], job['day'], draft['slot'], str(draft.get('title') or 'Mon repas')[:120],
                     text, json.dumps(items), datetime.now(timezone.utc).isoformat(), 'capture_' + job['id']))
                db.execute('INSERT OR REPLACE INTO meal_insights VALUES (?,?)', (job['id'], json.dumps(draft['clarifications'])))
                self.store.refresh_day(db, job['user_id'], job['day'])
                db.execute("UPDATE captures SET status='done',meal_id=?,text=?,error=NULL,updated=? WHERE id=?",
                           (job['id'], text, time.time(), job['id']))
        except (ValueError, PlanError) as error:
            self.fail(job, str(error))
        except requests.RequestException:
            self.fail(job, 'Luna est momentanément indisponible. Ton envoi est conservé ; tu peux réessayer.')
        except Exception:
            self.fail(job, 'Le traitement a été interrompu. Ton envoi est conservé ; tu peux réessayer.')
        return True

    def fail(self, job, message):
        with self.store.connect() as db:
            db.execute("UPDATE captures SET status='failed',error=?,updated=? WHERE id=? AND status!='cancelled'",
                       (message[:500], time.time(), job['id']))

    def cleanup(self):
        with self.store.connect() as db:
            for row in db.execute("SELECT id FROM captures WHERE status IN ('cancelled','done') AND created<?",
                                  (time.time() - 86400,)).fetchall():
                db.execute('DELETE FROM captures WHERE id=?', (row['id'],))
