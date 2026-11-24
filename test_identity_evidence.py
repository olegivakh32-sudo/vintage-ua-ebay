import json
import unittest

from test_mobile import m


class IdentityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.urls = ['https://catalog.example/catalog/a', 'https://museum.example/collection/b']
        self.sources = [
            {'url': url, 'kind': 'MUSEUM', 'document_id': str(i), 'match': 'EXACT', 'excerpt': 'Label M-1.'}
            for i, url in enumerate(self.urls)
        ]
        self.claim = {'field': 'model', 'value': 'Camera M-1', 'photos': [1], 'sources': self.sources}

    def consensus(self, claims, urls=None):
        identity = 'IDENTITY_CLAIMS_JSON ' + json.dumps({'claims': claims})
        market = ('AUTO_SPECS_JSON {"exact_model_confirmed":true,"confidence":96,"match_count":2,"model":"Camera M-1"}'
                  '\nWEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': self.urls if urls is None else urls}))
        return m._consensus_specs_from_research(identity, market, photo_count=3)

    def test_research_requires_independent_observed_links_and_valid_photo_anchor(self):
        eligible = self.consensus([self.claim])['identity_claims'][0]
        self.assertEqual(eligible['status'], 'CORROBORATED_REFERENCE')
        for claim, urls in (
            (self.claim, self.urls[:1]),
            ({**self.claim, 'photos': [4, -1, '1']}, self.urls),
            ({**self.claim, 'sources': [self.sources[0], {**self.sources[1], 'document_id': '0'}]}, self.urls),
        ):
            self.assertEqual(self.consensus([claim], urls)['identity_claims'][0]['status'], 'HYPOTHESIS')

    def test_conflicting_claims_cannot_enter_attributed_description(self):
        claims = [self.claim, {**self.claim, 'value': 'Camera M-2'}]
        specs = self.consensus(claims)
        self.assertEqual({record['status'] for record in specs['identity_claims']}, {'CONTESTED'})
        draft = {'title': 'Film camera', 'physical_item_count': 1, 'seller_verified_marking': 'M-1'}
        self.assertFalse(m._p089_research_handoff(draft, specs, {}))
        self.assertNotIn('description', draft)
        self.assertTrue(m._p089_research_handoff(draft, self.consensus([self.claim]), {}))
        self.assertIn('Reference identification: Camera M-1', draft['description'])

    def test_quality_mark_is_retained_as_ocr_without_promoting_it_to_maker(self):
        facts = m._auto_facts_from_analysis('AUTO_FACTS_JSON: {"materials":12,"brand":"XYZ",'
                                            '"markings":[{"photo":1,"literal":"XYZ","language":"uk","kind":"QUALITY_MARK"}]}')
        self.assertEqual(facts['materials'], [])
        self.assertEqual(facts['brand'], '')
        self.assertEqual(facts['markings'][0]['literal'], 'XYZ')
        self.assertEqual(facts['markings'][0]['kind'], 'QUALITY_MARK')
