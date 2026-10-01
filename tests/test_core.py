import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from curl_cffi.requests.exceptions import ConnectionError, SSLError

from app import create_app
from mymiam.dashboard import summary, trends
from mymiam.garmin import normalize_stats
from mymiam.nutrition import normalize, resting_energy, search_foods, totals
from mymiam.openai_plan import ChatGPTPlan, PlanError, protected_write
from scripts.import_ciqual import cell


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app({"TESTING": True, "INSTANCE": self.tmp.name, "PUBLIC_ORIGIN": "https://my.test",
                               "SECURE_COOKIES": True})
        self.client = self.app.test_client()
        self.store = self.app.extensions["store"]
        self.user = {"id": "owner-uuid", "email": "owner@example.test", "legacyOwner": True}
        self.store.bind_owner(self.user["id"])
        self.remote = patch("app.renfo_requests.request", side_effect=self.renfo)
        self.remote.start()
        self.addCleanup(self.remote.stop)
        with self.store.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(b"test-session").hexdigest(), "r"*32, time.time()+60))
            for food_id, name, kcal in (("ciqual:rice", "Riz blanc cuit", 130), ("ciqual:chicken", "Poulet cuit", 165)):
                db.execute("INSERT INTO foods VALUES (?,?,?,?,?,?)", (food_id, name, normalize(name), "Ciqual 2025", json.dumps({"kcal":kcal,"protein":10,"carbs":20,"fat":2,"fiber":1}), "{}"))
        self.client.set_cookie("mymiam_session", "test-session")
        self.headers = {"Origin": "https://my.test"}
        self.day = (date.today()-timedelta(days=1)).isoformat()
        self.profile = {"birth_date":"1990-01-01", "sex":"male", "height":180,"weight":80,
                        "activity_factor":1.4,"deficit":250,"protein_pct":20,"carbs_pct":45,"fat_pct":35}
        self.client.put("/api/profile", json=self.profile, headers=self.headers)

    def renfo(self, method, url, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda:{"user":self.user},
                               cookies=SimpleNamespace(get=lambda name:"r"*32))

    def meal(self, **changes):
        payload={"day":self.day,"slot":"lunch","title":"Déjeuner","text":"200g riz cuit",
                 "request_id":"request_1234567890","items":[{"food_id":"ciqual:rice","grams":200}]}
        payload.update(changes)
        return self.client.post("/api/meals",json=payload,headers=self.headers)

    def test_private_routes_require_session(self):
        self.client.delete_cookie("mymiam_session")
        for path in ("/api/dashboard","/api/profile","/api/integrations","/api/export","/api/favorites"):
            self.assertEqual(self.client.get(path).status_code,401)

    def test_wrong_and_missing_origin_rejected(self):
        for headers in ({}, {"Origin":"https://evil.test"}):
            self.assertEqual(self.client.put('/api/profile',json=self.profile,headers=headers).status_code,403)

    def test_friends_cannot_log_in_or_use_existing_session(self):
        self.user={"id":"friend-uuid","email":"friend@example.test","legacyOwner":False}
        self.assertEqual(self.client.post('/api/auth/login',json={"email":"friend@example.test","password":"password"},headers=self.headers).status_code,403)
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)

    def test_renfo_outage_fails_closed(self):
        with patch('app.renfo_requests.request',side_effect=ConnectionError('connection unavailable')):
            self.assertEqual(self.client.get('/api/dashboard').status_code,503)

    def test_renfo_certificate_failure_fails_closed(self):
        with patch('app.renfo_requests.request',side_effect=SSLError('certificate rejected')):
            self.assertEqual(self.client.get('/api/dashboard').status_code,503)

    def test_renfo_tls_verification_and_redirects(self):
        with patch('app.renfo_requests.request',side_effect=self.renfo) as remote:
            self.assertEqual(self.client.get('/api/dashboard').status_code,200)
        self.assertTrue(remote.call_args.kwargs['verify'])
        self.assertFalse(remote.call_args.kwargs['allow_redirects'])
        self.assertEqual(remote.call_args.args[1], 'https://95.216.152.157.sslip.io/api/auth/me')

    def test_logout_clears_local_session_even_when_renfo_is_down(self):
        with patch('app.renfo_requests.request',side_effect=ConnectionError('connection unavailable')):
            response=self.client.post('/api/auth/logout',json={},headers=self.headers)
        self.assertEqual(response.status_code,200)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)

    def test_login_cookie_security_and_no_password_in_database(self):
        response=self.client.post('/api/auth/login',json={"email":"owner@example.test","password":"never-store-this"},headers=self.headers)
        self.assertEqual(response.status_code,200)
        cookie=response.headers['Set-Cookie']
        for attribute in ('Secure','HttpOnly','SameSite=Strict'):self.assertIn(attribute,cookie)
        self.assertNotIn(b'never-store-this',self.store.path.read_bytes())

    def test_idempotent_save_and_payload_conflict(self):
        first, second=self.meal(),self.meal()
        self.assertEqual(first.status_code,201)
        self.assertEqual(first.json['id'],second.json['id'])
        self.assertEqual(self.meal(title='Autre repas').status_code,409)
        self.assertEqual(len(summary(self.store,self.user['id'],self.day)['meals']),1)

    def test_client_nutrients_never_override_catalogue(self):
        self.meal(items=[{"food_id":"ciqual:rice","grams":200,"nutrients":{"kcal":1}}])
        self.assertEqual(summary(self.store,self.user['id'],self.day)['intake']['kcal'],260)

    def test_invalid_quantities_unknown_foods_rejected(self):
        for grams in (0,-1,10001,'NaN','Infinity',True):
            self.assertEqual(self.meal(items=[{"food_id":"ciqual:rice","grams":grams}]).status_code,400)
        self.assertEqual(self.meal(items=[{"food_id":"unknown","grams":200}]).status_code,400)

    def test_missing_nutrients_not_silently_zero(self):
        with self.store.connect() as db:
            db.execute("UPDATE foods SET nutrients=? WHERE id='ciqual:rice'",(json.dumps({"kcal":None,"protein":None,"carbs":20,"fat":2,"fiber":1}),))
        self.meal()
        day=summary(self.store,self.user['id'],self.day)
        self.assertIsNone(day['intake']['kcal'])
        self.assertIsNone(day['deficit'])
        self.assertEqual(trends(self.store,self.user['id'],self.day)['covered_days'],0)

    def test_unlogged_days_not_zero_intake_in_cumulative(self):
        data=trends(self.store,self.user['id'],self.day)
        self.assertEqual(data['covered_days'],0)
        self.assertEqual(data['cumulative_deficit'],0)

    def test_surplus_reduces_cumulative_deficit(self):
        self.meal(items=[{"food_id":"ciqual:rice","grams":3000}])
        data=trends(self.store,self.user['id'],self.day)
        self.assertLess(data['cumulative_deficit'],0)
        self.assertEqual(data['covered_days'],1)

    def test_deleting_last_meal_removes_day_from_history(self):
        response=self.meal()
        self.assertTrue(summary(self.store,self.user['id'],self.day)['complete'])
        self.client.delete('/api/meals/'+response.json['id'],json={},headers=self.headers)
        self.assertFalse(summary(self.store,self.user['id'],self.day)['complete'])
        self.assertIsNone(summary(self.store,self.user['id'],self.day)['deficit'])
        self.assertEqual(trends(self.store,self.user['id'],self.day)['covered_days'],0)

    def test_add_edit_delete_recalculates_history_without_confirmation(self):
        first=self.meal()
        before=trends(self.store,self.user['id'],self.day)['cumulative_deficit']
        second=self.meal(request_id='another_request_1234567890')
        self.assertEqual(trends(self.store,self.user['id'],self.day)['cumulative_deficit'],before-260)
        meal=summary(self.store,self.user['id'],self.day)['meals'][0]
        edited=self.client.put('/api/meals/'+first.json['id'],json={
            **meal,'items':[{'food_id':'ciqual:rice','grams':100}]},headers=self.headers)
        self.assertEqual(edited.status_code,200)
        self.assertEqual(trends(self.store,self.user['id'],self.day)['cumulative_deficit'],before-130)
        self.client.delete('/api/meals/'+second.json['id'],json={},headers=self.headers)
        self.assertEqual(trends(self.store,self.user['id'],self.day)['cumulative_deficit'],before+130)

    def test_existing_unconfirmed_meals_are_included_automatically(self):
        self.meal()
        with self.store.connect() as db:
            db.execute('UPDATE days SET complete=0 WHERE user_id=?',(self.user['id'],))
        self.assertEqual(trends(self.store,self.user['id'],self.day)['covered_days'],1)

    def test_moving_meal_recalculates_both_days(self):
        result=self.meal()
        moved=(date.fromisoformat(self.day)-timedelta(days=1)).isoformat()
        meal=summary(self.store,self.user['id'],self.day)['meals'][0]
        response=self.client.put('/api/meals/'+result.json['id'],json={
            **meal,'day':moved,'items':[{'food_id':'ciqual:rice','grams':200}]},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertIsNone(summary(self.store,self.user['id'],self.day)['deficit'])
        self.assertIsNotNone(summary(self.store,self.user['id'],moved)['deficit'])
        self.assertEqual(trends(self.store,self.user['id'],self.day)['covered_days'],1)

    def test_other_user_cannot_delete_owner_meal(self):
        response=self.meal()
        with self.store.connect() as db:
            db.execute("UPDATE meals SET user_id='another' WHERE id=?",(response.json['id'],))
        self.assertEqual(self.client.delete('/api/meals/'+response.json['id'],json={},headers=self.headers).status_code,404)

    def test_garmin_total_replaces_baseline_not_added(self):
        raw=normalize_stats({'totalKilocalories':2600,'activeKilocalories':850,'bmrKilocalories':1750},self.day)
        with self.store.connect() as db:db.execute('INSERT INTO garmin_days VALUES (?,?,?)',(self.user['id'],self.day,json.dumps(raw)))
        day=summary(self.store,self.user['id'],self.day)
        self.assertEqual(day['expenditure'],2600)
        self.assertNotEqual(day['expenditure'],3450)

    def test_partial_garmin_does_not_replace_full_day_projection(self):
        day=date.today().isoformat()
        raw=normalize_stats({'totalKilocalories':500,'activeKilocalories':50,'bmrKilocalories':450},day)
        with self.store.connect() as db:db.execute('INSERT INTO garmin_days VALUES (?,?,?)',(self.user['id'],day,json.dumps(raw)))
        result=summary(self.store,self.user['id'],day)
        self.assertTrue(result['garmin']['partial'])
        self.assertEqual(result['expenditure'],round(result['resting']*1.4))

    def test_today_excluded_from_cumulative_automatically(self):
        self.meal(day=date.today().isoformat())
        self.assertEqual(trends(self.store,self.user['id'],date.today().isoformat())['covered_days'],0)

    def test_profile_change_preserves_logged_past_day_targets(self):
        self.meal()
        before=summary(self.store,self.user['id'],self.day)['targets']
        self.client.put('/api/profile',json={**self.profile,'deficit':500,'activity_factor':1.8},headers=self.headers)
        self.assertEqual(summary(self.store,self.user['id'],self.day)['targets'],before)

    def test_today_profile_changes_apply_without_freezing_goals(self):
        day=date.today().isoformat()
        self.meal(day=day)
        before=summary(self.store,self.user['id'],day)['targets']['kcal']
        self.client.put('/api/profile',json={**self.profile,'deficit':500},headers=self.headers)
        self.assertEqual(summary(self.store,self.user['id'],day)['targets']['kcal'],before-250)

    def test_profile_macro_percentages_must_sum_to_100(self):
        self.assertEqual(self.client.put('/api/profile',json={**self.profile,'protein_pct':60},headers=self.headers).status_code,400)

    def test_import_zero_is_real_zero_limits_remain_unknown(self):
        self.assertEqual(cell(0),(0.0,None))
        for value in ('-','traces','< 0,2',None):self.assertIsNone(cell(value)[0])
        self.assertEqual(cell('1,25')[0],1.25)

    def test_french_apostrophes_and_cooking_qualifiers(self):
        self.assertEqual(normalize("huile d’olive"),normalize("huile d'olive"))
        self.assertEqual(normalize("œuf"),"oeuf")
        with self.store.connect() as db:
            for key,name in [('raw','Poulet, filet sans peau cru'),('cooked','Poulet, filet sans peau grillé/poêlé')]:
                db.execute('INSERT INTO foods VALUES (?,?,?,?,?,?)',(key,name,normalize(name),'Ciqual 2025','{}','{}'))
        matches=search_foods(self.store,'blanc de poulet cuit')
        self.assertEqual([f['id'] for f in matches],['cooked'])

    def test_export_does_not_include_credentials(self):
        body=self.client.get('/api/export').json
        self.assertNotIn('sessions',body)
        self.assertNotIn('settings',body)
        self.assertNotIn('renfo_token',json.dumps(body))

    def test_oauth_state_mismatch_expiry_and_wrong_client_rejected(self):
        plan=ChatGPTPlan(self.tmp.name)
        attempt,_=plan.authorization('http://127.0.0.1:9455/auth/callback')
        for query in ({'state':'bad','code':'fake'}, {'state':attempt['state'],'code':'fake','client_id':'dynamic_agent_client'}):
            with self.assertRaises(PlanError):plan.exchange(attempt,query)
        attempt['created']=time.time()-601
        with self.assertRaises(PlanError):plan.exchange(attempt,{'state':attempt['state'],'code':'fake','client_id':'oaiapp_fake'})

    def test_no_plan_no_paid_api_fallback(self):
        plan=self.app.extensions['plan']
        self.assertFalse(plan.status()['paid_fallback'])
        with patch('mymiam.openai_plan.requests.post') as network:
            with self.assertRaises(PlanError):plan.models()
            network.assert_not_called()

    def test_plan_stream_without_final_output_uses_completed_deltas(self):
        from unittest.mock import MagicMock
        plan=self.app.extensions['plan']
        protected_write(plan.path,{'access_token':'test-only','expires_at':time.time()+600})
        protected_write(Path(self.tmp.name)/'model.json',{'slug':'gpt-6-luna'})
        draft={'title':'Riz','items':[{'label':'riz cuit','grams':200,'estimated':False,'note':''}],'questions':[]}
        events=[{'type':'response.output_text.delta','delta':json.dumps(draft)}, {'type':'response.completed','response':{'usage':{'input_tokens':10}}}]
        response=MagicMock(status_code=200)
        response.__enter__.return_value=response
        response.iter_lines.return_value=[('data: '+json.dumps(e)).encode() for e in events]
        with patch('mymiam.openai_plan.requests.post',return_value=response):
            self.assertEqual(plan.parse('200g riz',[]),draft)

    def test_failed_stream_never_accepts_already_received_text(self):
        from unittest.mock import MagicMock
        plan=self.app.extensions['plan']
        protected_write(plan.path,{'access_token':'test-only','expires_at':time.time()+600})
        protected_write(Path(self.tmp.name)/'model.json',{'slug':'gpt-6-luna'})
        events=[{'type':'response.output_text.delta','delta':'{"items":[]}'}, {'type':'response.failed'}]
        response=MagicMock(status_code=200)
        response.__enter__.return_value=response
        response.iter_lines.return_value=[('data: '+json.dumps(e)).encode() for e in events]
        with patch('mymiam.openai_plan.requests.post',return_value=response):
            with self.assertRaises(PlanError):plan.parse('riz',[])


if __name__=='__main__':unittest.main()
