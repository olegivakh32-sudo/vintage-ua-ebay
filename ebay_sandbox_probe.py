"""Read-only Sandbox reachability checks for an operator's test credentials."""
import argparse
import json
import os
import re

import requests

from catalog_policy import TaxonomyTree, marketplace_code
from workspace_ebay import TOKEN_API


SANDBOX = 'https://api.sandbox.ebay.com'
APP_SCOPE = 'https://api.ebay.com/oauth/api_scope'
TIMEOUT = (4, 10)


def _body(response, action):
    if response.status_code != 200:
        raise ValueError(f'{action}: HTTP {response.status_code}')
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f'{action}: expected an object')
    return body


def _token(value):
    return value if isinstance(value, str) and len(value) <= 8192 and re.fullmatch(r'[\x21-\x7e]+', value) else None


def probe(*, connect=False, marketplace='EBAY_US', config=None, transport=requests):
    marketplace_code(marketplace)
    config = os.environ if config is None else config
    client = config.get('VINTAGE_SANDBOX_CLIENT_ID')
    secret = config.get('VINTAGE_SANDBOX_CLIENT_SECRET')
    seller_token = _token(config.get('VINTAGE_SANDBOX_SELLER_TOKEN'))
    report = {'environment': 'sandbox', 'marketplace': marketplace, 'app_credentials': bool(client and secret),
              'seller_token': bool(seller_token), 'taxonomy': 'NOT_RUN', 'inventory': 'NOT_RUN'}
    if not connect or not client or not secret:
        return report
    try:
        data = _body(transport.post(TOKEN_API, data={'grant_type': 'client_credentials', 'scope': APP_SCOPE},
                                    auth=(client, secret), timeout=TIMEOUT, allow_redirects=False), 'OAuth')
        token = _token(data.get('access_token'))
        if token is None:
            raise ValueError('OAuth: missing token')
        data = _body(transport.get(SANDBOX + '/commerce/taxonomy/v1/get_default_category_tree_id',
                                   params={'marketplace_id': marketplace}, headers={'Authorization': 'Bearer ' + token},
                                   timeout=TIMEOUT, allow_redirects=False), 'Taxonomy')
        tree = TaxonomyTree.from_response(marketplace, data)
        report.update(taxonomy='OK', tree_id=tree.tree_id, tree_version=tree.version)
    except (ValueError, requests.RequestException):
        report['taxonomy'] = 'REVIEW'
    if seller_token:
        try:
            data = _body(transport.get(SANDBOX + '/sell/inventory/v1/inventory_item',
                                       params={'limit': 1}, headers={'Authorization': 'Bearer ' + seller_token},
                                       timeout=TIMEOUT, allow_redirects=False), 'Inventory')
            if type(data.get('total')) is not int or data['total'] < 0:
                raise ValueError('Inventory: missing total')
            report.update(inventory='OK', inventory_total=data['total'])
        except (ValueError, requests.RequestException):
            report['inventory'] = 'REVIEW'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--marketplace', default='EBAY_US')
    parser.add_argument('--connect', action='store_true', help='Run OAuth and read-only Sandbox requests.')
    options = parser.parse_args()
    try:
        report = probe(connect=options.connect, marketplace=options.marketplace)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report))
    return int(options.connect and (report['taxonomy'] != 'OK' or report['inventory'] == 'REVIEW'))


if __name__ == '__main__':
    raise SystemExit(main())
