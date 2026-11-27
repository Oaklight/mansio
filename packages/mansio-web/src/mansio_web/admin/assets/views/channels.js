import { api } from '../api.js';
import { esc, formatTime, getSenderColor } from '../util.js';

/* ============== Channels Tab ============== */
let channelData = [];

let channelView = 'tree';

const expandedGroups = new Set();

export function _chPrefix(name) {
    const i = name.indexOf(':');
    return i > 0 ? name.substring(0, i) : '';
}

export function _chType(name) {
    const p = _chPrefix(name);
    if (p === '_system') return 'system';
    if (['dm','notebook','memory','broadcast'].includes(p)) return p;
    return 'custom';
}

export function _chAgents(ch) {
    const parts = ch.name.split(':').slice(1);
    return parts.length ? parts : [];
}

export function _allAgents(channels) {
    const agents = new Set();
    for (const ch of channels) {
        for (const a of _chAgents(ch)) agents.add(a);
    }
    return [...agents].sort();
}

export function _filterChannels() {
    const search = document.getElementById('chSearch').value.toLowerCase();
    const typeFilter = document.getElementById('chTypeFilter').value;
    const agentFilter = document.getElementById('chAgentFilter').value;
    const showSystem = document.getElementById('chShowSystem').checked;
    return channelData.filter(ch => {
        if (!showSystem && _chType(ch.name) === 'system') return false;
        if (typeFilter !== 'all' && _chType(ch.name) !== typeFilter) return false;
        if (agentFilter && !ch.name.split(':').includes(agentFilter)) return false;
        if (search && !ch.name.toLowerCase().includes(search)) return false;
        return true;
    });
}

export function _chRow(ch) {
    const nameJson = JSON.stringify(ch.name);
    return `<tr>
        <td><strong class="clickable" style="color:var(--accent)" onclick='openChannelDetail(${nameJson})'>${esc(ch.name)}</strong></td>
        <td>${ch.message_count}</td><td>${ch.sender_count}</td>
        <td>${ch.subscription_count}</td>
        <td style="color:var(--text-secondary);font-size:0.8rem">${formatTime(ch.last_message_time)}</td>
    </tr>`;
}

export function _renderTree(filtered) {
    const groups = {};
    const ungrouped = [];
    for (const ch of filtered) {
        const p = _chPrefix(ch.name);
        if (p) {
            (groups[p] = groups[p] || []).push(ch);
        } else {
            ungrouped.push(ch);
        }
    }
    const order = ['dm','notebook','memory','broadcast','_system'];
    const sortedKeys = Object.keys(groups).sort((a, b) => {
        const ia = order.indexOf(a), ib = order.indexOf(b);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    let html = '';
    for (const prefix of sortedKeys) {
        const items = groups[prefix];
        const isOpen = expandedGroups.has(prefix);
        const prefixJson = JSON.stringify(prefix);
        html += `<div class="tree-group">
            <div class="tree-group-header" onclick='toggleTreeGroup(${prefixJson})'>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="transition:transform 0.2s;${isOpen ? 'transform:rotate(90deg)' : ''}"><polyline points="9 18 15 12 9 6"/></svg>
                <span>${esc(prefix)}</span>
                <span class="count">(${items.length})</span>
            </div>
            <div class="tree-group-body${isOpen ? ' open' : ''}">
                <table>${items.map(_chRow).join('')}</table>
            </div>
        </div>`;
    }
    if (ungrouped.length) {
        html += `<div class="table-container"><table>
            <thead><tr><th>Channel</th><th>Messages</th><th>Senders</th><th>Subs</th><th>Last</th></tr></thead>
            <tbody>${ungrouped.map(_chRow).join('')}</tbody>
        </table></div>`;
    }
    return html || '<div style="color:var(--text-secondary);text-align:center;padding:40px">No channels match filters</div>';
}

export function _renderFlat(filtered) {
    if (!filtered.length) return '<div style="color:var(--text-secondary);text-align:center;padding:40px">No channels match filters</div>';
    return `<div class="table-container"><table>
        <thead><tr><th>Channel</th><th>Messages</th><th>Senders</th><th>Subs</th><th>Last</th></tr></thead>
        <tbody>${filtered.map(_chRow).join('')}</tbody>
    </table></div>`;
}

export function _renderAgentView(filtered) {
    const agents = _allAgents(channelData);
    if (!agents.length) return '<div style="color:var(--text-secondary);text-align:center;padding:40px">No agents found</div>';
    return agents.map(agent => {
        const owned = filtered.filter(ch => ch.name === `notebook:${agent}` || ch.name === `memory:${agent}`);
        const participated = filtered.filter(ch => ch.name.split(':').includes(agent) && !owned.includes(ch));
        if (!owned.length && !participated.length) return '';
        return `<div class="agent-card">
            <div class="agent-card-header"><span class="badge">${esc(agent)}</span></div>
            ${owned.length ? `<div class="agent-section-title">Owned</div>
                <table>${owned.map(_chRow).join('')}</table>` : ''}
            ${participated.length ? `<div class="agent-section-title">Participated</div>
                <table>${participated.map(_chRow).join('')}</table>` : ''}
        </div>`;
    }).join('') || '<div style="color:var(--text-secondary);text-align:center;padding:40px">No agents match filters</div>';
}

export function renderChannelView() {
    if (activeChannelDetail) return;  // don't overwrite detail panel
    const filtered = _filterChannels();
    const container = document.getElementById('channelViewContainer');
    if (channelView === 'tree') container.innerHTML = _renderTree(filtered);
    else if (channelView === 'flat') container.innerHTML = _renderFlat(filtered);
    else container.innerHTML = _renderAgentView(filtered);
}

export function setChannelView(view) {
    channelView = view;
    activeChannelDetail = null;
    document.querySelectorAll('.ch-view-btns button').forEach(b => b.classList.remove('active'));
    document.querySelector(`.ch-view-btns button:nth-child(${view === 'tree' ? 1 : view === 'flat' ? 2 : 3})`).classList.add('active');
    renderChannelView();
}

export function toggleTreeGroup(prefix) {
    if (expandedGroups.has(prefix)) expandedGroups.delete(prefix);
    else expandedGroups.add(prefix);
    renderChannelView();
}

let activeChannelDetail = null;

export async function openChannelDetail(name) {
    activeChannelDetail = name;
    const container = document.getElementById('channelViewContainer');
    container.innerHTML = '<div class="ch-detail-panel"><div style="color:var(--text-secondary)">Loading...</div></div>';
    try {
        const ch = await api.getChannel(name);
        const typeBadges = ch.msg_types.map(t =>
            `<span class="badge">${esc(t.msg_type)}</span> <span class="badge badge-count">${t.count}</span>`
        ).join(' ');
        const senderBadges = ch.senders.map(s => `<span class="badge" style="background:${getSenderColor(s)}20;color:${getSenderColor(s)};border:1px solid ${getSenderColor(s)}40">${esc(s)}</span>`).join(' ');

        // Fetch recent messages for preview
        const msgData = await api.getMessages({ channel: name, limit: 20 });
        const msgRows = msgData.messages.map((m, i) => {
            const payload = m.payload || '';
            const short = payload.length > 100 ? payload.substring(0, 100) + '...' : payload;
            const expandable = payload.length > 100;
            const payloadId = `detail-payload-${i}`;
            return `
            <tr>
                <td style="font-size:0.8rem;color:var(--text-secondary);white-space:nowrap;width:80px">${formatTime(m.timestamp)}</td>
                <td style="width:auto;white-space:nowrap"><span class="badge" style="background:${getSenderColor(m.sender)}20;color:${getSenderColor(m.sender)};font-size:0.75rem">${esc(m.sender)}</span></td>
                <td style="width:auto;white-space:nowrap"><span class="msg-type-badge">${esc(m.msg_type)}</span></td>
                <td style="font-size:0.85rem;word-break:break-word">${expandable
                    ? `<span id="${payloadId}-short" class="clickable" style="cursor:pointer" onclick="toggleDetailPayload('${payloadId}')">${esc(short)}</span><div id="${payloadId}-full" style="display:none;white-space:pre-wrap;padding:8px;background:var(--bg-primary);border-radius:var(--radius-sm);font-size:0.8rem;max-height:300px;overflow-y:auto;cursor:pointer" onclick="toggleDetailPayload('${payloadId}')">${esc(payload)}</div>`
                    : `<span>${esc(payload)}</span>`
                }</td>
            </tr>`;
        }).join('');

        container.innerHTML = `
            <div class="ch-detail-panel">
                <div class="ch-detail-back" onclick="closeChannelDetail()">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
                    Back to channels
                </div>
                <div class="ch-detail-name">${esc(name)}</div>
                <div class="ch-detail-stats">
                    <span><strong>${ch.message_count}</strong> messages</span>
                    <span><strong>${ch.senders.length}</strong> senders</span>
                    <span>Last: <strong>${formatTime(ch.last_message_time)}</strong></span>
                </div>
                <div class="detail-grid" style="margin-bottom:16px">
                    <div>
                        <div class="detail-section-title">Senders</div>
                        <div>${senderBadges}</div>
                    </div>
                    <div>
                        <div class="detail-section-title">Message Types</div>
                        <div>${typeBadges}</div>
                    </div>
                </div>
                <div class="detail-section-title">Recent Messages</div>
                <div class="table-container" style="margin-top:4px">
                    <table>
                        <thead><tr><th style="width:80px">Time</th><th style="width:auto">Sender</th><th style="width:auto">Type</th><th>Payload</th></tr></thead>
                        <tbody>${msgRows || '<tr><td colspan="4" style="color:var(--text-secondary);text-align:center">No messages</td></tr>'}</tbody>
                    </table>
                </div>
            </div>
        `;
    } catch (e) {
        container.innerHTML = `<div class="ch-detail-panel"><div style="color:var(--error)">Failed to load channel: ${esc(e.message)}</div></div>`;
    }
}

export function closeChannelDetail() {
    activeChannelDetail = null;
    renderChannelView();
}

export async function loadChannels() {
    try {
        const data = await api.getChannels();
        channelData = data.channels;
        // Populate agent filter dropdown
        const agentSelect = document.getElementById('chAgentFilter');
        const currentAgent = agentSelect.value;
        const agents = _allAgents(channelData);
        agentSelect.innerHTML = '<option value="">All agents</option>' +
            agents.map(a => `<option value="${esc(a)}" ${a === currentAgent ? 'selected' : ''}>${esc(a)}</option>`).join('');
        renderChannelView();
    } catch (e) {
        console.error('Channels load error:', e);
    }
}
