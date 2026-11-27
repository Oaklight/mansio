import test from 'node:test';
import assert from 'node:assert/strict';

import * as contracts from '../../src/mansio_web/admin/assets/contracts.js';
import {
    toBucket,
    toChannelBreakdown,
    toChannelDetail,
    toChannelSummary,
    toMessage,
    toPrefix,
    toStats,
    toSubscription,
    toSystem,
    toToken,
    toUser,
} from '../../src/mansio_web/admin/assets/models.js';

const { ContractMismatch } = contracts;

const VALID = {
    parseAdminChannelDetail: {
        name: 'general',
        message_count: 2,
        senders: ['alice'],
        msg_types: [],
        last_message_time: null,
    },
    parseAdminChannelSummary: {
        name: 'general',
        message_count: 1,
        sender_count: 1,
        subscription_count: 0,
        last_message_time: null,
    },
    parseAdminChannelsResult: { channels: [] },
    parseAdminMessagesResult: { count: 0, messages: [] },
    parseAdminPrefixCreatedResult: { ok: true, prefix: {} },
    parseAdminPrefixDeletedResult: { ok: true, deleted: 'group' },
    parseAdminPrefixesResult: { prefixes: [], enabled: true },
    parseAdminStatsResult: { total_messages: 0 },
    parseAdminSubscriptionsResult: { total: 0, channels: [] },
    parseAdminSystemResult: { version: '0.3.0', backend: {}, tokens: {} },
    parseAdminThroughputResult: { window_seconds: 60, buckets: [] },
    parseAdminTokenCreatedResult: { ok: true, token: {} },
    parseAdminTokenDeletedResult: { ok: true, deleted: 'tok' },
    parseAdminTokenRotatedResult: { ok: true, token: {} },
    parseAdminTokensResult: { tokens: [], enabled: false },
    parseAdminUserDeletedResult: { ok: true, deleted_tokens: 0 },
    parseAdminUserDetailResult: { user_id: 'alice', tokens: [], status: 'unknown' },
    parseAdminUserRegisteredResult: { ok: true, user_id: 'alice', token: {} },
    parseAdminUserTokensResult: { user_id: 'alice', tokens: [] },
    parseAdminUsersResult: { users: [], enabled: true },
};

test('every generated parser accepts its minimal document', () => {
    for (const [name, document] of Object.entries(VALID)) {
        const parse = contracts[name];
        assert.equal(typeof parse, 'function', `${name} missing`);
        const parsed = parse(document);
        assert.equal(typeof parsed, 'object');
        assert.ok(Object.isFrozen(parsed), `${name} result should be frozen`);
    }
});

test('optional fields may be omitted or null', () => {
    assert.doesNotThrow(() =>
        contracts.parseAdminChannelDetail({
            name: 'g',
            message_count: 0,
            senders: [],
            msg_types: [],
        }),
    );
    assert.doesNotThrow(() =>
        contracts.parseAdminTokenCreatedResult({ ok: true, supertoken: null, token: {} }),
    );
    assert.doesNotThrow(() =>
        contracts.parseAdminUserDetailResult({
            user_id: 'a',
            tokens: [],
            status: 'unknown',
            last_seen: null,
        }),
    );
});

test('a missing required field names the route', () => {
    assert.throws(
        () => contracts.parseAdminChannelsResult({}),
        (error) =>
            error instanceof ContractMismatch &&
            error.message === 'GET /api/channels: missing channels',
    );
    assert.throws(
        () => contracts.parseAdminUsersResult({ users: [] }),
        (error) => error instanceof ContractMismatch && error.message.includes('missing enabled'),
    );
});

test('wrong field types are rejected', () => {
    assert.throws(
        () => contracts.parseAdminChannelsResult({ channels: 'nope' }),
        (error) =>
            error instanceof ContractMismatch &&
            error.message === 'GET /api/channels: channels must be array',
    );
    assert.throws(
        () => contracts.parseAdminThroughputResult({ window_seconds: '60', buckets: [] }),
        (error) => error instanceof ContractMismatch && error.message.includes('window_seconds'),
    );
    assert.throws(() => contracts.parseAdminChannelsResult([]), ContractMismatch);
});

test('error envelopes parse to a plain object', () => {
    assert.deepEqual(
        contracts.parseErrorEnvelope({ error: 'Bad Request', message: 'nope' }, 'GET /api/x'),
        { error: 'Bad Request', message: 'nope' },
    );
    assert.throws(
        () => contracts.parseErrorEnvelope({ message: 'nope' }, 'GET /api/x'),
        (error) => error.message === 'GET /api/x: missing error',
    );
});

test('toStats builds typed breakdown entries', () => {
    const stats = toStats({
        total_messages: 3,
        total_channels: 1,
        total_senders: 2,
        active_subscriptions: 1,
        channel_breakdown: [
            { channel: 'general', message_count: 3, sender_count: 2, last_message_time: null },
        ],
        msg_type_distribution: { text: 3 },
    });
    assert.equal(stats.total_messages, 3);
    assert.equal(stats.channel_breakdown[0].channel, 'general');
    assert.ok(Object.isFrozen(stats.channel_breakdown));
    assert.throws(() => toStats({ total_messages: 1 }), ContractMismatch);
    assert.throws(
        () =>
            toChannelBreakdown({
                channel: 'g',
                message_count: 1,
                sender_count: 1,
                last_message_time: 2,
            }),
        ContractMismatch,
    );
});

test('message, token and user adapters reject bad rows', () => {
    const message = toMessage({
        id: 'm1',
        channel: 'general',
        sender: 'alice',
        msg_type: 'text',
        payload: 'hi',
        timestamp: '2026-01-01T00:00:00Z',
        metadata: null,
    });
    assert.deepEqual(Object.keys(message), [
        'id',
        'channel',
        'sender',
        'msg_type',
        'payload',
        'timestamp',
        'metadata',
    ]);
    assert.throws(() => toMessage({ id: 'm1' }), ContractMismatch);

    const token = toToken({
        id: 't1',
        token_prefix: 'mst-abc',
        user_id: null,
        label: 'super',
        created_at: '2026-01-01',
        last_used_at: null,
    });
    assert.equal(token.user_id, null);
    assert.throws(() => toToken({ id: 't1' }), ContractMismatch);

    const user = toUser({ user_id: 'alice', token_count: 2, last_active: null });
    assert.equal(user.status, 'unknown');
    assert.equal(user.last_seen, null);
    assert.throws(() => toUser({ user_id: 'alice', token_count: '2' }), ContractMismatch);
});

test('channel, subscription, prefix, system and bucket adapters', () => {
    const summary = toChannelSummary({
        name: 'general',
        message_count: 1,
        sender_count: 1,
        subscription_count: 0,
        last_message_time: null,
    });
    assert.equal(summary.name, 'general');
    assert.throws(
        () => toChannelSummary({ name: 'g', message_count: 1, sender_count: 1 }),
        ContractMismatch,
    );

    const detail = toChannelDetail({
        name: 'general',
        message_count: 1,
        senders: ['alice'],
        msg_types: [{ msg_type: 'text', count: 1 }],
        last_message_time: null,
    });
    assert.deepEqual(detail.senders, ['alice']);
    assert.equal(detail.msg_types[0].count, 1);
    assert.throws(
        () => toChannelDetail({ name: 'g', message_count: 1, senders: [1], msg_types: [] }),
        ContractMismatch,
    );

    assert.equal(toSubscription({ channel: 'g', subscription_ids: ['s1'], count: 1 }).count, 1);
    assert.throws(
        () => toSubscription({ channel: 'g', subscription_ids: [1], count: 1 }),
        ContractMismatch,
    );

    assert.equal(toPrefix({ prefix: 'group', label: 'Group', created_at: 'now' }).prefix, 'group');
    assert.throws(() => toPrefix({ label: 'x' }), ContractMismatch);

    const system = toSystem({
        version: '0.3',
        backend: { type: 'sqlite' },
        tokens: { configured: 2 },
    });
    assert.equal(system.tokens.configured, 2);
    assert.throws(
        () => toSystem({ version: '0.3', backend: {}, tokens: { configured: '2' } }),
        ContractMismatch,
    );

    assert.equal(toBucket({ time: '12:00:00', count: 1 }).count, 1);
    assert.throws(() => toBucket({ time: '12:00:00' }), ContractMismatch);
});
