const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');

const source = readFileSync('static/mobile-prepare.js', 'utf8');

async function simulate(replies) {
    const button = { disabled: false };
    const status = { textContent: '', append(link) { this.link = link; } };
    let submit;
    let opened;
    const form = { action: '/mobile/prepare-stage', querySelector: () => button,
        addEventListener: (_, handler) => { submit = handler; } };
    const stages = [];
    const delays = [];
    vm.runInNewContext(source, {
        document: { getElementById: (id) => id === 'mobile-prepare' ? form : status, createElement: () => ({}) },
        window: { location: { assign: (url) => { opened = url; } } },
        FormData: class extends Map { constructor() { super(); } },
        AbortController,
        setTimeout: (callback, duration) => {
            delays.push(duration);
            return setTimeout(callback, 2);
        },
        clearTimeout,
        fetch: async (_url, { body, signal }) => {
            stages.push(body.get('stage'));
            assert.ok(replies.length);
            const next = replies.shift();
            if (next === 'stall') {
                return { ok: true, json: () => new Promise((_, reject) => {
                    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
                }) };
            }
            return { ok: !next.error, json: async () => next };
        },
    });
    await submit({ preventDefault() {} });
    assert.equal(button.disabled, false);
    return { stages, delays, opened, status };
}

test('prepares the three stages once and opens the saved draft', async () => {
    const result = await simulate([{ next: 'research' }, { next: 'draft' }, { open: '/picker/review-final-draft' }]);
    assert.deepEqual(result.stages, ['photos', 'research', 'draft']);
    assert.deepEqual(result.delays, [165_000, 330_000, 150_000]);
    assert.equal(result.opened, '/picker/review-final-draft');
});

test('stops on a stalled response body and permits a later explicit retry', async () => {
    const result = await simulate([{ next: 'research' }, 'stall']);
    assert.deepEqual(result.stages, ['photos', 'research']);
    assert.match(result.status.textContent, /Сервер довго відповідає/);
});

test('stops an invalid repeated stage and surfaces the existing results link if provided', async () => {
    const repeated = await simulate([{ next: 'research' }, { next: 'research' }]);
    assert.deepEqual(repeated.stages, ['photos', 'research']);
    assert.match(repeated.status.textContent, /Підготовка зупинилася/);
    const blocked = await simulate([{ error: 'ШІ очікує погодження', open: '/mobile/acceptance-check' }]);
    assert.equal(blocked.status.link.href, '/mobile/acceptance-check');
});
