"""Exercise 35 real photos with an explicitly simulated Responses transport."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

import requests

from ai_pilot import _public_catalogue_url
from blind_live_pilot import API
import multiview_acceptance as m12
from multiview_review import FIELDS, available_fields, grade, review
from s1_acceptance import StopRun, response_issue
from test_blind_live_pilot import response
from test_mobile import m
from test_s1_acceptance import RESEARCH, VISUAL, reply
from visual_protocol import VISUAL_TEXT


class MultiviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.plan = m12.frozen_plan(m12.MANIFEST, m12.ARCHIVE)
        self.phase = self.plan['stages'][m12.STAGE]

    def test_blind_package_and_references_are_separate_and_approved_limit_is_exact(self):
        lots = m12.blind_inputs(m12.ARCHIVE, self.phase, self.root)
        self.assertEqual(len(lots), 12)
        self.assertEqual(sum(map(len, lots.values())), 35)
        coverage = Counter(field for row in available_fields(self.plan).values() for field in row)
        self.assertEqual(dict(coverage), dict(zip(FIELDS, (12, 12, 2, 2, 10, 10, 11, 1))))
        self.assertEqual(m12.CAP, Decimal('1.76340'))
        package = self.root / 'chatgpt.zip'
        m12.chatgpt_package(lots, package)
        with ZipFile(package) as prepared:
            self.assertEqual(set(prepared.namelist()), {photo.name for frames in lots.values() for photo in frames}
                             | {'CHATGPT-PROMPT.txt'})
            self.assertTrue(all(prepared.read(photo.name) == photo.read_bytes() for frames in lots.values() for photo in frames))
        changed = self.root / 'changed.zip'
        changed.write_bytes(m12.ARCHIVE.read_bytes() + b'changed')
        with self.assertRaisesRegex(StopRun, 'FROZEN_PHOTOS_CHANGED'):
            m12.frozen_plan(m12.MANIFEST, changed)
        with ZipFile(changed, 'w') as z:
            z.writestr('answer-key.json', '[]')
        with self.assertRaisesRegex(StopRun, 'UNEXPECTED_ARCHIVE_ENTRIES'):
            m12.blind_inputs(changed, self.phase, self.root)

    def test_paid_path_cannot_authorize_without_all_protections(self):
        argv = ['multiview_acceptance.py', '--execute', str(self.root / 'paid-output')]
        env = {'GITHUB_REF': m12.APPROVAL_REF, 'GITHUB_RUN_ATTEMPT': '1', 'ENABLE_LIVE_PUBLISH': '0',
               'LOTNEXO_M12_RESERVED': m12.PLAN_SHA, 'LOTNEXO_M12_APPROVED_USD': str(m12.CAP)}
        for altered in ({}, env | {'GITHUB_RUN_ATTEMPT': '2'},
                        env | {'LOTNEXO_M12_APPROVED_USD': '1.76339'},
                        env | {'LOTNEXO_M12_RESERVED': 'other'}, env | {'ENABLE_LIVE_PUBLISH': '1'}):
            with (patch.dict('os.environ', altered, clear=True), patch('sys.argv', argv),
                  patch('builtins.print'), patch('multiview_acceptance.authorize') as auth):
                self.assertEqual(m12.main(), 1)
                auth.assert_not_called()
        preflight = self.root / 'preflight.json'
        with (patch.dict('os.environ', {'GITHUB_REF': m12.APPROVAL_REF, 'GITHUB_RUN_ATTEMPT': '1',
                                       'P094_PILOT_OPENAI_API_KEY': 'fixture-only'}, clear=True),
              patch('sys.argv', ['multiview_acceptance.py', '--preflight', str(preflight)]),
              patch('multiview_acceptance.authorize') as auth, patch('multiview_acceptance.execute') as run):
            self.assertEqual(m12.main(), 0)
            auth.assert_called_once_with('fixture-only', preflight)
            run.assert_not_called()

    def test_missing_or_duplicate_views_are_rejected(self):
        budget = m12.MultiviewBudget(self.plan, self.root / 'missing')
        with self.assertRaisesRegex(StopRun, 'PHOTO_PAYLOAD_COUNT_MISMATCH'):
            budget.bind('MV-201', {'text': VISUAL_TEXT, 'input': [{'content': [{'type': 'input_image'}]}]})
        with patch.dict('sys.modules', {'app': m}):
            self.assertEqual(response_issue(reply(), 'photos', {}, photo_count=3), 'INVALID_VISUAL_SCHEMA')
            duplicate = VISUAL | {'photos': [VISUAL['photos'][0] for _ in range(3)]}
            self.assertEqual(response_issue(reply(output_text=json.dumps(duplicate)), 'photos', {}, photo_count=3),
                             'INVALID_VISUAL_SCHEMA')

    def test_full_blind_cohort_with_synthetic_provider(self):
        lots = m12.blind_inputs(m12.ARCHIVE, self.phase, self.root)
        wire_counts = []
        requests_sent = []

        def provider(session, method, url, **kwargs):
            self.assertEqual(method, 'POST')
            body = kwargs['json']
            text = '\n'.join(item['text'] for msg in body['input'] for item in msg['content']
                             if item['type'] == 'input_text')
            self.assertNotIn(self.plan['reference_sha256'], text)
            self.assertNotIn('clevelandart.org', text)
            if url == f'{API}/responses/input_tokens':
                wire_counts.append(body)
                return response({'input_tokens': 2000})
            self.assertEqual(url, f'{API}/responses')
            self.assertEqual(wire_counts[-1], {key: body[key] for key in wire_counts[-1]})
            self.assertFalse(kwargs['allow_redirects'])
            requests_sent.append(body)
            if body.get('tools'):
                self.assertEqual(body['max_tool_calls'], 2)
                return response(reply(output=[{'type': 'web_search_call', 'action': {'sources': []}}],
                                      output_text=RESEARCH))
            count = sum(item['type'] == 'input_image' for msg in body['input'] for item in msg['content'])
            visual = VISUAL | {'photos': [VISUAL['photos'][0] | {'photo': index} for index in range(1, count+1)]}
            return response(reply(output_text=json.dumps(visual)))

        output = self.root / 'simulated'
        with (patch.object(requests.Session, 'request', side_effect=provider),
              patch.object(m, 'OPENAI_API_KEY', 'fixture-only'), patch.dict('sys.modules', {'app': m}),
              patch.multiple(m, EBAY_CLIENT_ID=None, EBAY_CLIENT_SECRET=None, EBAY_REFRESH_TOKEN=None)):
            m12.execute(self.plan, lots, output)
        self.assertEqual((len(wire_counts), len(requests_sent)), (24, 24))
        ledger = json.loads((output / 'run.json').read_text())
        self.assertEqual(ledger['stage'], 'M12')
        self.assertEqual(ledger['status'], 'AWAIT_INDEPENDENT_REVIEW')
        self.assertEqual(sum(Decimal(item['reserved_usd']) for item in ledger['attempts']), m12.CAP)
        self.assertEqual(ledger['unsettled_reserve_usd'], '0')
        report = review(self.plan, output)
        self.assertEqual(report['fields']['identity']['pending'], 12)
        self.assertEqual(report['fields']['identity']['filled'], 12)
        self.assertFalse(report['pilot_identity_checkpoint_met'])
        self.assertFalse(report['admitted'])

    def test_review_distinguishes_filling_accuracy_and_unknowns(self):
        data = available_fields(self.plan)
        results = {'MV-201': {'identity': 'reported', 'model': 'asserted without reference'},
                   'MV-202': {'identity': 'unconfirmed guess'}}
        judgments = {'MV-201': {'identity': {'verdict': 'CORRECT', 'evidence': 'fixture verification'}},
                     'MV-202': {'identity': {'verdict': 'UNCONFIRMED', 'evidence': 'no independent match'}}}
        stats = grade(data, results, judgments)['fields']
        self.assertEqual(stats['identity']['filled'], 2)
        self.assertEqual(stats['identity']['unknown'], 10)
        self.assertEqual(stats['identity']['accuracy_of_answers'], .5)
        self.assertEqual(stats['identity']['confirmed_completeness'], 1/12)
        self.assertEqual(stats['model']['asserted_without_reference'], 1)
        with self.assertRaises(ValueError):
            grade(data, results, {'MV-201': {'model': judgments['MV-201']['identity']}})

    def test_only_the_public_catalogue_paths_bypass_the_paid_budget(self):
        self.assertTrue(_public_catalogue_url('https://openaccess-api.clevelandart.org/api/artworks/142716'))
        self.assertTrue(_public_catalogue_url('https://openaccess-api.clevelandart.org/api/artworks/?accession_number=1930.232&limit=2'))
        self.assertTrue(_public_catalogue_url('https://openaccess-cdn.clevelandart.org/1930.232/test_web.jpg'))
        for url in ('https://other.example/api/artworks/3', 'https://openaccess-api.clevelandart.org/',
                    'https://openaccess-api.clevelandart.org/api/artworks/?accession_number=1930.232&limit=20',
                    'https://openaccess-cdn.clevelandart.org/1930.232/test_web.jpg?query=anything'):
            self.assertFalse(_public_catalogue_url(url))
