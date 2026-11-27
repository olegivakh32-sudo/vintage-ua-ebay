"""Reproduce the M12 application path without sending any provider API requests."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

import requests

from acceptance_stage import sha
from ai_pilot import _public_catalogue_url
from blind_live_pilot import API
from multiview_acceptance import ARCHIVE, ARTIFACTS, MANIFEST, STAGE, MultiviewBudget, blind_inputs, frozen_inputs
from s1_acceptance import LotFailure, run_lots


RECEIPTS = ARTIFACTS / 'lotnexo-m12-46062461677-evidence.zip'
RECEIPT_SHA = '9d7fc7baf43dd457b65c4f193d5b7dc8637eb2d8b8aa38efd7f750e860357869'


class ArchivedBudget(MultiviewBudget):
    def __init__(self, plan, output, recorded):
        super().__init__(plan, output)
        self.recorded = recorded
        self.used = []
        self.missing = set()

    def bind(self, lot, original):
        payload = super().bind(lot, original)
        key = self.pending[:2]
        if key not in self.recorded:
            self.missing.add(key)
            raise LotFailure('ARCHIVED_RESPONSE_UNAVAILABLE')
        return payload

    def skip_lot(self, lot, reason):
        reason = 'ARCHIVED_RESPONSE_UNAVAILABLE' if (lot, 'research') in self.missing else reason
        super().skip_lot(lot, reason)

    def send(self, request, session, kwargs):
        key = self.pending[:2]
        if key in self.used:
            self.stop('DUPLICATE_ARCHIVED_RESPONSE')
        count, reply = self.recorded[key]

        def archived_response(session, method, url, **kwargs):
            if method != 'POST' or url not in (f'{API}/responses/input_tokens', f'{API}/responses'):
                raise AssertionError('Unexpected archived transport call')
            body = {'input_tokens': count} if url.endswith('/input_tokens') else reply
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(body).encode()
            return response

        response = super().send(archived_response, session, kwargs)
        self.used.append(key)
        return response


def replay(output, *, public=False):
    if sha(RECEIPTS) != RECEIPT_SHA:
        raise ValueError('Archived evidence checksum mismatch')
    plan = frozen_inputs(MANIFEST, ARCHIVE)
    with ZipFile(RECEIPTS) as bundle:
        ledger = json.loads(bundle.read('results/run.json'))
        if ledger['stage'] != STAGE or ledger['blind_sha256'] != plan['stages'][STAGE]['blind_sha256']:
            raise ValueError('Archived evidence is from another cohort')
        recorded = {(row['lot'], row['step']): (row['input_tokens_counted'],
                    json.loads(bundle.read('results/' + row['response_file'])))
                    for row in ledger['attempts'] if row.get('response_file')}

    public_requests = Counter()
    transport = requests.Session.request

    def reject_network(session, method, url, **kwargs):
        if method.upper() == 'GET' and _public_catalogue_url(url):
            if public:
                public_requests[url] += 1
                return transport(session, method, url, **kwargs)
            response = requests.Response()
            response.status_code = 503
            return response
        raise AssertionError('Non-catalogue external request in archive replay')

    budget = ArchivedBudget(plan, output, recorded)
    with TemporaryDirectory(prefix='lotnexo-m12-replay-') as directory, patch.dict(os.environ, {
        'ENABLE_LIVE_PUBLISH': '0', 'ALLOW_PAID_AI': '0', 'APP_SECRET': 'archived-m12-local',
        'VINTAGE_RESEARCH_ALTERNATE': '0', 'ACTIVE_LOT_CHECKPOINT_DIR': directory, 'HOLD_QUEUE_DIR': directory,
        'VINTAGE_ACCEPTANCE_SERVER': '0', 'VINTAGE_OPERATOR_PASSWORD': '', 'RENDER': '',
    }), patch.object(requests.Session, 'request', side_effect=reject_network):
        import app
        import museum_photo_evidence

        museum_photo_evidence.verified_record.cache_clear()
        lots = blind_inputs(ARCHIVE, plan['stages'][STAGE], Path(directory))
        with patch.multiple(app, OPENAI_API_KEY='archived-transport', EBAY_CLIENT_ID=None,
                            EBAY_CLIENT_SECRET=None, EBAY_REFRESH_TOKEN=None):
            run_lots(budget, lots, output)
    if budget.used != list(recorded):
        raise RuntimeError('The application did not consume every archived response exactly once')
    return {'lots': len(lots), 'archived_responses': len(budget.used), 'missing_responses': sorted(budget.missing),
            'public_catalogue_requests': sum(public_requests.values()), 'new_provider_requests': 0,
            'new_provider_cost_usd': '0'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--public-catalogue', action='store_true', help='Allow free reads of exact public museum records.')
    args = parser.parse_args()
    print(json.dumps(replay(args.output, public=args.public_catalogue)))
