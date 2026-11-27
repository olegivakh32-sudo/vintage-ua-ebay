"""Read-only Sandbox reachability checks for an operator's test credentials."""
import argparse
import json
import os
import re

import requests

from catalog_policy import TaxonomyTree, marketplace_code
from workspace_ebay import TOKEN_API


SANDBOX = 'https://api.sandbox.ebay.com'
PRODUCTION = 'https://api.ebay.com'
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


def production_taxonomy(*, connect=False, marketplace='EBAY_US', category_id=None, config=None, transport=requests):
    """Read the current category tree and optionally a leaf's required aspects with an application token."""
    marketplace_code(marketplace)
    if category_id is not None and not re.fullmatch(r'[0-9]{1,12}', category_id):
        raise ValueError('Expected a numeric leaf category ID')
    config = os.environ if config is None else config
    client = config.get('VINTAGE_EBAY_TAXONOMY_CLIENT_ID')
    secret = config.get('VINTAGE_EBAY_TAXONOMY_CLIENT_SECRET')
    token = _token(config.get('VINTAGE_EBAY_TAXONOMY_TOKEN'))
    report = {'environment': 'production', 'marketplace': marketplace, 'app_credentials': bool(client and secret),
              'app_token': bool(token), 'taxonomy': 'NOT_RUN', 'aspects': 'NOT_RUN'}
    if not connect or not (token or (client and secret)):
        return report
    try:
        if not token:
            reply = transport.post(PRODUCTION + '/identity/v1/oauth2/token',
                                   data={'grant_type': 'client_credentials', 'scope': APP_SCOPE},
                                   auth=(client, secret), timeout=TIMEOUT, allow_redirects=False)
            token = _token(_body(reply, 'OAuth').get('access_token'))
            if token is None:
                raise ValueError('OAuth: missing token')
        headers = {'Authorization': 'Bearer ' + token}
        base = PRODUCTION + '/commerce/taxonomy/v1'
        reply = transport.get(base + '/get_default_category_tree_id', params={'marketplace_id': marketplace},
                              headers=headers, timeout=TIMEOUT, allow_redirects=False)
        tree = TaxonomyTree.from_response(marketplace, _body(reply, 'Taxonomy'))
        report.update(taxonomy='OK', tree_id=tree.tree_id, tree_version=tree.version)
        if category_id:
            reply = transport.get(f'{base}/category_tree/{tree.tree_id}/get_item_aspects_for_category',
                                  params={'category_id': category_id}, headers=headers,
                                  timeout=TIMEOUT, allow_redirects=False)
            values = _body(reply, 'Leaf aspects').get('aspects')
            if not isinstance(values, list):
                raise ValueError('Leaf aspects: missing values')
            required = [row['localizedAspectName'] for row in values
                        if row.get('aspectConstraint', {}).get('aspectRequired') is True]
            report.update(aspects='OK', category_id=category_id, required_aspects=required)
    except (ValueError, KeyError, TypeError, AttributeError, requests.RequestException) as error:
        target = 'aspects' if report['taxonomy'] == 'OK' else 'taxonomy'
        report.update({target: 'REVIEW', 'error': str(error) if type(error) is ValueError
                       and re.fullmatch(r'[\w ]+: (?:HTTP [0-9]{3}|missing token|missing values)', str(error))
                       else 'PROBE_FAILED'})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--marketplace', default='EBAY_US')
    parser.add_argument('--connect', action='store_true', help='Run OAuth and read-only requests.')
    parser.add_argument('--production-taxonomy', action='store_true', help='Read public production Taxonomy as an app.')
    parser.add_argument('--category-id', help='Check required aspects for this production leaf.')
    options = parser.parse_args()
    try:
        if options.production_taxonomy:
            report = production_taxonomy(connect=options.connect, marketplace=options.marketplace,
                                         category_id=options.category_id)
        else:
            if options.category_id is not None:
                parser.error('--category-id requires --production-taxonomy')
            report = probe(connect=options.connect, marketplace=options.marketplace)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report))
    return int(options.connect and (report['taxonomy'] != 'OK' or 'REVIEW' in (report.get('inventory'),
                                                                              report.get('aspects'))))


if __name__ == '__main__':
    raise SystemExit(main())
