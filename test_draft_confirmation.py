from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_mobile import m


class DraftConfirmationTests(unittest.TestCase):
    def setUp(self):
        storage = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(m, 'ACTIVE_LOT_CHECKPOINT_DIR', str(storage / 'checkpoints')))
        self.enterContext(patch.dict(m.os.environ, {'HOLD_QUEUE_DIR': str(storage / 'hold')}))
        self.enterContext(patch.object(
            m.requests.sessions.Session, 'request', side_effect=AssertionError('External network forbidden')
        ))
        self.picker = 'fixture-draft-review'
        self.initial = {
            'title': 'Vintage Portable Film Camera', 'price': '15.00',
            'description': 'A film camera with visible wear on the case.',
            'item_specifics': {'Type': 'Film Camera'},
            'listing_text': '1 TITLE\nVintage Portable Film Camera\n\n2 PRICE USD\n$15.00',
            'confirmed_material': 'Plastic', 'physical_item_count': 1,
        }
        self.enterContext(patch.dict(m.LOT002_DRAFT_CACHE, {self.picker: self.initial.copy()}))
        self.client = m.app.test_client()
        with self.client.session_transaction() as data:
            data.update(picker_session_id=self.picker, current_lot_number=501)

    def review_form(self):
        response = self.client.get('/picker/review-final-draft')
        self.assertEqual(response.status_code, 200)
        markup = response.get_data(as_text=True)
        return {name: re.search(fr'name="{name}" value="([^"]+)"', markup)[1]
                for name in ('hold_csrf', 'draft_digest')}

    @staticmethod
    def save(client, form, price, material='Metal'):
        return client.post('/picker/confirm-final-draft', data={
            **form, 'confirmed_price': price, 'confirmed_material': material,
        })

    def test_replay_acknowledges_current_save_but_conflicting_old_form_keeps_hold_intact(self):
        form = self.review_form()
        self.assertEqual(self.client.post('/ebay/hold-queue/add', data=form).status_code, 303)
        self.assertEqual(self.save(self.client, form, '39,95').status_code, 303)
        with patch.object(m, '_save_active_lot_checkpoint', side_effect=AssertionError('Replay must be read-only')):
            self.assertEqual(self.save(self.client, form, '39.95').status_code, 303)
        self.assertEqual(self.save(self.client, form, '20.00', 'Wood').status_code, 409)
        draft = m.LOT002_DRAFT_CACHE[self.picker]
        self.assertEqual(draft['price'], '39.95')
        self.assertEqual(draft['confirmed_material'], 'Metal')
        self.assertIn('$39.95', draft['listing_text'])
        self.assertEqual(draft['description'], self.initial['description'])
        with m._p058_db() as db:
            row = db.execute('SELECT data FROM hold_lots WHERE picker_id=?', (self.picker,)).fetchone()
        self.assertEqual(json.loads(row['data'])['snapshot']['draft']['price'], '39.95')

    def test_two_concurrent_confirmations_of_same_version_allow_one_change(self):
        form = self.review_form()
        cookie = self.client.get_cookie('session').value

        def confirm(price):
            client = m.app.test_client()
            client.set_cookie('session', cookie)
            return self.save(client, form, price).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(confirm, ('20', '25')))
        self.assertEqual(sorted(codes), [303, 409])
        self.assertEqual(m.LOT002_DRAFT_CACHE[self.picker]['price'], ('20.00', '25.00')[codes.index(303)])

    def test_failed_save_restores_previous_version_and_form_can_retry(self):
        form = self.review_form()
        with patch.object(m, '_save_active_lot_checkpoint', return_value=False):
            self.assertEqual(self.save(self.client, form, '40.00').status_code, 503)
        self.assertEqual(m.LOT002_DRAFT_CACHE[self.picker]['price'], '15.00')
        self.assertEqual(self.save(self.client, form, '40.00').status_code, 303)
        self.assertEqual(m.LOT002_DRAFT_CACHE[self.picker]['price'], '40.00')

    def test_missing_csrf_missing_version_and_subcent_price_are_rejected(self):
        form = self.review_form()
        self.assertEqual(self.save(self.client, {'draft_digest': form['draft_digest']}, '12').status_code, 403)
        self.assertEqual(self.save(self.client, {'hold_csrf': form['hold_csrf']}, '12').status_code, 409)
        self.assertEqual(self.save(self.client, form, '0.004').status_code, 400)
        self.assertEqual(m.LOT002_DRAFT_CACHE[self.picker]['price'], '15.00')
