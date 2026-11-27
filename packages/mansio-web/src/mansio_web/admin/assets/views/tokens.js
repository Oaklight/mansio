import { api } from '../api.js';
import { esc, formatTime, showToast } from '../util.js';

/* ============== Token Management ============== */
export async function loadTokens() {
    try {
        const data = await api.listTokens();
        const tbody = document.getElementById('tokenList');
        const status = document.getElementById('tokenStatus');
        status.textContent = data.enabled
            ? `${data.tokens.length} token(s) configured`
            : 'Token auth not configured';
        if (data.tokens.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="color:var(--text-secondary);text-align:center;padding:40px">No tokens yet — API rejects all requests until a token is created</td></tr>';
            return;
        }
        tbody.innerHTML = data.tokens.map(t => `
            <tr>
                <td style="font-family:monospace;font-size:0.8rem">${esc(t.id)}</td>
                <td>${t.user_id ? esc(t.user_id) : '<em style="color:var(--warning)">supertoken</em>'}</td>
                <td>${esc(t.label)}</td>
                <td style="font-family:monospace;font-size:0.8rem">${esc(t.token_prefix)}…</td>
                <td style="font-size:0.8rem;color:var(--text-secondary)">${formatTime(t.created_at)}</td>
                <td style="font-size:0.8rem;color:var(--text-secondary)">${t.last_used_at ? formatTime(t.last_used_at) : '-'}</td>
                <td>
                    <button class="btn btn-secondary btn-sm-action" onclick="rotateToken('${t.id}')">Rotate</button>
                    <button class="btn btn-danger btn-sm-action" onclick="deleteToken('${t.id}')">Delete</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('Tokens load error:', e);
    }
}

export function showCreateTokenModal() {
    document.getElementById('createTokenModal').classList.remove('hidden');
    document.getElementById('newTokenAgentId').value = '';
    document.getElementById('newTokenLabel').value = '';
    document.getElementById('newTokenSupertoken').checked = false;
    syncSupertokenInput();
    document.getElementById('newTokenAgentId').focus();
}

export function syncSupertokenInput() {
    const supertoken = document.getElementById('newTokenSupertoken').checked;
    const input = document.getElementById('newTokenAgentId');
    input.disabled = supertoken;
    if (supertoken) {
        input.value = '';
    } else {
        input.focus();
    }
}

export function hideCreateTokenModal() {
    document.getElementById('createTokenModal').classList.add('hidden');
}

export async function submitCreateToken() {
    const agentId = document.getElementById('newTokenAgentId').value.trim();
    const label = document.getElementById('newTokenLabel').value.trim();
    const supertoken = document.getElementById('newTokenSupertoken').checked;
    if (!supertoken && !agentId) {
        showToast('Agent ID is required unless creating a supertoken', 'error');
        return;
    }
    try {
        const result = await api.createToken(agentId, label, supertoken);
        if (!result.ok || !result.token) {
            showToast(result.message || 'Failed to create token', 'error');
            return;
        }
        hideCreateTokenModal();
        showTokenReveal(result.token.token, 'New token created — copy it now, it won\'t be shown again:');
        showToast('Token created for ' + (supertoken ? 'supertoken' : agentId), 'success');
        loadTokens();
    } catch (e) {
        showToast(e.message || 'Failed to create token', 'error');
    }
}

export async function deleteToken(id) {
    if (!confirm('Delete this token? Agents using it will be locked out immediately.')) return;
    try {
        await api.deleteToken(id);
        showToast('Token deleted', 'success');
        loadTokens();
    } catch (e) {
        showToast('Failed to delete token', 'error');
    }
}

export async function rotateToken(id) {
    if (!confirm('Rotate this token? The old value will stop working immediately.')) return;
    try {
        const result = await api.rotateToken(id);
        if (result.ok && result.token) {
            showTokenReveal(result.token.token, 'Token rotated — copy the new value:');
            showToast('Token rotated', 'success');
        }
        loadTokens();
    } catch (e) {
        showToast('Failed to rotate token', 'error');
    }
}

export function showTokenReveal(tokenValue, label) {
    const area = document.getElementById('tokenRevealArea');
    area.innerHTML = `
        <div class="token-reveal">
            <div class="token-reveal-label"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#E65100" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:text-bottom"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg> ${esc(label)}</div>
            <div class="token-reveal-value">${esc(tokenValue)}</div>
            <button class="btn btn-primary btn-sm btn-icon" onclick="copyToken('${esc(tokenValue)}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy to Clipboard</button>
            <button class="btn btn-secondary btn-sm" onclick="this.parentElement.remove()" style="margin-left:8px">Dismiss</button>
        </div>
    `;
}

export function copyToken(token) {
    navigator.clipboard.writeText(token).then(
        () => showToast('Token copied to clipboard', 'success'),
        () => showToast('Failed to copy — select and copy manually', 'error')
    );
}
