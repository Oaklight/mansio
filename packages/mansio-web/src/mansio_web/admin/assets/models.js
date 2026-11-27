// View models for the admin UI.
//
// The envelopes come from contracts.js; this module turns the nested
// objects inside them (token rows, user rows, message summaries) into
// frozen, field-checked objects. Views never index a raw response.

import { ContractMismatch } from './contracts.js';

/**
 * @typedef {Object} Stats
 * @property {number} total_messages
 * @property {number} total_channels
 * @property {number} total_senders
 * @property {number} active_subscriptions
 * @property {Array<ChannelBreakdown>} channel_breakdown
 * @property {Object<string, number>} msg_type_distribution
 *
 * @typedef {Object} ChannelBreakdown
 * @property {string} channel
 * @property {number} message_count
 * @property {number} sender_count
 * @property {string|null} last_message_time
 *
 * @typedef {Object} ThroughputBucket
 * @property {string} time
 * @property {number} count
 *
 * @typedef {Object} ChannelSummary
 * @property {string} name
 * @property {number} message_count
 * @property {number} sender_count
 * @property {number} subscription_count
 * @property {string|null} last_message_time
 *
 * @typedef {Object} MessageTypeCount
 * @property {string} msg_type
 * @property {number} count
 *
 * @typedef {Object} ChannelDetail
 * @property {string} name
 * @property {number} message_count
 * @property {Array<string>} senders
 * @property {Array<MessageTypeCount>} msg_types
 * @property {string|null} last_message_time
 *
 * @typedef {Object} Message
 * @property {string} id
 * @property {string} channel
 * @property {string} sender
 * @property {string} msg_type
 * @property {string} payload
 * @property {string} timestamp
 * @property {Object|null} metadata
 *
 * @typedef {Object} Token
 * @property {string} id
 * @property {string} token_prefix
 * @property {string|null} user_id
 * @property {string} label
 * @property {string} created_at
 * @property {string|null} last_used_at
 *
 * @typedef {Token & {token: string}} CreatedToken
 *
 * @typedef {Object} User
 * @property {string} user_id
 * @property {number} token_count
 * @property {string|null} last_active
 * @property {string} status
 * @property {string|null} last_seen
 *
 * @typedef {Object} Prefix
 * @property {string} prefix
 * @property {string} label
 * @property {string} created_at
 *
 * @typedef {Object} Subscription
 * @property {string} channel
 * @property {Array<string>} subscription_ids
 * @property {number} count
 *
 * @typedef {Object} SystemInfo
 * @property {string} version
 * @property {Object<string, unknown>} backend
 * @property {{configured: number}} tokens
 */

function fail(document, detail) {
    throw new ContractMismatch(`${document}: ${detail}`);
}

function checkObject(raw, document) {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
        fail(document, 'entry must be an object');
    }
    return raw;
}

function stringOf(raw, name, document) {
    const value = raw[name];
    if (typeof value !== 'string') fail(document, `${name} must be a string`);
    return value;
}

function numberField(raw, name, document) {
    const value = raw[name];
    if (typeof value !== 'number' || !Number.isFinite(value)) {
        fail(document, `${name} must be a number`);
    }
    return value;
}

function optionalString(raw, name, document) {
    const value = raw[name];
    if (value === undefined || value === null) return null;
    if (typeof value !== 'string') fail(document, `${name} must be a string or null`);
    return value;
}

function stringArray(raw, name, document) {
    const value = raw[name];
    if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
        fail(document, `${name} must be an array of strings`);
    }
    return Object.freeze(value.slice());
}

function objectOf(raw, name, document) {
    const value = raw[name];
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        fail(document, `${name} must be an object`);
    }
    return Object.freeze({ ...value });
}

/** @returns {Stats} */
export function toStats(raw) {
    const document = 'GET /api/stats';
    checkObject(raw, document);
    const breakdown = raw.channel_breakdown;
    if (!Array.isArray(breakdown)) fail(document, 'channel_breakdown must be an array');
    return Object.freeze({
        total_messages: numberField(raw, 'total_messages', document),
        total_channels: numberField(raw, 'total_channels', document),
        total_senders: numberField(raw, 'total_senders', document),
        active_subscriptions: numberField(raw, 'active_subscriptions', document),
        channel_breakdown: Object.freeze(breakdown.map((item) => toChannelBreakdown(item))),
        msg_type_distribution: Object.freeze({ ...(raw.msg_type_distribution || {}) }),
    });
}

/** @returns {ChannelBreakdown} */
export function toChannelBreakdown(raw) {
    const document = 'GET /api/stats channel_breakdown';
    checkObject(raw, document);
    return Object.freeze({
        channel: stringOf(raw, 'channel', document),
        message_count: numberField(raw, 'message_count', document),
        sender_count: numberField(raw, 'sender_count', document),
        last_message_time: optionalString(raw, 'last_message_time', document),
    });
}

/** @returns {ThroughputBucket} */
export function toBucket(raw) {
    const document = 'GET /api/stats/throughput bucket';
    checkObject(raw, document);
    return Object.freeze({
        time: stringOf(raw, 'time', document),
        count: numberField(raw, 'count', document),
    });
}

/** @returns {ChannelSummary} */
export function toChannelSummary(raw) {
    const document = 'GET /api/channels entry';
    checkObject(raw, document);
    return Object.freeze({
        name: stringOf(raw, 'name', document),
        message_count: numberField(raw, 'message_count', document),
        sender_count: numberField(raw, 'sender_count', document),
        subscription_count: numberField(raw, 'subscription_count', document),
        last_message_time: optionalString(raw, 'last_message_time', document),
    });
}

/** @returns {ChannelDetail} */
export function toChannelDetail(raw) {
    const document = 'GET /api/channels/<name>';
    checkObject(raw, document);
    const types = raw.msg_types;
    if (!Array.isArray(types)) fail(document, 'msg_types must be an array');
    return Object.freeze({
        name: stringOf(raw, 'name', document),
        message_count: numberField(raw, 'message_count', document),
        senders: stringArray(raw, 'senders', document),
        msg_types: Object.freeze(
            types.map((item) => {
                checkObject(item, document);
                return Object.freeze({
                    msg_type: stringOf(item, 'msg_type', document),
                    count: numberField(item, 'count', document),
                });
            }),
        ),
        last_message_time: optionalString(raw, 'last_message_time', document),
    });
}

/** @returns {Message} */
export function toMessage(raw) {
    const document = 'GET /api/messages entry';
    checkObject(raw, document);
    const metadata = raw.metadata;
    if (metadata !== undefined && metadata !== null) {
        if (typeof metadata !== 'object' || Array.isArray(metadata)) {
            fail(document, 'metadata must be an object or null');
        }
    }
    return Object.freeze({
        id: stringOf(raw, 'id', document),
        channel: stringOf(raw, 'channel', document),
        sender: stringOf(raw, 'sender', document),
        msg_type: stringOf(raw, 'msg_type', document),
        payload: stringOf(raw, 'payload', document),
        timestamp: stringOf(raw, 'timestamp', document),
        metadata: metadata === undefined ? null : metadata,
    });
}

/** @returns {Token} */
export function toToken(raw) {
    const document = 'token entry';
    checkObject(raw, document);
    return Object.freeze({
        id: stringOf(raw, 'id', document),
        token_prefix: stringOf(raw, 'token_prefix', document),
        user_id: optionalString(raw, 'user_id', document),
        label: stringOf(raw, 'label', document),
        created_at: stringOf(raw, 'created_at', document),
        last_used_at: optionalString(raw, 'last_used_at', document),
    });
}

/** @returns {CreatedToken} */
export function toCreatedToken(raw) {
    const token = toToken(raw);
    return Object.freeze({
        ...token,
        token: stringOf(raw, 'token', 'created token'),
    });
}

/** @returns {User} */
export function toUser(raw) {
    const document = 'GET /api/users entry';
    checkObject(raw, document);
    return Object.freeze({
        user_id: stringOf(raw, 'user_id', document),
        token_count: numberField(raw, 'token_count', document),
        last_active: optionalString(raw, 'last_active', document),
        status: typeof raw.status === 'string' ? raw.status : 'unknown',
        last_seen: optionalString(raw, 'last_seen', document),
    });
}

/** @returns {Prefix} */
export function toPrefix(raw) {
    const document = 'GET /api/prefixes entry';
    checkObject(raw, document);
    return Object.freeze({
        prefix: stringOf(raw, 'prefix', document),
        label: typeof raw.label === 'string' ? raw.label : '',
        created_at: typeof raw.created_at === 'string' ? raw.created_at : '',
    });
}

/** @returns {Subscription} */
export function toSubscription(raw) {
    const document = 'GET /api/subscriptions channel';
    checkObject(raw, document);
    return Object.freeze({
        channel: stringOf(raw, 'channel', document),
        subscription_ids: stringArray(raw, 'subscription_ids', document),
        count: numberField(raw, 'count', document),
    });
}

/** @returns {SystemInfo} */
export function toSystem(raw) {
    const document = 'GET /api/system';
    checkObject(raw, document);
    const tokens = raw.tokens;
    if (tokens === null || typeof tokens !== 'object' || Array.isArray(tokens)) {
        fail(document, 'tokens must be an object');
    }
    return Object.freeze({
        version: stringOf(raw, 'version', document),
        backend: objectOf(raw, 'backend', document),
        tokens: Object.freeze({ configured: numberField(tokens, 'configured', document) }),
    });
}
