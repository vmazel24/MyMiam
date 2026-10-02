import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mymiam.storage import Store
from mymiam.nutrition import normalize
from mymiam.nutrition_tools import NutritionTools
from mymiam.openai_plan import ChatGPTPlan, PlanError, protected_write
from mymiam.captures import prepare_draft


class NutritionToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name)
        self.plan = ChatGPTPlan(directory.name, self.store)
        protected_write(self.plan.path, {'access_token': 'test-only', 'expires_at': time.time() + 600})
        protected_write(Path(directory.name) / 'model.json', {'slug': 'gpt-5.6-luna'})
        with self.store.connect() as db:
            for food_id, name, kcal in [('average','Pizza (aliment moyen)',234),('cheese','Pizza aux fromages, préemballée',269)]:
                nutrients={'kcal':kcal, 'protein':10, 'carbs':30, 'fat':8, 'fiber':None}
                db.execute('INSERT INTO foods VALUES (?,?,?,?,?,?)',
                    (food_id,name,normalize(name),'Ciqual',json.dumps(nutrients),'{}'))
        self.tools = NutritionTools(self.store)

    @staticmethod
    def stream(output=None, text=None, failure=False):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        events=[]
        if output:
            events.extend({'type':'response.output_item.done','output_index':i,'item':item} for i,item in enumerate(output))
        if text:
            events.append({'type':'response.output_text.delta','delta':json.dumps(text)})
        events.append({'type':'response.failed'} if failure else {
            'type':'response.completed','response':{'usage':{'input_tokens':10,'output_tokens':5},'output':[]}})
        response.iter_lines.return_value=[('data: '+json.dumps(event)).encode() for event in events]
        return response

    @staticmethod
    def draft(food_id='cheese'):
        return {'title':'Pizza', 'slot':'dinner', 'items':[
            {'label':'pizza', 'food_id':food_id,'grams':700,'estimated':True,'note':'Deux pizzas'}],
            'clarifications':[{'item_index':0,'label':'Taille','selected':0,'options':[
                {'label':'Moyennes','grams':700,'food_label':'pizza','food_id':food_id},
                {'label':'Grandes','grams':1000,'food_label':'pizza','food_id':food_id}]}]}

    @staticmethod
    def call(name='search_foods', args=None):
        return {'type':'function_call','id':'fc_test','call_id':'call_test', 'namespace':'nutrition',
                'name':name,'arguments':json.dumps(args or {'queries':['pizza']})}

    def test_search_and_portion_calculation_use_catalogue_and_keep_unknowns(self):
        result=self.tools.execute('nutrition.search_foods',{'queries':['pizza']})
        self.assertEqual(result['results'][0]['foods'][0]['food_id'],'average')
        result=self.tools.execute('calculate_portions',{'items':[{'food_id':'cheese','grams':700}]},'nutrition')
        self.assertEqual(result['totals']['kcal'],1883)
        self.assertIsNone(result['totals']['fiber'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM meals').fetchone()[0],0)

    def test_unsafe_tools_unseen_ids_and_invalid_portions_are_rejected(self):
        for name, args, namespace in [('run_shell',{},None),('search_foods',{'queries':['pizza']},'other'),
              ('calculate_portions',{'items':[{'food_id':'cheese','grams':100}]},None),
              ('search_foods',{'queries':['pizza']*17},None)]:
            with self.assertRaises(ValueError): self.tools.execute(name,args,namespace)
        self.tools.execute('search_foods',{'queries':['pizza']})
        for grams in [0,-1,True,'NaN',10001]:
            with self.assertRaises(ValueError):
                self.tools.execute('calculate_portions',{'items':[{'food_id':'cheese','grams':grams}]})

    def test_model_selection_overrides_first_search_result_and_options_preserve_id(self):
        self.tools.execute('search_foods',{'queries':['pizza']})
        draft=self.draft()
        self.tools.validate_selection(draft)
        prepared=prepare_draft(self.store,draft)
        self.assertEqual(prepared['items'][0]['food_id'],'cheese')
        self.assertTrue(all(option['food_id']=='cheese' for option in prepared['clarifications'][0]['options']))

    def test_unknown_selection_stays_unknown_and_hallucinated_id_is_rejected(self):
        self.tools.validate_selection(self.draft(None))
        draft=prepare_draft(self.store,self.draft(None))
        self.assertTrue(draft['items'][0]['food_id'].startswith('unresolved:'))
        self.assertIsNone(draft['items'][0]['matches'][0]['nutrients']['kcal'])
        with self.assertRaises(ValueError): self.tools.validate_selection(self.draft('invented'))

    def test_duplicate_refinement_labels_are_disambiguated_by_quantity(self):
        draft=self.draft()
        for option in draft['clarifications'][0]['options']:
            option['label']='Deux pizzas'
        prepared=prepare_draft(self.store,draft)
        labels=[value['label'] for value in prepared['clarifications'][0]['options']]
        self.assertEqual(len(set(labels)),2)
        self.assertIn('700 g',labels[0])

    def test_stateless_loop_replays_completed_calls_reasoning_and_server_results(self):
        reasoning={'type':'reasoning','id':'rs_test','summary':[],'encrypted_content':'test-encrypted-reasoning'}
        responses=[self.stream([reasoning,self.call()]), self.stream(text=self.draft())]
        sent=[]
        def post(*args,**kwargs):
            sent.append(json.loads(json.dumps(kwargs['json'])))
            return responses.pop(0)
        with patch('mymiam.openai_plan.requests.post',side_effect=post):
            result=self.plan.parse('Ce soir deux pizzas',[],{'slot_hint':'lunch'})
        self.assertEqual(result['items'][0]['food_id'],'cheese')
        self.assertEqual(sent[0]['tools'][0]['type'],'namespace')
        self.assertEqual(sent[0]['tool_choice'],'required')
        self.assertFalse(sent[0]['store'])
        self.assertTrue(sent[0]['stream'])
        self.assertNotIn('previous_response_id',sent[1])
        self.assertIn(reasoning,sent[1]['input'])
        tool_result=next(item for item in sent[1]['input'] if item.get('type')=='function_call_output')
        self.assertEqual(json.loads(tool_result['output'])['results'][0]['foods'][0]['source'],'Ciqual')
        usage=json.loads((self.store.directory/'last_usage.json').read_text())
        self.assertEqual(usage['usage']['input_tokens'],20)
        self.assertEqual(usage['tool_calls'],1)

    def test_failed_stream_never_executes_completed_tool_call(self):
        with patch('mymiam.openai_plan.requests.post',return_value=self.stream([self.call()],failure=True)), \
             patch('mymiam.openai_plan.NutritionTools.execute') as execute:
            with self.assertRaises(PlanError):self.plan.parse('pizza',[])
            execute.assert_not_called()

    def test_shared_plan_limit_has_explicit_recoverable_error(self):
        response=MagicMock(status_code=200)
        response.__enter__.return_value=response
        response.iter_lines.return_value=[('data: '+json.dumps({'type':'error','error':{
            'code':'subscription_sharing_usage_limit_exceeded'}})).encode()]
        with patch('mymiam.openai_plan.requests.post',return_value=response):
            with self.assertRaisesRegex(PlanError,'Limite d’usage ChatGPT'):
                self.plan.parse('Pizza',[])

    def test_tool_loop_has_bounded_number_of_responses(self):
        sent=[]
        def repeated(*args,**kwargs):
            sent.append(kwargs['json']['tool_choice'])
            return self.stream([self.call()])
        with patch('mymiam.openai_plan.requests.post',side_effect=repeated):
            with self.assertRaises(PlanError):self.plan.parse('pizza',[])
        self.assertEqual(len(sent),4)
        self.assertEqual(sent[-1],'none')

    def test_unknown_tool_returns_error_without_executing_it(self):
        responses=[self.stream([self.call('run_shell',{'cmd':'whoami'})]),self.stream(text=self.draft(None))]
        with patch('mymiam.openai_plan.requests.post',side_effect=responses):
            self.plan.parse('pizza',[])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM meals').fetchone()[0],0)

    def test_live_workflow_research_save_then_reuse_without_web(self):
        query='Prosciutto e Funghi Tripletta Bordeaux'
        evidence={'found':True,'exact_match':True,'kind':'restaurant',
            'name':'Prosciutto e Funghi','canonical_query':query,
            'source_url':'https://example.org/menu','ingredients':['fromage'],
            'barcode':None,'note':'Carte vérifiée'}
        # One local lookup, bounded external research, component lookup, save,
        # then final selection. No meals are created by this catalogue workflow.
        responses=[self.stream([self.call(args={'queries':[query]})]),
            self.stream([self.call('research_food',{'query':query})]),
            self.stream([{'type':'web_search_call','action':{'sources':[{'url':evidence['source_url']}]}}],text=evidence),
            self.stream([self.call(args={'queries':['pizza']})]),
            self.stream([self.call('save_recipe',{'query':query,'components':[{'food_id':'cheese','grams':450}], 'note':'Estimation'})])]
        sent=[]
        def post(*args,**kwargs):
            sent.append(json.loads(json.dumps(kwargs['json'])))
            if responses:return responses.pop(0)
            with self.store.connect() as db:
                food_id=db.execute('SELECT food_id FROM food_references').fetchone()[0]
            return self.stream(text=self.draft(food_id))
        with patch('mymiam.openai_plan.requests.post',side_effect=post):
            first=self.plan.parse('Pizza chez Tripletta Bordeaux',[])
        self.assertEqual(len(sent),6)
        self.assertEqual(sent[2]['tools'][0]['type'],'web_search')
        self.assertNotIn('Pizza chez',sent[2]['input'][0]['content'])
        responses.extend([self.stream([self.call(args={'queries':['pizza entière Prosciutto e Funghi chez Tripletta Bordeaux']})]),
                          self.stream(text=self.draft(first['items'][0]['food_id']))])
        sent.clear()
        with patch('mymiam.openai_plan.requests.post',side_effect=post):
            second=self.plan.parse('Deux pizzas chez Tripletta Bordeaux',[])
        self.assertEqual(first['items'][0]['food_id'],second['items'][0]['food_id'])
        self.assertEqual(len(sent),2)
        usage=json.loads((self.store.directory/'last_usage.json').read_text())
        self.assertEqual(usage['tools'],['search_foods'])

    def test_external_failure_keeps_explicit_generic_estimate(self):
        query='Regina Tripletta Bordeaux'
        responses=[self.stream([self.call(args={'queries':[query]})]),
                   self.stream([self.call('research_food',{'query':query})])]
        # The separate web request fails; the original workflow can still finish.
        def post(*args,**kwargs):
            if kwargs['json']['tools'][0]['type']=='web_search':
                raise PlanError('Outil web indisponible')
            if responses:return responses.pop(0)
            return self.stream(text=self.draft(None))
        with patch('mymiam.openai_plan.requests.post',side_effect=post):
            result=self.plan.parse('Une Regina chez Tripletta Bordeaux',[])
        self.assertIsNone(result['items'][0]['food_id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM food_references').fetchone()[0],0)

    def test_final_answer_cannot_skip_verification_of_named_brand(self):
        query={'label':'pizza','brand':'Auchan','restaurant':None,'city':None}
        responses=[self.stream([self.call(args={'queries':[query]})]),
                   self.stream(text=self.draft(None)),
                   self.stream([self.call('research_food',{'query':'pizza Auchan'})]),
                   self.stream([{'type':'web_search_call','action':{'sources':[]}}],
                               text={'found':False,'exact_match':False,'note':'Pas de source sûre'}),
                   self.stream(text=self.draft(None))]
        sent=[]
        def post(*args,**kwargs):
            sent.append(json.loads(json.dumps(kwargs['json'])))
            return responses.pop(0)
        with patch('mymiam.openai_plan.requests.post',side_effect=post):
            result=self.plan.parse('Une pizza Auchan',[])
        self.assertEqual(result['items'][0]['food_id'], 'average')
        self.assertTrue(result['items'][0]['composition_estimated'])
        self.assertIn('Composition moyenne', result['items'][0]['note'])
        self.assertEqual(len(sent),5)
        self.assertEqual(sent[2]['tool_choice'],'required')
        self.assertTrue(any(item.get('role')=='developer' for item in sent[2]['input']))
