"""Reprocess the sealed S1 replies with all network transport replaced."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import requests

from s1_acceptance import LotFailure, StageBudget, execute, frozen_plan
from s1_review import align_chatgpt
from sellable_scope import resolve_scope
from test_blind_live_pilot import RECOGNITION, response
from test_mobile import m
from test_s1_acceptance import ALGORITHM, ARCHIVE, PLAN
from acceptance_stage import sha


EVIDENCE = RECOGNITION / 'lotnexo-s1-r2-46011779924-evidence.zip'


def replay(root):
    plan = frozen_plan(PLAN, ARCHIVE, ALGORITHM)
    assert sha(EVIDENCE) == '1c17da2c5ef393a4e14ef2d80d09d7dec39b34f183fd0d9998ad9bd10ec3ee29'
    with ZipFile(EVIDENCE) as source:
        archived = json.loads(source.read('results/run.json'))['attempts']
        recorded = {(row['lot'], row['step']): (
            row['input_tokens_counted'], json.loads(source.read('results/' + row['response_file'])),
        ) for row in archived}
    used, missing = [], []

    class ReplayBudget(StageBudget):
        # Keep the normal application path, but require a recorded response before transport.
        def bind(self, lot, original):
            payload = super().bind(lot, original)
            key = self.pending[:2]
            if key not in recorded:
                missing.append(key)
                raise LotFailure('ARCHIVED_RESPONSE_UNAVAILABLE')
            return payload

        def skip_lot(self, lot, reason):
            if any(name == lot for name, _ in missing):
                reason = 'ARCHIVED_RESPONSE_UNAVAILABLE'
            super().skip_lot(lot, reason)

        def send(self, request, session, kwargs):
            key = self.pending[:2]
            if key in used:
                self.stop('DUPLICATE_OR_OUT_OF_ORDER')
            self.replaying = key
            reply = super().send(request, session, kwargs)
            used.append(key)
            return reply

    output = root / 'replayed'
    budget = ReplayBudget(plan, output)
    calls = []

    def no_network(session, method, url, **kwargs):
        calls.append((method, url))
        assert method == 'POST'
        count, saved = recorded[budget.replaying]
        endpoint = 'https://api.openai.com/v1/responses'
        if url == endpoint + '/input_tokens':
            return response({'input_tokens': count})
        assert url == endpoint
        return response(saved)

    with (patch('s1_acceptance.StageBudget', return_value=budget),
          patch.object(requests.Session, 'request', side_effect=no_network),
          patch.object(m, 'OPENAI_API_KEY', 'offline-fixture'), patch.dict('sys.modules', {'app': m}),
          patch.multiple(m, EBAY_CLIENT_ID=None, EBAY_CLIENT_SECRET=None, EBAY_REFRESH_TOKEN=None)):
        execute(plan, ARCHIVE, output)
    assert used == list(recorded)
    assert len(calls) == 2 * len(recorded)
    return output, missing


class S1ReplayTests(unittest.TestCase):
    def test_archived_batch_keeps_evidence_and_advances_after_missing_response(self):
        with TemporaryDirectory() as directory:
            out, missing = replay(Path(directory))
            self.assertEqual(missing, [('P039', 'research'), ('P053', 'photos')])
            cards = {p.name.split('-')[0]: json.loads(p.read_bytes()) for p in out.glob('*-card.json')}
            self.assertEqual(len(cards), 10)
            self.assertEqual(cards['P039']['sellable_scope']['physical_item_count'], 1)
            self.assertEqual(cards['P039']['review_card']['draft_status'], 'NO_DRAFT')
            self.assertEqual(cards['P053']['review_card']['draft_status'], 'NO_DRAFT')
            self.assertEqual(cards['P001']['review_card']['draft_status'], 'NO_DRAFT')
            self.assertEqual(cards['P004']['review_card']['draft_status'], 'HOLD')
            self.assertEqual(cards['P004']['draft']['item_specifics']['Model'], '33-425')
            self.assertEqual(cards['P004']['draft']['product_export']['model_marking']['visible_text'],
                             'PowerLock 25; 33-425')
            self.assertTrue(cards['P006']['review_card']['evidence']['research_note'])
            self.assertNotIn('1523', cards['P006']['draft']['description'])
            ledger = json.loads((out / 'run.json').read_bytes())
            self.assertEqual(ledger['metered_usd'], '0.0964136')
            self.assertEqual(ledger['unsettled_reserve_usd'], '0')
            self.assertEqual([row['status'] for row in ledger['attempts'][-3:]], ['SKIPPED'] * 3)
            self.assertEqual({row['reason'] for row in ledger['attempts'][-3:]}, {'ARCHIVED_RESPONSE_UNAVAILABLE'})
            self.assertEqual(ledger['status'], 'AWAIT_INDEPENDENT_REVIEW')

    def test_supplied_photo_mapping_is_explicit_and_one_to_one(self):
        photos = {'P001': 'first-sha', 'P002': 'second-sha'}
        rows = [{'lot': 'phone-a', 'admitted': False}, {'lot': 'phone-b', 'admitted': False}]
        mapping = [{'lot': 'P002', 'received_id': 'phone-a', 'photo_sha256': 'second-sha'},
                   {'lot': 'P001', 'received_id': 'phone-b', 'photo_sha256': 'first-sha'}]
        self.assertEqual([row['lot'] for row in align_chatgpt(rows, mapping, photos)], ['P002', 'P001'])
        for invalid in (mapping[:1], [mapping[0], mapping[1] | {'photo_sha256': 'second-sha'}],
                        [mapping[0], mapping[0] | {'received_id': 'phone-b'}]):
            with self.assertRaises(ValueError):
                align_chatgpt(rows, invalid, photos)

    def test_retail_parts_do_not_collapse_independent_or_uncertain_products(self):
        anchor = [{'field': 'type', 'value': 'Film camera with controls', 'confidence': 96, 'photos': [1]}]
        def part(label, role):
            return {'label': label, 'role': role, 'quantity': 1, 'photos': [1]}
        base = {'scope': 'MULTIPLE_PHYSICAL_ITEMS', 'physical_item_count': 3, 'confidence': 94, 'note': ''}
        parts = [part('film camera', 'PRODUCT'), part('cap', 'COMPONENT'), part('carton', 'PACKAGING')]
        self.assertEqual(resolve_scope(base | {'components': parts}, anchor, 1)['physical_item_count'], 1)
        unsure = base | {'components': parts, 'scope': 'UNCERTAIN', 'confidence': 0}
        self.assertEqual(resolve_scope(unsure, anchor, 1)['scope'], 'UNCERTAIN')
        parts[1] = part('independent film camera', 'PRODUCT')
        self.assertEqual(resolve_scope(base | {'components': parts}, anchor, 1)['physical_item_count'], 2)
        parts[1] = part('separate lens', 'UNKNOWN')
        self.assertEqual(resolve_scope(base | {'components': parts}, anchor, 1)['scope'], 'UNCERTAIN')
        legacy = base | {'note': 'Visible film camera, separate camera, and product box.'}
        self.assertEqual(resolve_scope(legacy, anchor, 1)['physical_item_count'], 3)
