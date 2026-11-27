"""Replay the completed paid pilot locally through the actual draft and HOLD routes."""
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import requests

from ai_pilot import PilotLimits, run
from blind_live_pilot import API, CAP, LiveBudget, frozen_inputs, prediction, read_paid_checkpoint
from listing_review import canonical_product_type, export_review, hold_seo_preview
from market_evidence import completed_sale_valuation
from recognition_benchmark import score
from test_blind_live_pilot import RECOGNITION, response
from test_mobile import m


def saved_replay(root):
    prior = read_paid_checkpoint(RECOGNITION / 'lotnexo-paid-45888499679.zip')
    archive = RECOGNITION / 'lotnexo-paid-45955681812.zip'
    assert sha256(archive.read_bytes()).hexdigest() == 'e1e42967a129c051133e5d4930615fb51d077abe3c9c253a33daef0dc0b2cf8e'
    with ZipFile(archive) as package:
        ledger = json.loads(package.read('results/run.json'))
        stages = [row for row in ledger['attempts'] if row['sent']]
        replay = [reply for row in stages for reply in (
            response({'input_tokens': row['preflight_input_tokens']}),
            response(json.loads(package.read('results/' + row['response_file']))),
        )]
    photos = root / 'photos'
    photos.mkdir()
    lots = frozen_inputs(RECOGNITION / 'lotnexo-live-pilot-blind.zip', photos)
    budget = LiveBudget(root / 'replay.json', checkpoint=prior)
    held = []
    capture = m._p058_capture_current

    def save_and_read(reason):
        record = capture(reason)
        proof = json.loads(m._p058_row(record)['data'])
        draft = proof['snapshot'].get('draft') or {}
        assert draft.get('hold_preview') and not proof['price']
        assert m._p087_quality_block(draft) and m._p083_compliance(draft)['status'] == 'REVIEW'
        with m.app.test_client() as client:
            picker = draft['product_export']
            with client.session_transaction() as session:
                session['picker_session_id'] = proof['picker_id']
                session['current_lot_number'] = proof['lot_number']
                session['hold_csrf'] = 'fixture'
            document = client.get('/picker/hold-draft.json')
            assert document.status_code == 200 and document.json['product_export'] == picker
            with patch.dict(os.environ, {'ENABLE_LIVE_PUBLISH': '1'}):
                for route in ('/picker/confirm-final-draft', '/ebay/current-setup', '/ebay/current-publish'):
                    assert client.post(route, data={'hold_csrf': 'fixture'}).status_code == 409
        held.append({'source_sha256': [photo['mobile_original_sha256'] for photo in proof['snapshot']['photos']],
                     'stored_draft': draft, 'open_after_cache_clear': False})
        m.LOT002_DRAFT_CACHE.pop(proof['picker_id'])
        m._p058_open_record(record)
        assert m.LOT002_DRAFT_CACHE[proof['picker_id']] == draft
        held[-1]['open_after_cache_clear'] = True
        return record

    with (patch.object(requests.Session, 'request', side_effect=replay) as transport,
          patch.object(m, 'OPENAI_API_KEY', 'offline-fixture'), patch.dict('sys.modules', {'app': m}),
          patch.multiple(m, EBAY_CLIENT_ID=None, EBAY_CLIENT_SECRET=None, EBAY_REFRESH_TOKEN=None),
          patch.object(m, '_p058_capture_current', side_effect=save_and_read)):
        results = [run({'lot': lot}, frames, PilotLimits(CAP), live_web=budget) for lot, frames in lots.items()]
    assert len(held) == len(results) == 3 and transport.call_count == 10
    assert [call.args[2] for call in transport.call_args_list] == [f'{API}/responses/input_tokens', f'{API}/responses'] * 5
    predictions = [prediction(dict(row, elapsed_ms=None), budget.attempts) for row in results]
    frozen = json.loads((RECOGNITION / 'lotnexo-live-pilot-sources.json').read_text())
    for result, proof in zip(results, held):
        assert proof['source_sha256'] == [sha256(photo.read_bytes()).hexdigest() for photo in lots[result['lot']]]
        assert result['stage_http'] == {'visual': 200, 'research': 200, 'draft': 200}
    return predictions, held, score(frozen, predictions, allow_partial_categories=True)


class LotnexoReviewTests(unittest.TestCase):
    def test_archived_three_lots_use_real_hold_export_and_remain_blocked(self):
        with TemporaryDirectory() as temporary:
            rows, held, result = saved_replay(Path(temporary))
        self.assertEqual([row['identity'].casefold() for row in rows],
                         ['plate', 'single-board computer', 'video game controller'])
        self.assertEqual(result['fields']['identity'], {'correct': 3, 'labelled': 3})
        self.assertEqual(result['admitted'], 0)
        self.assertTrue(all(row['stored_draft']['category_review']['status'] == 'UNVERIFIED' for row in held))
        self.assertTrue(all(row['stored_draft']['market_valuation']['price'] is None for row in held))
        self.assertEqual(rows[0]['product_export']['measurements']['width_cm']['reference'][0]['value'], 30.5)
        self.assertIsNone(rows[0]['product_export']['measurements']['width_cm']['actual'])
        self.assertIn('FCC ID: 2ABCB-RPI4B', rows[1]['ocr'])
        self.assertIn('20953-RPI4B', rows[1]['ocr'])
        self.assertIn('FCC ID: 2ABCB-RP14B', rows[1]['ocr_raw'])
        self.assertEqual(rows[2]['model'], 'DOL-003')
        drafts = [row['stored_draft'] for row in held]
        self.assertEqual([draft['title'] for draft in drafts], [
            'Pierced Salt-Glazed Stoneware Plate',
            'Raspberry Pi 4 Model B Single-board computer',
            'Nintendo GAMECUBE DOL-003 Controller',
        ])
        self.assertTrue(all(len(draft['title']) <= 80 and 'before listing' not in draft['description']
                            for draft in drafts))

    def test_photo_title_refinements_require_consistent_evidence_on_other_products(self):
        def entry(value, status='PHOTO_OBSERVATION'):
            return {'value': value, 'status': status, 'photos': [1]}

        camera = {'type': [entry('Film camera')], 'brand': [entry('Northstar')], 'model': [entry('M-42')],
                  'marking': [entry('Northstar Vision Camera')]}
        self.assertEqual(hold_seo_preview(export_review(camera))['title'], 'Northstar Vision M-42 Camera')
        camera['marking'].append(entry('Northstar Compact Camera'))
        self.assertEqual(hold_seo_preview(export_review(camera))['title'], 'Northstar M-42 Film camera')
        stand = {'type': [entry('Folding display stand')], 'feature': [entry('Folding hinges', 'PHOTO_CANDIDATE')]}
        self.assertEqual(hold_seo_preview(export_review(stand, 'Display stand'))['title'], 'Display stand')
        stand['feature'][0]['status'] = 'PHOTO_OBSERVATION'
        self.assertEqual(hold_seo_preview(export_review(stand, 'Display stand'))['title'], 'Folding Display stand')

    def test_normalization_uses_observation_and_preserves_scope_ambiguity(self):
        evidence = {'type': [{'value': 'Drill or impact driver with case', 'status': 'PHOTO_OBSERVATION'}]}
        self.assertEqual(canonical_product_type(evidence), '')
        self.assertEqual(canonical_product_type(evidence, 'Drill'), '')
        evidence['type'][0]['value'] = 'Film camera with lens and leather case'
        self.assertEqual(export_review(evidence, 'Camera case')['type'], 'Film camera')

    def test_hold_taxonomy_checks_full_hierarchy_and_required_aspects_via_read_only_api(self):
        def lookup(method, url, **kwargs):
            self.assertEqual(method, 'GET')
            if url.endswith('/get_default_category_tree_id'):
                return response({'categoryTreeId': '0', 'categoryTreeVersion': '101'})
            if url.endswith('/get_category_suggestions'):
                nodes = [
                    {'category': {'categoryId': identifier, 'categoryName': 'Controllers & Attachments'},
                     'categoryTreeNodeLevel': 2,
                     'categoryTreeNodeAncestors': [{'categoryName': branch, 'categoryTreeNodeLevel': 1}]}
                    for identifier, branch in [('123', 'Industrial Automation'), ('117042', 'Video Games & Consoles')]
                ]
                return response({'categorySuggestions': nodes})
            self.assertTrue(url.endswith('/get_item_aspects_for_category'))
            self.assertEqual(kwargs['params']['category_id'], '117042')
            return response({'aspects': [{'localizedAspectName': 'Type', 'aspectConstraint': {'aspectRequired': True}}]})

        draft = {'hold_preview': True, 'title': 'Video game controller', 'item_specifics': {'Type': 'Video game controller'},
                 'product_export': {'type': 'Video game controller'}}
        with (m.app.test_request_context(), patch.object(m, 'get_ebay_access_token', return_value='fixture'),
              patch.object(m, '_lot002_request', side_effect=lookup),
              patch.multiple(m, EBAY_CLIENT_ID='fixture', EBAY_CLIENT_SECRET='fixture', EBAY_REFRESH_TOKEN='fixture')):
            category = m._hold_category_review(draft, '')
        self.assertEqual(category['status'], 'TAXONOMY_CHECKED')
        self.assertEqual(category['id'], '117042')
        self.assertEqual(category['version'], '101')
        self.assertNotIn('category_id', draft)

    def test_valuation_requires_distinct_comparable_completed_sales(self):
        subject = {'identity': 'Manufacturer Model 110', 'condition': 'Used Good', 'completeness': 'Body Only'}
        sale = dict(subject, currency='USD', status='SOLD', verification='MARKETPLACE_COMPLETED_SALE',
                    sold_date='2026-10-26')
        sold = [dict(sale, url=f'https://www.ebay.com/itm/{number}', item_price_usd=price)
                for number, price in [(111000000001, 20), (111000000002, 45), (111000000003, 30)]]
        records = sold + [dict(sold[0], url=sold[0]['url']+'?mkcid=10'),
                          dict(sold[0], status='ACTIVE'), dict(sold[0], completeness='Kit'),
                          dict(sold[0], verification='MODEL_OUTPUT', item_price_usd=999)]
        as_of = date(2026, 11, 27)
        self.assertEqual(completed_sale_valuation(records, subject, as_of=as_of)['price'], '30.00')
        conflicting = dict(sold[0], item_price_usd=21)
        self.assertIsNone(completed_sale_valuation(records + [conflicting], subject, as_of=as_of)['price'])
        self.assertIsNone(completed_sale_valuation(records, dict(subject, condition='unknown'), as_of=as_of)['price'])
