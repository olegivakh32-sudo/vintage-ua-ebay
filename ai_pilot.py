"""Dry-run or execute one labelled lot through the isolated recognition pipeline."""

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
import json
import os
import re
from pathlib import Path
import tempfile
from unittest.mock import patch

import requests


@dataclass
class PilotLimits:
    stop_usd: Decimal
    attempts: int = 0
    used_web: bool = False

    def bound(self, payload, spent):
        if self.attempts >= 5 or Decimal(str(spent)) >= self.stop_usd:
            raise RuntimeError('Ліміт пілота вичерпано.')
        images = sum(
            entry.get('type') == 'input_image'
            for message in payload['input'] if isinstance(message, dict)
            for entry in message.get('content', []) if isinstance(entry, dict)
        ) if isinstance(payload.get('input'), list) else 0
        if images > 24 or not 1 <= payload.get('max_output_tokens', 0) <= 5200:
            raise RuntimeError('Розмір запиту перевищує ліміт пілота.')
        tools = payload.get('tools') or []
        if tools and (self.used_web or len(tools) != 1 or tools[0].get('type') != 'web_search'):
            raise RuntimeError('Вебпошук пілота вже використано або запитано інший інструмент.')
        self.attempts += 1
        if tools:
            self.used_web = True
            return dict(payload, max_tool_calls=2)
        return payload


def inputs(filename):
    data = json.loads(filename.read_text(encoding='utf-8'))
    photos = [(filename.parent / path).resolve() for path in data['photos']]
    if not 1 <= len(photos) <= 24 or len(set(photos)) != len(photos):
        raise ValueError('Виберіть від 1 до 24 різних фото.')
    if any(not path.is_file() or path.stat().st_size > 10 * 1024 * 1024 for path in photos):
        raise ValueError('Кожне фото має бути локальним файлом до 10 МБ.')
    if not isinstance(data['lot'], str) or not data['lot'].strip():
        raise ValueError('Потрібен ідентифікатор лота.')
    taxonomy = data['taxonomy']
    if not taxonomy['tree_id'] or not taxonomy['suggestions'] or not isinstance(taxonomy['aspects_by_category'], dict):
        raise ValueError('Потрібен збережений знімок таксономії eBay.')
    return data, photos


class DraftForm(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.active = False
        self.values = {}
        self.textarea = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == 'form':
            self.active = data.get('action') == '/picker/final-listing'
        if not self.active:
            return
        if tag == 'textarea' and data.get('name'):
            self.textarea = data['name']
            self.values[self.textarea] = ''
        if tag == 'input' and data.get('name'):
            if data.get('type') == 'hidden':
                self.values[data['name']] = data.get('value', '')
            if data.get('name') == 'material_choice' and 'checked' in data:
                self.values.setdefault('material_choice', []).append(data.get('value', ''))

    def handle_data(self, data):
        if self.textarea:
            self.values[self.textarea] += data

    def handle_endtag(self, tag):
        if tag == 'textarea':
            self.textarea = None
        if tag == 'form':
            self.active = False



def run(data, photos, limits):
    import app as vintage

    client = vintage.app.test_client()
    actual_request = requests.Session.request
    actual_api = vintage._p054_openai_request

    def request_guard(session, method, url, *args, **kwargs):
        if method.upper() != 'POST' or url != vintage.OPENAI_URL:
            raise RuntimeError('Зовнішній запит поза межами пілота.')
        kwargs['allow_redirects'] = False
        return actual_request(session, method, url, *args, **kwargs)

    def bounded_api(payload, timeout, usage_picker_id, final_draft_slot):
        picker, usage = vintage._current_usage_record(usage_picker_id)
        bounded = limits.bound(payload, usage.get('estimated_cost_usd', 0))
        return actual_api(bounded, timeout, picker, final_draft_slot)

    taxonomy = data['taxonomy']

    def recorded_taxonomy(method, url, **kwargs):
        if method == 'GET' and url.endswith('/get_default_category_tree_id'):
            body = {'categoryTreeId': str(taxonomy['tree_id'])}
        elif method == 'GET' and url.endswith('/get_category_suggestions'):
            body = {'categorySuggestions': taxonomy['suggestions']}
        elif method == 'GET' and url.endswith('/get_item_aspects_for_category'):
            aspects = taxonomy['aspects_by_category'].get(kwargs['params']['category_id'])
            if not isinstance(aspects, list):
                raise RuntimeError('У знімку немає характеристик цієї категорії.')
            body = {'aspects': aspects}
        else:
            raise RuntimeError('Виклик поза збереженою таксономією.')
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps(body).encode()
        return result

    with tempfile.TemporaryDirectory(prefix='vintage-ai-pilot-') as tmp, patch.dict(os.environ, {
        'ALLOW_PAID_AI': '1', 'ENABLE_LIVE_PUBLISH': '0', 'ENABLE_BRAND_RESCAN': '0', 'HOLD_QUEUE_DIR': tmp,
    }), patch.object(vintage, 'ACTIVE_LOT_CHECKPOINT_DIR', tmp), patch.object(
        vintage, '_p054_openai_request', bounded_api
    ), patch.object(requests.Session, 'request', request_guard):
        client.get('/mobile')
        with client.session_transaction() as session:
            csrf = session['hold_csrf']
            session['current_lot_number'] = 4
        with ExitStack() as stack, patch.object(vintage, '_discover_next_lot_from_ebay', return_value=4):
            upload = client.post('/mobile/upload', data={
                'hold_csrf': csrf, 'photos': [(stack.enter_context(path.open('rb')), path.name) for path in photos],
            })
        if upload.status_code != 303:
            raise ValueError(f'Завантаження фото зупинено, HTTP {upload.status_code}.')
        with client.session_transaction() as session:
            picker = session['picker_session_id']
        stage_a = client.post('/picker/analyze')
        analysis = vintage.ANALYSIS_CACHE.get(picker, '')
        approval = None
        if stage_a.status_code == 200:
            approval = client.post('/picker/editor/approve', data={'main_photo': data.get('main_photo', 0)})
        stage_bc = client.post('/picker/fast-research', data={'analysis': analysis}) if (
            approval is not None and approval.status_code == 302
        ) else None
        identification = vintage.IDENTIFICATION_CACHE.get(picker, '')
        market = vintage.MARKET_CACHE.get(picker, '')
        measurement = vintage.MEASUREMENT_CACHE.get(picker, {})
        identity = vintage._research_machine_json(identification, 'IDENTITY_JSON')
        facts = vintage.AUTO_FACT_CACHE.get(picker, {})
        draft = {}
        stage_d = None
        if analysis and identification and market and data.get('seller_measurements'):
            fields = data['seller_measurements']
            stage_d = client.post('/picker/shipping-submit', data={
                'analysis': analysis, 'identification': identification, 'market': market,
                'item_weight': fields['weight_kg'], 'dim_l': fields['length_cm'],
                'dim_w': fields['width_cm'], 'dim_h': fields['height_cm'],
                'seller_verified_measurements': 'yes',
            })
            if stage_d.status_code == 303:
                shipping = client.get('/picker/resume-shipping')
                fields = DraftForm(shipping.get_data(as_text=True)).values
                if fields:
                    stage_d = client.post('/picker/final-listing', data=fields)
            draft = vintage.LOT002_DRAFT_CACHE.get(picker, {})
        kind = str(identity.get('type') or '')
        category_draft = dict(draft) if draft else {'title': kind, 'item_specifics': {'Type': kind}}
        category_draft['visual_object_type'] = facts.get('object_type') or ''
        category_id, category_note = None, None
        with vintage.app.test_request_context(), patch.object(vintage, '_lot002_request', recorded_taxonomy):
            if kind and kind.upper() != 'UNKNOWN':
                try:
                    candidate, label = vintage._current_category('recorded', category_draft['title'], category_draft)
                    category_draft.update(category_id=candidate, category_name=label)
                    valid, category_note = vintage._p057_category_snapshot_check(category_draft)
                    if valid:
                        category_id = candidate
                except RuntimeError as exc:
                    category_note = str(exc)
        return {
            'lot': data['lot'], 'identity': kind, 'period': identity.get('period'),
            'material': identity.get('material'),
            'ocr': '\n'.join(dict.fromkeys(mark['literal'] for mark in facts.get('markings', []))),
            'category_id': category_id, 'admitted': False,
            'category_source': 'recorded_taxonomy', 'category_note': category_note,
            'stage_http': {'visual': stage_a.status_code, 'research': stage_bc.status_code if stage_bc else None,
                           'draft': stage_d.status_code if stage_d else None},
            'stage_messages': [match[1] for stage in (stage_a, approval, stage_bc, stage_d)
                               if stage is not None and stage.status_code >= 400
                               and (match := re.search(r'<h2>([^<]+)</h2>', stage.get_data(as_text=True)))],
            'photo_roles': vintage.PHOTO_ROLE_CACHE.get(picker, {}), 'measurement': measurement,
            'analysis': analysis, 'identification': identification, 'market': market, 'draft': draft,
            'usage': vintage.AI_USAGE_CACHE.get(picker, {}), 'actual_attempts': limits.attempts,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--estimated-stop-usd', type=Decimal, default=Decimal('0.25'))
    args = parser.parse_args()
    try:
        data, photos = inputs(args.manifest)
        if not args.estimated_stop_usd.is_finite() or not 0 < args.estimated_stop_usd <= Decimal('0.25'):
            raise ValueError('Поріг зупинки має бути додатним і не перевищувати 0.25 USD.')
        limits = PilotLimits(args.estimated_stop_usd)
        if not args.execute:
            print(f"{data['lot']}: {len(photos)} фото, до 5 спроб, один запит із вебпошуком (до 2 викликів).")
            print(f"Поріг перед наступним запитом {limits.stop_usd} USD за оцінкою. Оплачених викликів: 0.")
            return 0
        if not args.report or os.environ.get('APPROVE_AI_PILOT') != '1' or not os.environ.get('OPENAI_API_KEY'):
            raise ValueError('Для запуску потрібні погодження, APPROVE_AI_PILOT=1, окремий ключ і новий --report.')
        with args.report.open('x', encoding='utf-8') as report:
            json.dump({'lot': data['lot'], 'status': 'started'}, report, ensure_ascii=False)
            report.flush()
            result = run(data, photos, limits)
            report.seek(0)
            json.dump(result, report, ensure_ascii=False, indent=2)
            report.truncate()
        print(f'Звіт: {args.report}. Спроб API: {limits.attempts}.')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
