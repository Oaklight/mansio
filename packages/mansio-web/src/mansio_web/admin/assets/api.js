import { showLoginOverlay, updateStatus } from './util.js';

/* ============== API Client ============== */
export class MansioAPI {
    constructor() {
        this.baseUrl = window.location.origin;
    }

    async request(path, options = {}) {
        try {
            const resp = await fetch(this.baseUrl + path, {
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                ...options,
            });
            if (resp.status === 401) {
                showLoginOverlay();
                throw new Error('Authentication required');
            }
            updateStatus(true);
            return resp.json();
        } catch (e) {
            if (e.message !== 'Authentication required') {
                updateStatus(false);
            }
            throw e;
        }
    }

    async login(password) {
        const resp = await fetch(this.baseUrl + '/api/login', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password }),
        });
        return { ok: resp.ok, data: await resp.json() };
    }

    async logout() {
        return fetch(this.baseUrl + '/api/logout', {
            method: 'POST', credentials: 'same-origin',
        });
    }

    async checkAuth() {
        const resp = await fetch(this.baseUrl + '/api/auth-check', { credentials: 'same-origin' });
        return resp.json();
    }

    async getStats() { return this.request('/api/stats'); }
    async getThroughput() { return this.request('/api/stats/throughput'); }
    async getChannels() { return this.request('/api/channels'); }
    async getChannel(name) { return this.request('/api/channels/' + encodeURIComponent(name)); }
    async getMessages(params) {
        const q = new URLSearchParams();
        for (const [k, v] of Object.entries(params)) {
            if (v !== null && v !== undefined && v !== '') q.set(k, v);
        }
        return this.request('/api/messages?' + q.toString());
    }
    async publishMessage(data) {
        return this.request('/api/messages', { method: 'POST', body: JSON.stringify(data) });
    }
    async getSubscriptions() { return this.request('/api/subscriptions'); }
    async getSystemInfo() { return this.request('/api/system'); }

    // Token management
    async listTokens() { return this.request('/api/tokens'); }
    async createToken(userId, label, supertoken) {
        const body = { label: label || '' };
        if (supertoken) {
            body.supertoken = true;
        } else {
            body.user_id = userId || '';
        }
        return this.request('/api/tokens', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }
    async deleteToken(id) {
        return this.request('/api/tokens/' + id, { method: 'DELETE' });
    }
    async rotateToken(id) {
        return this.request('/api/tokens/' + id + '/rotate', { method: 'POST' });
    }
}

export const api = new MansioAPI();
