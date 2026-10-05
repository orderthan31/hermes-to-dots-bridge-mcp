"""Persistent error metadata must not erase active review findings."""
import json
import tempfile
import unittest
from pathlib import Path
from hyemi_contracts import normalize_executor_result, owner_id
from hyemi_review import failure as executor_failure
from hyemi_store import WorkflowStore
from hyemi_workers import normalize_review
from test_review_executor import good
from test_workflow_store import request


class BackendFailureLedgerTests(unittest.TestCase):
    def test_actual_worker_error_adapter_preserves_registry_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=WorkflowStore(Path(tmp)/'ledger.db'); owner=owner_id('fixture','owner')
            first=request('review',criteria='산술 정확성',contents='1+1=3')
            store.admit(owner,first)
            raw=good(); raw['completion_signal']=False
            store.finish(first['event_id'],normalize_executor_result(raw,'review'))
            second=request('review',work_id=first['work_id'],revision=2,
                           previous_event_id=first['event_id'],contents='1+1=2',criteria='산술 정확성')
            store.admit(owner,second)
            failed=normalize_review(executor_failure('backend_failed'))
            self.assertEqual(failed['hyemi_result']['review_metadata']['error']['code'],'backend_failed')
            store.finish(second['event_id'],failed,telegram_phase='final')
            stored=store.result(owner,second['event_id'])
            self.assertEqual(stored['outcome'],'unverified'); self.assertFalse(stored['completion_signal'])
            third=request('review',work_id=first['work_id'],revision=3,
                          previous_event_id=second['event_id'],contents='1+1=2',criteria='산술 정확성')
            store.admit(owner,third)
            history=store.review_history(owner,first['work_id'],third['event_id'])
            self.assertEqual(history[-1]['known_finding_ids'],['F1'])
            self.assertEqual(history[-1]['active_finding_ids'],['F1'])
            self.assertEqual(history[-1]['hyemi_result']['review_metadata']['error']['code'],'backend_failed')


if __name__=='__main__': unittest.main()
