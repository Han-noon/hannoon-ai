"""No network, no real DB: transactional repository double + deterministic engine."""
import sys
import importlib.util
import types
import unittest
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

# The container has no OpenAI SDK; the test never calls any SDK methods.
if importlib.util.find_spec('openai') is None:
    stub = types.ModuleType('openai')
    stub.OpenAI = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('Unexpected AI API'))
    sys.modules['openai'] = stub

from subtopic_classifier import service as svc
from subtopic_classifier.models import EventRow, ArticleRow
from subtopic_classifier.engine import initial_state
from subtopic_classifier.profile import from_cached_profile

class FakeDB:
    def __init__(self):
        self.events = {}; self.articles = {}; self.profiles = {}; self.rows = {}; self.saved = None
        self.next_id = 1; self.fail_save = False; self.fail_mark = False; self.calls = []
    def add(self, n, *, long=False):
        dt = datetime(2026,1,1) + timedelta(days=n)
        self.events[n] = EventRow(n,84,'AI 부정행위',f'Event {n}',dt,dt,None)
        self.articles[n] = [ArticleRow(n,f'Article {n}','press',dt,'https://example.invalid','body')]
        if long:
            self.articles[n].append(ArticleRow(n+100,'late','press',dt+timedelta(days=31),'https://example.invalid','body'))
        self.profiles[n] = {'event_id': n, 'event_summary': f'Summary {n}', 'anchors': [], 'input_hash': None, 'prompt_version': None}
    @contextmanager
    def transaction(self):
        backup=deepcopy((self.events,self.profiles,self.rows,self.saved,self.next_id))
        try: yield
        except BaseException:
            self.events,self.profiles,self.rows,self.saved,self.next_id=backup
            raise
    def topic_events(self,c,t): return list(self.events.values())
    def pending(self,c,**kw): return [e for e in self.events.values() if e.subtopic_processed_at is None]
    def insert(self,c,t,item):
        n=self.next_id; self.next_id+=1
        self.rows[n]={'id':n,'topic_id':t,'summary':None,'event_ids':[],**{k:item[k] for k in ('name','type','common_basis','membership_criterion')}}
        return n
    def update(self,c,n,t,item):
        self.rows[n].update({k:item[k] for k in ('name','type','common_basis','membership_criterion')})
    def sync(self,c,*,topic_id,db_subtopic_id,event_ids): self.rows[db_subtopic_id]['event_ids']=sorted(event_ids)
    def save(self,c,t,state,profiles):
        self.calls.append('save')
        if self.fail_save: raise RuntimeError('simulated save failure')
        self.saved=deepcopy({'state_version':1,'core_state':state,'runtime_profiles':profiles})
    def mark(self,c,ids):
        self.calls.append('mark')
        for n in ids: self.events[n]=replace(self.events[n],subtopic_processed_at=datetime(2026,2,1))
        if self.fail_mark: raise RuntimeError('simulated mark failure')
    def clear(self,c,ids):
        for n in ids: self.events[n]=replace(self.events[n],subtopic_processed_at=None)
    def patches(self,stack):
        methods={
            'validate_schema':lambda *a,**k:None, 'fetch_topic_events':self.topic_events,
            'fetch_pending_events':self.pending,'fetch_event':lambda c,n:self.events.get(n),
            'fetch_profile_events':lambda c,**k:list(self.events.values()),
            'fetch_event_articles':lambda c,n:self.articles[n],
            'fetch_event_profile':lambda c,n:deepcopy(self.profiles.get(n)),
            'fetch_topic_subtopics':lambda c,t:deepcopy(list(self.rows.values())),
            'fetch_classifier_state':lambda c,t:deepcopy(self.saved),
            'save_classifier_state':self.save,'mark_events_processed':self.mark,'clear_events_processed':self.clear,
            'try_topic_advisory_lock':lambda *a:True,'release_topic_advisory_lock':lambda *a:None,
            'lock_event':lambda *a:None,'lock_topic':lambda *a:None,
            'insert_subtopic':self.insert,'update_subtopic':self.update,'sync_memberships':self.sync,
            'delete_subtopic':lambda c,n,t:self.rows.pop(n),
            'fetch_topics_with_missing_summaries':lambda *a,**k:[],
            'fetch_subtopics_needing_summary':lambda *a:[],
            'invalidate_event_subtopic_summaries':lambda *a:None,
            'upsert_event_profile':lambda c,p:self.profiles.update({p.event_id:{'event_summary':p.event_summary,'anchors':p.anchors}}),
        }
        for name,fn in methods.items(): stack.enter_context(patch.object(svc.repo,name,side_effect=fn))

class MockEngine:
    def __init__(self): self.received=[]
    def process_one(self,*,topic_name,state,profiles,current_event_id):
        self.received.append((deepcopy(state),deepcopy(profiles),current_event_id))
        s=deepcopy(state); s['processed_event_ids'].append(str(current_event_id)); s['last_processed_event_id']=str(current_event_id)
        s['last_processed_step']+=1
        if s['last_processed_step']==7:
            s['subtopics']=[{'subtopic_id':'ST001','name':'Test issue','type':'PROCESS','common_basis':'shared','membership_criterion':'same','event_ids':['1','7'],'created_at_step':7}]
            s['next_subtopic_number']=2; s['last_consolidated_step']=7; s['consolidation_count']=1
            s['retired_subtopics']=[{'subtopic_id':'ST000','reason':'test'}]
        if s['last_processed_step']==8:
            s['subtopics'][0]['event_ids'].append(str(current_event_id))
        return s

class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.db=FakeDB(); self.stack=ExitStack(); self.addCleanup(self.stack.close); self.db.patches(self.stack)
    def service(self,dry=False):
        service=svc.SubtopicBatchService(self.db,env_file=Path('.env'),dry_run=dry)
        service.engine=MockEngine(); return service
    def add(self, n):
        for i in range(1,n+1): self.db.add(i)
    def run7(self):
        self.add(7); a=self.service(); result=a.run(topic_id=84)
        self.assertEqual(result['failures'],[]); self.assertEqual(result['processed_event_count'],7)
        return a
    def test_six_then_seven_then_restart(self):
        self.add(6); a=self.service(); self.assertEqual(a.run(topic_id=84)['processed_event_count'],0); self.assertIsNone(self.db.saved)
        self.db.add(7); self.assertEqual(a.run(topic_id=84)['processed_event_count'],7)
        snapshot=deepcopy(self.db.saved); db_id=next(iter(self.db.rows))
        self.db.add(8); b=self.service(); r=b.run(topic_id=84)
        self.assertEqual(r['failures'],[]); self.assertEqual(r['processed_event_count'],1)
        self.assertEqual(b.engine.received[0][0],snapshot['core_state'])
        self.assertEqual(b.engine.received[0][1][:-1],snapshot['runtime_profiles'])
        self.assertEqual(list(self.db.rows),[db_id]); self.assertEqual(self.db.rows[db_id]['event_ids'],[1,7,8])
        self.assertEqual(self.db.saved['core_state']['last_consolidated_step'],7)
        self.assertEqual(self.db.saved['core_state']['retired_subtopics'],snapshot['core_state']['retired_subtopics'])
        self.assertEqual(self.service().run(topic_id=84)['processed_event_count'],0)
    def test_save_failure_rolls_back_public(self):
        self.run7(); old=deepcopy((self.db.saved,self.db.rows)); self.db.add(8); self.db.fail_save=True
        r=self.service().run(topic_id=84); self.assertTrue(r['failures']); self.assertEqual((self.db.saved,self.db.rows),old); self.assertIsNone(self.db.events[8].subtopic_processed_at)
    def test_marker_failure_rolls_back_state(self):
        self.run7(); old=deepcopy((self.db.saved,self.db.rows)); self.db.add(8); self.db.fail_mark=True
        r=self.service().run(topic_id=84); self.assertTrue(r['failures']); self.assertEqual((self.db.saved,self.db.rows),old); self.assertIsNone(self.db.events[8].subtopic_processed_at); self.assertEqual(self.db.calls[-2:],['save','mark'])
    def test_legacy_without_state_fails_closed(self):
        self.run7(); self.db.saved=None; self.db.add(8); r=self.service().run(topic_id=84)
        self.assertEqual(r['failures'][0]['error_type'],'ClassifierStateUnavailableError'); self.assertIsNone(self.db.events[8].subtopic_processed_at)
    def test_membership_drift_fails_closed(self):
        self.run7(); self.db.rows[1]['event_ids']=[1]; self.db.add(8); r=self.service().run(topic_id=84)
        self.assertEqual(r['failures'][0]['error_type'],'ClassifierStateUnavailableError'); self.assertIsNone(self.db.events[8].subtopic_processed_at)
    def test_changed_old_articles_do_not_replay(self):
        self.run7(); old=deepcopy(self.db.saved['runtime_profiles']); self.db.articles[1]=[replace(self.db.articles[1][0],published_at=datetime(2025,1,1))]
        self.db.add(8); b=self.service(); self.assertEqual(b.run(topic_id=84)['processed_event_count'],1); self.assertEqual(b.engine.received[0][1][:-1],old)
    def test_profile_drift_fails_closed(self):
        self.run7(); self.db.profiles[1]['event_summary']='changed'; self.db.add(8); r=self.service().run(topic_id=84)
        self.assertEqual(r['failures'][0]['error_type'],'ClassifierStateUnavailableError')
    def test_missing_profile_fails_closed(self):
        self.run7(); del self.db.profiles[1]; self.db.add(8); r=self.service().run(topic_id=84)
        self.assertEqual(r['failures'][0]['error_type'],'ProcessedProfileMissingError')
    def test_excluded_not_processed_or_saved(self):
        self.add(7); self.db.add(8,long=True); r=self.service().run(topic_id=84)
        self.assertEqual(r['excluded_event_count'],1); self.assertIsNone(self.db.events[8].subtopic_processed_at); self.assertNotIn('8',self.db.saved['core_state']['processed_event_ids'])
    def test_dry_run_no_persistence(self):
        self.add(7); r=self.service(True).run(topic_id=84); self.assertEqual(r['processed_event_count'],7); self.assertIsNone(self.db.saved); self.assertEqual(self.db.rows,{}); self.assertTrue(all(e.subtopic_processed_at is None for e in self.db.events.values()))
    def test_explicit_replay_creates_checkpoint(self):
        self.run7(); self.db.saved=None; self.db.add(8); r=self.service().run(topic_id=84,replay_topic=True)
        self.assertEqual(r['failures'],[]); self.assertEqual(r['rebuilt_topic_count'],1); self.assertEqual(len(self.db.saved['core_state']['processed_event_ids']),8)
    def test_topic_rename_preserves_checkpoint(self):
        self.run7(); self.db.add(8); self.db.events={n:replace(e,topic_name='renamed') for n,e in self.db.events.items()}
        r=self.service().run(topic_id=84); self.assertEqual(r['failures'],[]); self.assertEqual(self.db.saved['core_state']['topic_name'],'renamed')
    def test_profile_only_without_state(self):
        self.add(3); r=self.service().generate_profiles_only(topic_id=84); self.assertEqual(r['reused_profile_count'],3); self.assertIsNone(self.db.saved); self.assertTrue(all(e.subtopic_processed_at is None for e in self.db.events.values()))

    def test_import_exact_legacy_checkpoint_no_public_changes(self):
        self.run7(); original=deepcopy(self.db.saved); self.db.saved=None
        public=deepcopy((self.db.events,self.db.rows,self.db.profiles))
        dry=self.service(True).import_checkpoint(84,original)
        self.assertEqual(dry['processed_event_count'],7); self.assertIsNone(self.db.saved)
        self.service().import_checkpoint(84,original)
        self.assertEqual(self.db.saved,original); self.assertEqual((self.db.events,self.db.rows,self.db.profiles),public)
    def test_import_mismatched_checkpoint_rejected(self):
        self.run7(); original=deepcopy(self.db.saved); self.db.saved=None
        original['core_state']['subtopics'][0]['name']='wrong'
        with self.assertRaises(svc.ClassifierStateUnavailableError):
            self.service().import_checkpoint(84,original)
        self.assertIsNone(self.db.saved)
    def test_import_existing_checkpoint_rejected(self):
        self.run7()
        with self.assertRaises(svc.ClassifierStateUnavailableError):
            self.service().import_checkpoint(84,deepcopy(self.db.saved))

if __name__=='__main__': unittest.main()
