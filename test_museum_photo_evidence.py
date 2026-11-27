import json
from hashlib import sha256
from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

import museum_photo_evidence as museum
from listing_review import export_review, hold_seo_preview
from test_mobile import m


class MuseumPhotoEvidenceTests(unittest.TestCase):
    def test_verified_asset_reaches_review_without_fabricating_specimen_measurements(self):
        picture = BytesIO()
        Image.new('RGB', (64, 64), '#16166e').save(picture, 'JPEG')
        raw = picture.getvalue()
        digest = sha256(raw).hexdigest()
        accession = '1980.123'
        citation = f'https://www.clevelandart.org/art/{accession}'
        image = f'https://{museum.IMAGE_HOST}/{accession}/test_web.jpg'
        official_response = {'data': [{'accession_number': accession, 'share_license_status': 'CC0',
                              'title': 'Lacquered document box with birds', 'type': 'Document box',
                              'technique': 'Wood and lacquer',
                              'creation_date': '1880', 'measurements': 'Lid: 23 cm',
                              'images': {'web': {'url': image}}}]}
        candidate = {'url': citation, 'type': 'document box', 'photos': [1]}
        market = 'WEB_TOOL_SOURCES_JSON ' + json.dumps({'urls': [citation]})
        identity = 'REFERENCE_OBJECT_JSON ' + json.dumps(candidate)
        analysis = '\n'.join((
            'SCOPE_JSON {"scope":"ONE_PHYSICAL_ITEM","confidence":99,"physical_item_count":1}',
            'PRODUCT_OBSERVATIONS_JSON {"fields":[{"field":"type","value":"Serving tray",'
            '"photos":[1],"basis":"Early silhouette classification","confidence":96}]}',
        ))
        museum.verified_record.cache_clear()
        self.addCleanup(museum.verified_record.cache_clear)
        with (patch.object(museum, '_read', side_effect=[json.dumps(official_response).encode(), raw]) as fetch,
              patch.dict(m.os.environ, {'VINTAGE_VERIFY_PUBLIC_CATALOGUE': '1'})):
            to_review = lambda photos, sources: export_review(m._product_evidence_for_review(
                analysis, identity, sources, photos, {}, {})[0])
            items = [{'mobile_original_sha256': digest}]
            self.assertEqual(to_review(items, '')['type'], 'Serving tray')
            fetch.assert_not_called()
            reviewed = to_review(items, market)
            self.assertEqual(reviewed['type'], 'document box')
            self.assertEqual(reviewed['identity_review']['previous'], ['Serving tray'])
            self.assertEqual(reviewed['fields']['research_note'][1]['value'], 'Lid: 23 cm')
            self.assertIsNone(reviewed['measurements']['width_cm']['actual'])
            draft = hold_seo_preview(reviewed)
            self.assertEqual(draft['item_specifics']['Type'], 'document box')
            self.assertIn('Documented catalogue material: Wood and lacquer.', draft['description'])
            self.assertEqual(to_review([{'mobile_original_sha256': 'different-photo'}], market)['type'], 'Serving tray')
            bird = museum.museum_claims(candidate | {'type': 'birds'}, [citation], {1: digest})
            self.assertFalse(any(claim['field'] == 'type' for claim in bird))
            self.assertEqual(fetch.call_count, 2)

    def test_rejects_untrusted_domains_and_paths(self):
        for url in ('http://www.clevelandart.org/art/1980.123',
                    'https://www.clevelandart.org@other.example/art/1980.123',
                    'https://www.clevelandart.org/art/1980.123?redirect=x',
                    'https://openaccess-api.clevelandart.org/api/artworks/111/../../unsafe',
                    'https://[broken'):
            self.assertIsNone(museum.record_key(url))
