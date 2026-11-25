'use strict';

const byId = (id) => document.getElementById(id);
const notice = byId('notice');
const retryKeys = new Map();
let csrf = null;
let terms = '';
let storeId = new URL(window.location.href).searchParams.get('shop') || '';
let selectedDraft = null;

async function fetchWithLimit(path, options, milliseconds = 15_000) {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), milliseconds);
    try {
        const reply = await fetch(path, {...options, signal: controller.signal});
        const data = await reply.json();
        return {ok: reply.ok, status: reply.status, data};
    } catch (error) {
        if (controller.signal.aborted) throw new Error('Сервер довго відповідає. Оновіть стан перед повтором.');
        throw error;
    } finally {
        clearTimeout(deadline);
    }
}

async function api(path, method = 'GET', data, repeatable = false) {
    const headers = {};
    const options = { method, headers };
    let replayPath;
    if (data !== undefined) {
        headers['Content-Type'] = 'application/json';
        if (csrf) headers['X-CSRF-Token'] = csrf;
        options.body = JSON.stringify(data);
    }
    if (repeatable) {
        replayPath = path + options.body;
        if (!retryKeys.has(replayPath)) retryKeys.set(replayPath, crypto.randomUUID());
        headers['Idempotency-Key'] = retryKeys.get(replayPath);
    }
    const response = await fetchWithLimit(path, options);
    const result = response.data;
    if (replayPath && response.status < 500) retryKeys.delete(replayPath);
    if (!response.ok) throw new Error(result.error || 'Повторіть дію.');
    return result;
}

function action(handler) {
    return async (event) => {
        event.preventDefault();
        notice.textContent = '';
        const button = event.submitter || event.currentTarget;
        button.disabled = true;
        try {
            await handler(event.currentTarget);
        } catch (error) {
            notice.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    };
}

const fields = (form) => Object.fromEntries(new FormData(form));
const currentStore = () => '/stores/' + encodeURIComponent(storeId);
const currentAlbum = () => currentStore() + '/drafts/' + encodeURIComponent(selectedDraft.id) + '/photos';
const currency = (minor) => (minor / 100).toFixed(2) + ' грн (тест)';

function rows(id, entries, build) {
    byId(id).replaceChildren(...entries.map((entry) => {
        const item = document.createElement('li');
        build(item, entry);
        return item;
    }));
}

async function showDrafts() {
    storeId = byId('stores').value;
    byId('store-tools').hidden = !storeId;
    byId('ebay-state').textContent = '';
    if (!storeId) {
        byId('photo-tools').hidden = true;
        return;
    }
    const path = currentStore();
    const [content, ebay] = await Promise.all([api(path + '/drafts'), api(path + '/ebay')]);
    if (path !== currentStore()) return;
    rows('drafts', content.drafts, (item, draft) => {
        item.textContent = draft.sku + ' · ' + draft.title;
        const open = document.createElement('button');
        open.textContent = 'Фото';
        open.onclick = action(async () => {
            selectedDraft = draft;
            await showAlbum();
        });
        item.append(open);
    });
    if (!content.drafts.some((draft) => draft.id === selectedDraft?.id)) {
        selectedDraft = null;
        byId('photo-tools').hidden = true;
        byId('photo-list').replaceChildren();
    }
    byId('ebay-state').textContent = ebay.connection ? 'Магазин Sandbox підключено' :
        ebay.enabled ? 'Магазин Sandbox можна підключити' : 'eBay Sandbox ще не налаштовано';
    byId('link-ebay').hidden = !ebay.enabled || Boolean(ebay.connection);
    byId('unlink-ebay').hidden = !ebay.connection;
}

async function showAlbum() {
    const path = currentAlbum();
    const data = await api(path);
    if (!selectedDraft || path !== currentAlbum()) return;
    byId('photo-title').textContent = 'Фото · ' + selectedDraft.sku;
    byId('photo-tools').hidden = false;
    const cover = data.suggested_main ? ' · попереднє головне №' + data.suggested_main : '';
    const codes = data.retail_codes.status === 'CONFLICT' ? ' · кілька різних кодів' :
        data.retail_codes.status === 'UNIQUE' ? ' · код ' + data.retail_codes.candidates[0].code : '';
    byId('photo-hints').textContent = data.photos.length + ' із 24' + cover + codes;
    const states = {
        UNREQUESTED: '', PENDING: 'Фото в черзі на локальну перевірку.',
        RUNNING: 'Триває локальна перевірка.', READY: 'Готовий попередній відбір фото.',
        FAILED: 'Перевірка зупинилася після кількох спроб. Оригінали збережені.',
    };
    byId('photo-review-status').textContent = states[data.review.state];
    const reviewButton = byId('photo-review');
    reviewButton.hidden = !data.photos.length || !['UNREQUESTED', 'PENDING', 'RUNNING'].includes(data.review.state);
    reviewButton.textContent = data.review.state === 'UNREQUESTED' ? 'Перевірити фото' : 'Оновити стан';
    reviewButton.dataset.enqueue = data.review.state === 'UNREQUESTED' ? 'yes' : 'no';
    const assessed = new Map((data.review.photos || []).map((frame) => [frame.slot, frame.role]));
    const roles = {EVIDENCE_ONLY: 'Вимірювальний кадр', EBAY_PRODUCT: 'Для оголошення', NEEDS_REVIEW: 'Потрібна перевірка'};
    rows('photo-list', data.photos, (item, photo) => {
        const image = document.createElement('img');
        image.src = path + '/' + photo.slot + '/preview';
        image.alt = 'Фото №' + photo.slot;
        const original = document.createElement('a');
        const role = roles[assessed.get(photo.slot)];
        original.textContent = role ? 'Оригінал · ' + role : 'Оригінал';
        original.href = path + '/' + photo.slot + '/original';
        item.append(image, original);
    });
}

async function showStores() {
    const response = await api('/stores');
    byId('stores').replaceChildren(...response.stores.map((store) =>
        new Option(store.name + ' · ' + store.marketplace, store.id, false, store.id === storeId)));
    await showDrafts();
}

async function showBilling() {
    const state = await api('/billing');
    byId('usage').textContent = 'Підготовлено ' + state.used + ' із ' + state.limit +
        ' · облік витрат ' + currency(state.estimated_minor);
    rows('payments', state.payments, (item, payment) => {
        item.textContent = currency(payment.minor) + (payment.refunded_at ? ' · повернено' : '');
        if (!payment.refunded_at) {
            const refund = document.createElement('button');
            refund.textContent = 'Імітувати повернення';
            refund.onclick = action(async () => {
                await api('/billing/test/refund', 'POST', {payment_id: payment.id}, true);
                await showBilling();
            });
            item.append(refund);
        }
    });
}

async function showTickets() {
    const response = await api('/support');
    rows('tickets', response.tickets, (item, ticket) => { item.textContent = ticket.detail; });
}

async function showWorkspace(token) {
    csrf = token;
    byId('workspace').hidden = false;
    byId('entry').hidden = true;
    await Promise.all([showStores(), showBilling(), showTickets()]);
}

function endWorkspace() {
    csrf = null;
    storeId = '';
    selectedDraft = null;
    retryKeys.clear();
    byId('workspace').hidden = true;
    byId('entry').hidden = false;
    document.querySelectorAll('form').forEach((form) => form.reset());
    byId('photo-tools').hidden = true;
    for (const area of ['stores', 'drafts', 'payments', 'tickets', 'photo-list']) byId(area).replaceChildren();
}

byId('login').onsubmit = action(async (form) => {
    const response = await api('/login', 'POST', fields(form));
    form.reset();
    await showWorkspace(response.csrf);
});
byId('register').onsubmit = action(async (form) => {
    const {email, password, invite} = fields(form);
    await api('/register', 'POST', {email, password, invite, terms_version: terms});
    form.reset();
    notice.textContent = 'Акаунт створено. Увійдіть до кабінету.';
});
byId('add-store').onsubmit = action(async (form) => {
    const store = await api('/stores', 'POST', fields(form));
    storeId = store.id;
    form.reset();
    await showStores();
});
byId('stores').onchange = action(showDrafts);
byId('new-draft').onsubmit = action(async (form) => {
    await api(currentStore() + '/drafts', 'POST', fields(form), true);
    form.reset();
    await showDrafts();
});
byId('photo-upload').onsubmit = action(async (form) => {
    const path = currentAlbum();
    const images = [...form.elements.gallery.files, ...form.elements.camera.files];
    for (const file of images.slice(0, 24)) {
        const data = new FormData();
        data.append('photo', file);
        const response = await fetchWithLimit(path, {method: 'POST', headers: {'X-CSRF-Token': csrf}, body: data}, 120_000);
        if (!response.ok) throw new Error(response.data.error || 'Фото не збережено.');
        if (selectedDraft && path === currentAlbum()) await showAlbum();
    }
    form.reset();
});
byId('photo-review').onclick = action(async (button) => {
    if (button.dataset.enqueue === 'yes') await api(currentAlbum() + '/review', 'POST', {});
    if (selectedDraft) await showAlbum();
});
byId('link-ebay').onclick = action(async () => {
    const state = await api(currentStore() + '/ebay', 'POST', {});
    window.location.assign(state.authorization_url);
});
byId('unlink-ebay').onclick = action(async () => {
    await api(currentStore() + '/ebay', 'DELETE', {});
    await showDrafts();
});
byId('meter').onclick = action(async () => {
    await api(currentStore() + '/test-preparation', 'POST', {}, true);
    await showBilling();
});
byId('checkout').onsubmit = action(async (form) => {
    await api('/billing/test/checkout', 'POST', fields(form), true);
    await showBilling();
});
byId('cancel').onclick = action(async () => {
    await api('/billing/test/cancel', 'POST', {}, true);
    notice.textContent = 'Тестове продовження скасовано.';
});
byId('support').onsubmit = action(async (form) => {
    await api('/support', 'POST', fields(form));
    form.reset();
    await showTickets();
});
byId('logout').onclick = action(async () => {
    await api('/logout', 'POST', {});
    endWorkspace();
});
byId('erase').onsubmit = action(async (form) => {
    const {password, confirm} = fields(form);
    await api('/account', 'DELETE', {password, confirm: Boolean(confirm)});
    endWorkspace();
    notice.textContent = 'Тестовий акаунт видалено.';
});

(async function initialize() {
    try {
        const [legal, tariffs] = await Promise.all([api('/legal'), api('/plans')]);
        terms = legal.version;
        byId('tariffs').replaceChildren(...tariffs.plans.map((plan) => new Option(
            (plan.months === 1 ? 'Місяць' : 'Рік') + ' · ' + currency(plan.price_minor), plan.id)));
        const lookup = await fetchWithLimit('/session');
        if (lookup.ok) await showWorkspace(lookup.data.csrf);
    } catch (error) {
        notice.textContent = error.message;
    }
}());
