import { api } from '../api.js';
import { esc, formatTime, getSenderColor, showToast } from '../util.js';

/* ============== Messages Tab ============== */
export async function loadMessages() {
    const channel = document.getElementById('msgChannel').value;
    if (!channel) return;

    const sender = document.getElementById('msgSender').value;
    const msgType = document.getElementById('msgType').value;

    try {
        const data = await api.getMessages({
            channel,
            sender: sender || null,
            msg_type: msgType || null,
            limit: 200,
        });

        const container = document.getElementById('messageContainer');
        if (data.messages.length === 0) {
            container.innerHTML = '<div class="message-empty">No messages in this channel</div>';
            return;
        }

        container.innerHTML = data.messages.map((m, i) => {
            const color = getSenderColor(m.sender);
            const isLong = m.payload.length > 200;
            let isJson = false;
            let formattedPayload = '';
            try {
                const parsed = JSON.parse(m.payload);
                formattedPayload = JSON.stringify(parsed, null, 2);
                isJson = true;
            } catch(e) {}

            const displayPayload = isLong ? m.payload.substring(0, 200) + '...' : m.payload;
            const showExpand = isLong || isJson;

            return `
                <div class="bubble bubble-left">
                    <div class="bubble-sender" style="color:${color}">${esc(m.sender)}</div>
                    <div class="bubble-payload">${esc(displayPayload)}</div>
                    ${showExpand ? `
                        <div class="bubble-payload-expand" onclick="togglePayload(${i})">
                            ${isJson ? 'Show JSON' : 'Show full'}
                        </div>
                        <div class="bubble-payload-full" id="payload-${i}">${esc(isJson ? formattedPayload : m.payload)}</div>
                    ` : ''}
                    <div class="bubble-meta">
                        <span class="msg-type-badge">${esc(m.msg_type)}</span>
                        <span>${formatTime(m.timestamp)}</span>
                        ${m.metadata ? `<span title="${esc(JSON.stringify(m.metadata))}">+meta</span>` : ''}
                    </div>
                </div>
            `;
        }).join('');

        // Scroll to bottom
        container.scrollTop = container.scrollHeight;
    } catch (e) {
        console.error('Messages load error:', e);
    }
}

export function toggleDetailPayload(id) {
    const short = document.getElementById(id + '-short');
    const full = document.getElementById(id + '-full');
    if (!short || !full) return;
    const isExpanded = full.style.display !== 'none';
    short.style.display = isExpanded ? '' : 'none';
    full.style.display = isExpanded ? 'none' : 'block';
}

export function togglePayload(index) {
    const el = document.getElementById('payload-' + index);
    el.classList.toggle('visible');
}

export async function populateChannelSelector() {
    try {
        const data = await api.getChannels();
        const select = document.getElementById('msgChannel');
        const current = select.value;
        const opts = '<option value="">Select channel...</option>' +
            data.channels.map(ch => `<option value="${esc(ch.name)}" ${ch.name === current ? 'selected' : ''}>${esc(ch.name)} (${ch.message_count})</option>`).join('');
        select.innerHTML = opts;
    } catch (e) {
        console.error('Channel selector error:', e);
    }
}

/* ============== Publish ============== */
export function togglePublish() {
    const body = document.getElementById('publishBody');
    const chevron = document.getElementById('publishChevron');
    body.classList.toggle('visible');
    chevron.style.transform = body.classList.contains('visible') ? 'rotate(180deg)' : 'rotate(0deg)';

    // Pre-fill channel from selector
    if (body.classList.contains('visible')) {
        const ch = document.getElementById('msgChannel').value;
        if (ch) document.getElementById('pubChannel').value = ch;
    }
}

export async function publishMessage() {
    const channel = document.getElementById('pubChannel').value.trim();
    const sender = document.getElementById('pubSender').value.trim();
    const msgType = document.getElementById('pubMsgType').value.trim();
    const payload = document.getElementById('pubPayload').value;
    const metaStr = document.getElementById('pubMetadata').value.trim();

    if (!channel || !sender || !msgType || !payload) {
        showToast('All fields except metadata are required', 'error');
        return;
    }

    let metadata = null;
    if (metaStr) {
        try {
            metadata = JSON.parse(metaStr);
        } catch (e) {
            showToast('Invalid metadata JSON', 'error');
            return;
        }
    }

    try {
        const result = await api.publishMessage({ channel, sender, msg_type: msgType, payload, metadata });
        if (result.success) {
            showToast('Message sent: ' + result.message_id.substring(0, 8) + '...', 'success');
            document.getElementById('pubPayload').value = '';
            document.getElementById('pubMetadata').value = '';
            // Refresh messages if same channel is selected
            const sel = document.getElementById('msgChannel');
            if (sel.value === channel) loadMessages();
        }
    } catch (e) {
        showToast('Failed to send message', 'error');
    }
}
