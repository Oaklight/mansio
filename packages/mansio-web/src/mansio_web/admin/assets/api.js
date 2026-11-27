// Admin API client. Every response goes through the generated contracts and
// the models adapters; views never index raw JSON.

import {
    ContractMismatch,
    parseAdminChannelDetail,
    parseAdminChannelsResult,
    parseAdminMessagesResult,
    parseAdminPrefixCreatedResult,
    parseAdminPrefixDeletedResult,
    parseAdminPrefixesResult,
    parseAdminStatsResult,
    parseAdminSubscriptionsResult,
    parseAdminSystemResult,
    parseAdminThroughputResult,
    parseAdminTokenCreatedResult,
    parseAdminTokenRotatedResult,
    parseAdminTokensResult,
    parseAdminUserDeletedResult,
    parseAdminUserDetailResult,
    parseAdminUserRegisteredResult,
    parseAdminUserTokensResult,
    parseAdminUsersResult,
    parseErrorEnvelope,
} from './contracts.js';
import {
    toBucket,
    toChannelDetail,
    toChannelSummary,
    toCreatedToken,
    toMessage,
    toPrefix,
    toStats,
    toSubscription,
    toSystem,
    toToken,
    toUser,
} from './models.js';

export class ApiError extends Error {
    constructor(status, error, message) {
        super(message || error);
        this.name = 'ApiError';
        this.status = status;
        this.error = error;
    }
}

export class AdminAPI {
    constructor() {
        this.baseUrl = window.location.origin;
        this.onUnauthorized = null;
        this.onConnection = null;
    }

    async request(path, options = {}) {
        const method = options.method || 'GET';
        const document = `${method} ${path.split('?')[0]}`;
        let resp;
        try {
            resp = await fetch(this.baseUrl + path, {
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                ...options,
            });
        } catch (error) {
            if (this.onConnection) this.onConnection(false);
            throw new ApiError(0, 'Network Error', error.message);
        }
        if (this.onConnection) this.onConnection(true);

        let body = null;
        if (resp.status !== 204) {
            try {
                body = await resp.json();
            } catch {
                body = null;
            }
        }

        if (resp.status === 401) {
            if (this.onUnauthorized) this.onUnauthorized();
            throw new ApiError(401, 'Unauthorized', 'Admin authentication required');
        }
        if (!resp.ok) {
            const envelope = body
                ? parseErrorEnvelope(body, document)
                : { error: `HTTP ${resp.status}`, message: resp.statusText };
            throw new ApiError(resp.status, envelope.error, envelope.message);
        }
        if (body === null) {
            throw new ContractMismatch(`${document}: empty response body`);
        }
        return body;
    }

    // ── Session ────────────────────────────────────────────────

    async login(password) {
        const resp = await fetch(this.baseUrl + '/api/login', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password }),
        });
        let data = {};
        try {
            data = await resp.json();
        } catch {
            data = {};
        }
        return { ok: resp.ok, data };
    }

    async logout() {
        await fetch(this.baseUrl + '/api/logout', {
            method: 'POST',
            credentials: 'same-origin',
        });
    }

    async checkAuth() {
        const resp = await fetch(this.baseUrl + '/api/auth-check', {
            credentials: 'same-origin',
        });
        return resp.json();
    }

    // ── Reads ──────────────────────────────────────────────────

    async getStats() {
        return toStats(parseAdminStatsResult(await this.request('/api/stats')));
    }

    async getThroughput() {
        const data = parseAdminThroughputResult(await this.request('/api/stats/throughput'));
        return {
            window_seconds: data.window_seconds,
            buckets: data.buckets.map((bucket) => toBucket(bucket)),
        };
    }

    async getChannels() {
        const data = parseAdminChannelsResult(await this.request('/api/channels'));
        return data.channels.map((channel) => toChannelSummary(channel));
    }

    async getChannel(name) {
        const path = `/api/channels/${encodeURIComponent(name)}`;
        return toChannelDetail(parseAdminChannelDetail(await this.request(path)));
    }

    async getMessages(params = {}) {
        const query = new URLSearchParams();
        for (const [key, value] of Object.entries(params)) {
            if (value !== null && value !== undefined && value !== '') {
                query.set(key, value);
            }
        }
        const suffix = query.toString() ? `?${query.toString()}` : '';
        const data = parseAdminMessagesResult(await this.request(`/api/messages${suffix}`));
        return { count: data.count, messages: data.messages.map((row) => toMessage(row)) };
    }

    async publishMessage(payload) {
        const raw = await this.request('/api/messages', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
        if (raw.success !== true || typeof raw.message_id !== 'string') {
            throw new ContractMismatch('POST /api/messages: expected success and message_id');
        }
        return { success: true, message_id: raw.message_id };
    }

    async getSubscriptions() {
        const data = parseAdminSubscriptionsResult(await this.request('/api/subscriptions'));
        return {
            total: data.total,
            channels: data.channels.map((channel) => toSubscription(channel)),
        };
    }

    async getSystem() {
        return toSystem(parseAdminSystemResult(await this.request('/api/system')));
    }

    // ── Users ──────────────────────────────────────────────────

    async listUsers() {
        const data = parseAdminUsersResult(await this.request('/api/users'));
        return { users: data.users.map((user) => toUser(user)), enabled: data.enabled };
    }

    async registerUser(userId, label) {
        const raw = await this.request('/api/users', {
            method: 'POST',
            body: JSON.stringify({ user_id: userId, label }),
        });
        const data = parseAdminUserRegisteredResult(raw);
        return { user_id: data.user_id, token: toCreatedToken(data.token) };
    }

    async getUser(userId) {
        const path = `/api/users/${encodeURIComponent(userId)}`;
        const data = parseAdminUserDetailResult(await this.request(path));
        return {
            user_id: data.user_id,
            tokens: data.tokens.map((token) => toToken(token)),
            status: data.status,
            last_seen: data.last_seen ?? null,
        };
    }

    async deleteUser(userId) {
        const path = `/api/users/${encodeURIComponent(userId)}`;
        const data = parseAdminUserDeletedResult(await this.request(path, { method: 'DELETE' }));
        return { ok: data.ok, deleted_tokens: data.deleted_tokens };
    }

    async listUserTokens(userId) {
        const path = `/api/users/${encodeURIComponent(userId)}/tokens`;
        const data = parseAdminUserTokensResult(await this.request(path));
        return { user_id: data.user_id, tokens: data.tokens.map((token) => toToken(token)) };
    }

    async createUserToken(userId, label) {
        const path = `/api/users/${encodeURIComponent(userId)}/tokens`;
        const raw = await this.request(path, {
            method: 'POST',
            body: JSON.stringify({ label }),
        });
        const data = parseAdminTokenCreatedResult(raw);
        return { token: toCreatedToken(data.token) };
    }

    // ── Tokens ─────────────────────────────────────────────────

    async listTokens() {
        const data = parseAdminTokensResult(await this.request('/api/tokens'));
        return { tokens: data.tokens.map((token) => toToken(token)), enabled: data.enabled };
    }

    async createToken(userId, label, supertoken) {
        const body = { label };
        if (supertoken) body.supertoken = true;
        else body.user_id = userId;
        const data = parseAdminTokenCreatedResult(
            await this.request('/api/tokens', { method: 'POST', body: JSON.stringify(body) }),
        );
        return { supertoken: data.supertoken ?? false, token: toCreatedToken(data.token) };
    }

    async deleteToken(tokenId) {
        const path = `/api/tokens/${encodeURIComponent(tokenId)}`;
        return this.request(path, { method: 'DELETE' });
    }

    async rotateToken(tokenId) {
        const path = `/api/tokens/${encodeURIComponent(tokenId)}/rotate`;
        const data = parseAdminTokenRotatedResult(await this.request(path, { method: 'POST' }));
        return { token: toCreatedToken(data.token) };
    }

    // ── Prefixes ───────────────────────────────────────────────

    async listPrefixes() {
        const data = parseAdminPrefixesResult(await this.request('/api/prefixes'));
        return { prefixes: data.prefixes.map((prefix) => toPrefix(prefix)), enabled: data.enabled };
    }

    async createPrefix(prefix, label) {
        const data = parseAdminPrefixCreatedResult(
            await this.request('/api/prefixes', {
                method: 'POST',
                body: JSON.stringify({ prefix, label }),
            }),
        );
        return { prefix: toPrefix(data.prefix) };
    }

    async deletePrefix(prefix) {
        const path = `/api/prefixes/${encodeURIComponent(prefix)}`;
        const data = parseAdminPrefixDeletedResult(await this.request(path, { method: 'DELETE' }));
        return { ok: data.ok, deleted: data.deleted };
    }
}

export const api = new AdminAPI();
