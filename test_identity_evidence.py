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

    def test_claims_without_auto_specs_preserve_citations_and_decisions(self):
        rejected = [
            {**self.sources[1], 'document_id': '0'},
            {**self.sources[1], 'match': 'ANALOG'},
            {**self.sources[1], 'url': 'https://unobserved.example/model'},
        ]
        claims = [
            {**self.claim, 'sources': self.sources[:1] + rejected},
            {**self.claim, 'field': 'model', 'value': 'UNKNOWN'},
            {**self.claim, 'field': 'weight_kg', 'value': .35, 'specimen_basis': 'GROSS'},
            {**self.claim, 'field': 'length_cm', 'value': 12, 'specimen_basis': 'ITEM'},
        ]
        identity = 'IDENTITY_CLAIMS_JSON ' + json.dumps({'claims': claims})
        cases = m._consensus_specs_from_research(
            identity + '\nWEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': self.urls[:1]}),
            'WEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': self.urls[1:]}), photo_count=3,
        )
        decisions = cases['reference_decisions']
        self.assertEqual([row['status'] for row in decisions],
                         ['HYPOTHESIS', 'REJECTED', 'REJECTED', 'CORROBORATED_REFERENCE'])
        self.assertEqual([item['reason'] for item in decisions[0]['sources']],
                         ['ACCEPTED_CITATION', 'DUPLICATE_SOURCE', 'NONEXACT_SOURCE', 'URL_NOT_IN_SEARCH_RESULTS'])
        self.assertEqual(decisions[2]['reasons'], ['WRONG_MEASUREMENT_SCOPE'])
        self.assertEqual(cases['identity_claims'][-1]['value'], 12.0)
        html = m._observation_diagnostics_html(decisions)
        self.assertIn('повтор домену або документа', html)
        self.assertIn('ваги нетто', html)
        self.assertNotIn('unobserved.example', html)
        broken = m._consensus_specs_from_research('IDENTITY_CLAIMS_JSON {"claims":', '', photo_count=3)
        self.assertEqual(broken['identity_claims'], [])
        self.assertEqual(broken['reference_decisions'][0]['reasons'], ['INVALID_CLAIMS_BLOCK'])

    def test_analog_sources_use_metadata_on_either_research_block(self):
        sources = [
            dict(url=url, same_variant=True, confidence=94, matched_features=['shape', 'construction', 'material'],
                 length_cm=12, width_cm=8, height_cm=4, weight_basis='NET', weight_kg=.2)
            for url in self.urls
        ]
        identity = ('SPECS_EVIDENCE_JSON ' + json.dumps({'sources': sources})
                    + '\nWEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': self.urls[:1]}))
        market = 'WEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': self.urls[1:]})
        report = m._p054_reference_evidence(identity, market, {})
        self.assertEqual(report['estimates']['weight_kg'], .2)
        self.assertEqual(len(report['sources']), 2)
        self.assertFalse(report['independently_verified'])
        report = m._p054_reference_evidence(identity, 'WEB_TOOL_SOURCES_JSON {"urls":[]}', {})
        self.assertNotIn('weight_kg', report['estimates'])
        self.assertEqual(report['rejected'][0]['reason'], 'URL absent from web search metadata')
        for model in sources:
            model['document_id'] = 'Shared manual'
        mirrored = ('SPECS_EVIDENCE_JSON ' + json.dumps({'sources': sources})
                    + '\nWEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': self.urls[:1]}))
        report = m._p054_reference_evidence(mirrored, market, {})
        self.assertEqual(report['estimates'], {})
        self.assertIn('копія того самого документа', m._p054_evidence_html({'reference_evidence': report}))

    def test_reference_fields_keep_their_subject_and_units(self):
        claims = [
            dict(self.claim, field='manufacturer', value='Example Maker'),
            dict(self.claim, field='materials', value='Possibly bronze'),
            dict(self.claim, field='period/variant', value='1920 portrait series'),
            dict(self.claim, field='weight_g', value=55, specimen_basis='NET'),
            dict(self.claim, field='length', value='12 mm', specimen_basis='ITEM'),
            dict(self.claim, field='width', value='12', specimen_basis='ITEM'),
            dict(self.claim, field='height_cm', value=12, specimen_basis='PACKAGING'),
        ]
        report = self.consensus(claims)
        rows = report['identity_claims']
        self.assertEqual([(row['field'], row['value'], row['status']) for row in rows], [
            ('brand', 'Example Maker', 'CORROBORATED_REFERENCE'),
            ('material', 'Possibly bronze', 'HYPOTHESIS'),
            ('research_note', '1920 portrait series', 'HYPOTHESIS'),
            ('weight_kg', .055, 'CORROBORATED_REFERENCE'),
            ('length_cm', 1.2, 'CORROBORATED_REFERENCE'),
        ])
        self.assertEqual(rows[2]['name'], 'period/variant')
        self.assertEqual(report['reference_decisions'][-2]['reasons'], ['INVALID_MEASUREMENT'])
        self.assertEqual(report['reference_decisions'][-1]['reasons'], ['WRONG_MEASUREMENT_SCOPE'])

    def test_quality_mark_is_retained_as_ocr_without_promoting_it_to_maker(self):
        facts = m._auto_facts_from_analysis('AUTO_FACTS_JSON: {"materials":12,"brand":"XYZ",'
                                            '"markings":[{"photo":1,"literal":"XYZ","language":"uk","kind":"QUALITY_MARK"}]}')
        self.assertEqual(facts['materials'], [])
        self.assertEqual(facts['brand'], '')
        self.assertEqual(facts['markings'][0]['literal'], 'XYZ')
        self.assertEqual(facts['markings'][0]['kind'], 'QUALITY_MARK')
