// Messages: channel browse with a pinned composer.

import { api } from '../api.js';
import { formatTime } from '../format.js';
import { state } from '../state.js';
import {
    badge,
    button,
    card,
    el,
    emptyState,
    errorState,
    loadingState,
    pageHeader,
    searchField,
    selectField,
    textArea,
    textField,
    toast,
    toolbar,
} from '../ui.js';

let liveTimer = null;

function stopLive() {
    if (liveTimer) {
        window.clearInterval(liveTimer);
        liveTimer = null;
    }
}

function messageNode(message) {
    const payload = message.payload || '';
    const isLong = payload.length > 200;
    let isJson = false;
    try {
        JSON.parse(payload);
        isJson = true;
    } catch {
        isJson = false;
    }
    const head = el(
        'div',
        { className: 'message-head' },
        el('span', { className: 'message-sender', text: message.sender }),
        badge(message.msg_type),
        el('span', { className: 'message-time', text: formatTime(message.timestamp) }),
    );
    const body = isLong || isJson
        ? el(
              'details',
              {},
              el('summary', { className: 'message-payload', text: `${payload.slice(0, 200)}${isLong ? '…' : ''}` }),
              el('pre', { className: 'message-payload', text: isJson ? JSON.stringify(JSON.parse(payload), null, 2) : payload }),
          )
        : el('div', { className: 'message-payload', text: payload });
    const meta = message.metadata
        ? el(
              'details',
              { className: 'message-toggle' },
              el('summary', { className: 'muted', text: '+ metadata' }),
              el('pre', { className: 'message-payload', text: JSON.stringify(message.metadata, null, 2) }),
          )
        : null;
    return el('article', { className: 'message' }, head, body, meta);
}

export async function render(route, ctx) {
    stopLive();
    let channels = state.channels;
    if (!channels.length) {
        try {
            channels = await api.getChannels();
            state.channels = channels;
        } catch (error) {
            return { main: errorState(error, () => ctx.refresh()), detail: null };
        }
    }

    const defaultChannel =
        channels.find((channel) => !channel.name.startsWith('_system:'))?.name ||
        channels[0]?.name ||
        '';
    const params = {
        channel: route.query.get('channel') || defaultChannel,
        sender: route.query.get('sender') || '',
        msg_type: route.query.get('msg_type') || '',
    };

    const listNode = el('div', { className: 'message-list' });
    const countLabel = el('span', { className: 'muted' });
    const liveCheckbox = el('input', { type: 'checkbox', 'aria-label': 'Live refresh' });

    async function load() {
        if (!params.channel) {
            listNode.replaceChildren(emptyState('Choose a channel', 'Messages appear here once a channel is selected.'));
            countLabel.textContent = '';
            return;
        }
        try {
            const data = await api.getMessages({
                channel: params.channel,
                sender: params.sender || null,
                msg_type: params.msg_type || null,
                limit: 200,
            });
            countLabel.textContent = `${data.count} message${data.count === 1 ? '' : 's'}`;
            if (!data.messages.length) {
                listNode.replaceChildren(emptyState('No messages', 'Publish one from the composer below.'));
                return;
            }
            listNode.replaceChildren(...data.messages.map((message) => messageNode(message)));
        } catch (error) {
            listNode.replaceChildren(errorState(error, () => load()));
        }
    }

    function syncRoute() {
        ctx.navigate('messages', {
            query: {
                channel: params.channel,
                sender: params.sender,
                msg_type: params.msg_type,
            },
        });
    }

    const channelSelect = selectField({
        label: 'Channel',
        ariaLabel: 'Channel',
        value: params.channel,
        options: channels.map((channel) => ({ value: channel.name, label: `${channel.name} (${channel.message_count})` })),
        onChange: (value) => {
            params.channel = value;
            syncRoute();
        },
    });

    const senderFilter = searchField({
        placeholder: 'Filter sender…',
        value: params.sender,
        onInput: (value) => {
            params.sender = value.trim();
        },
    });
    senderFilter.querySelector('input').addEventListener('change', syncRoute);

    const typeFilter = searchField({
        placeholder: 'Filter msg_type…',
        value: params.msg_type,
        onInput: (value) => {
            params.msg_type = value.trim();
        },
    });
    typeFilter.querySelector('input').addEventListener('change', syncRoute);

    liveCheckbox.addEventListener('change', () => {
        stopLive();
        if (liveCheckbox.checked) {
            liveTimer = window.setInterval(load, 3000);
        }
    });

    // ── Composer ──────────────────────────────────────────────
    const composerChannel = textField({ label: 'Channel', value: params.channel });
    const composerSender = textField({ label: 'Sender', value: 'admin-panel' });
    const composerType = textField({ label: 'Message type', value: 'text' });
    const composerPayload = textArea({
        label: 'Payload',
        placeholder: 'Write a message… (Ctrl+Enter sends)',
        rows: 3,
    });
    const composerMetadata = textArea({
        label: 'Metadata (JSON, optional)',
        placeholder: '{"key": "value"}',
        rows: 2,
    });
    composerPayload.querySelector('textarea').addEventListener('keydown', (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
            event.preventDefault();
            send();
        }
    });

    async function send() {
        const channel = composerChannel.querySelector('input').value.trim();
        const sender = composerSender.querySelector('input').value.trim();
        const msgType = composerType.querySelector('input').value.trim();
        const payload = composerPayload.querySelector('textarea').value;
        const metadataText = composerMetadata.querySelector('textarea').value.trim();
        if (!channel || !sender || !msgType || !payload) {
            toast('Channel, sender, type and payload are required', 'error');
            return;
        }
        let metadata = null;
        if (metadataText) {
            try {
                metadata = JSON.parse(metadataText);
            } catch {
                toast('Metadata must be valid JSON', 'error');
                return;
            }
        }
        try {
            const result = await api.publishMessage({
                channel,
                sender,
                msg_type: msgType,
                payload,
                metadata,
            });
            toast(`Message sent (${result.message_id.slice(0, 8)}…)`);
            composerPayload.querySelector('textarea').value = '';
            composerMetadata.querySelector('textarea').value = '';
            if (!params.channel) {
                params.channel = channel;
                syncRoute();
            }
            await load();
        } catch (error) {
            toast(error.message || 'Failed to send message', 'error');
        }
    }

    const main = el(
        'div',
        {},
        pageHeader('Messages', { subtitle: 'Browse a channel and publish to the bus' }),
        card(
            null,
            [
                toolbar([
                    channelSelect,
                    senderFilter,
                    typeFilter,
                    el('label', { className: 'checkbox-field' }, liveCheckbox, 'Live · 3s'),
                    el('div', { className: 'grow' }),
                    countLabel,
                    button({ label: 'Reload', icon: 'refresh', onClick: () => load() }),
                ]),
                listNode,
                el(
                    'div',
                    { className: 'composer' },
                    el('div', { className: 'composer-fields' }, composerChannel, composerSender, composerType),
                    composerPayload,
                    composerMetadata,
                    el('div', { className: 'composer-footer' }, button({ label: 'Send', variant: 'primary', onClick: () => send() })),
                ),
            ],
            { className: 'messages-card' },
        ),
    );

    listNode.replaceChildren(loadingState('Loading messages…'));
    await load();

    return { main, detail: null, cleanup: stopLive };
}
