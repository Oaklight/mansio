import test from 'node:test';
import assert from 'node:assert/strict';

import { DEFAULT_ROUTE, href, parseHash, resolveHash } from '../../src/mansio_web/admin/assets/router.js';

test('parses known routes, ids and queries', () => {
    assert.deepEqual(parseHash('#/overview'), { name: 'overview', id: null, query: new URLSearchParams() });
    const channel = parseHash('#/channels/general');
    assert.equal(channel.name, 'channels');
    assert.equal(channel.id, 'general');
    const messages = parseHash('#/messages?channel=general&sender=alice');
    assert.equal(messages.name, 'messages');
    assert.equal(messages.query.get('channel'), 'general');
    assert.equal(messages.query.get('sender'), 'alice');
});

test('unknown routes fall back to overview', () => {
    assert.equal(parseHash('#/nope').name, DEFAULT_ROUTE);
    assert.equal(parseHash('#/nope/deeper').name, DEFAULT_ROUTE);
    assert.equal(parseHash('').name, DEFAULT_ROUTE);
});

test('a malformed percent sequence does not throw', () => {
    const route = parseHash('#/channels/%E0%A4%A');
    assert.equal(route.name, 'channels');
    assert.equal(route.id, '%E0%A4%A');
    assert.doesNotThrow(() => parseHash('#/users/%'));
    assert.equal(parseHash('#/users/%').name, 'users');
});

test('resolveHash redirects a malformed percent sequence to overview', () => {
    const { route, redirect } = resolveHash('#/channels/%E0%A4%A');
    assert.equal(redirect, true);
    assert.equal(route.name, DEFAULT_ROUTE);
    assert.equal(route.id, null);
    assert.equal(resolveHash('#/users/%').redirect, true);
});

test('resolveHash redirects an unknown screen name to overview', () => {
    for (const hash of ['#/nope', '#/nope/deeper']) {
        const { route, redirect } = resolveHash(hash);
        assert.equal(redirect, true, hash);
        assert.equal(route.name, DEFAULT_ROUTE, hash);
        assert.equal(route.id, null, hash);
    }
});

test('resolveHash keeps the empty hash and valid routes', () => {
    for (const hash of ['', '#/overview', '#/channels/general', '#/messages?channel=general']) {
        assert.equal(resolveHash(hash).redirect, false, hash);
    }
    assert.equal(resolveHash('').route.name, DEFAULT_ROUTE);
    assert.equal(resolveHash('#/channels/general').route.id, 'general');
});

test('href round-trips names, ids and queries', () => {
    assert.equal(href('overview'), '#/overview');
    assert.equal(href('channels', { id: 'general' }), '#/channels/general');
    assert.equal(href('messages', { query: { channel: 'general' } }), '#/messages?channel=general');
    assert.equal(parseHash(href('channels', { id: 'general' })).id, 'general');
});
