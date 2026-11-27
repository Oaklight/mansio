// Channels: filterable list (flat or grouped) with a detail pane.

import { api } from '../api.js';
import { formatCount, formatDateTime, formatTime } from '../format.js';
import { navigate } from '../router.js';
import { state } from '../state.js';
import {
    badge,
    button,
    dataTable,
    detailPane,
    el,
    emptyState,
    errorState,
    listRow,
    pageHeader,
    replacement,
    searchField,
    selectField,
    toolbar,
} from '../ui.js';

const filters = { search: '', type: 'all', agent: '', system: false };
let viewMode = 'flat';
const collapsed = new Set();

function prefixOf(name) {
    const index = name.indexOf(':');
    return index > 0 ? name.slice(0, index) : '';
}

function typeOf(name) {
    const prefix = prefixOf(name);
    if (prefix === '_system') return 'system';
    if (['dm', 'notebook', 'memory', 'broadcast'].includes(prefix)) return prefix;
    return 'custom';
}

function matches(channel) {
    const type = typeOf(channel.name);
    if (!filters.system && type === 'system') return false;
    if (filters.type !== 'all' && type !== filters.type) return false;
    if (filters.agent && !channel.name.split(':').includes(filters.agent)) return false;
    if (filters.search && !channel.name.toLowerCase().includes(filters.search)) return false;
    return true;
}

function agentsOf(channels) {
    const agents = new Set();
    for (const channel of channels) {
        for (const part of channel.name.split(':').slice(1)) agents.add(part);
    }
    return [...agents].sort();
}

function row(channel, activeName, onNavigate) {
    const active = channel.name === activeName;
    const type = typeOf(channel.name);
    return listRow({
        title: channel.name,
        subtitle: `${formatCount(channel.message_count)} messages · ${formatCount(channel.sender_count)} senders`,
        meta: [
            type === 'system' ? badge('system', 'warning') : badge(type),
            el('span', { text: `last ${formatDateTime(channel.last_message_time)}` }),
        ],
        active,
        onClick: () => onNavigate('channels', { id: channel.name }),
    });
}

function groupedNodes(channels, activeName, onNavigate, onRefresh) {
    const groups = new Map();
    const ungrouped = [];
    for (const channel of channels) {
        const prefix = prefixOf(channel.name);
        if (!prefix) {
            ungrouped.push(channel);
            continue;
        }
        if (!groups.has(prefix)) groups.set(prefix, []);
        groups.get(prefix).push(channel);
    }
    const order = ['dm', 'notebook', 'memory', 'broadcast', '_system'];
    const keys = [...groups.keys()].sort((a, b) => {
        const left = order.indexOf(a);
        const right = order.indexOf(b);
        return (left === -1 ? 99 : left) - (right === -1 ? 99 : right);
    });
    const nodes = [];
    for (const key of keys) {
        const items = groups.get(key);
        const isCollapsed = collapsed.has(key);
        nodes.push(
            el(
                'div',
                { className: 'tree-group-header' },
                button({
                    label: `${isCollapsed ? '▸' : '▾'} ${key}`,
                    variant: 'ghost',
                    ariaLabel: `${isCollapsed ? 'Expand' : 'Collapse'} ${key}`,
                    onClick: () => {
                        if (isCollapsed) collapsed.delete(key);
                        else collapsed.add(key);
                        onRefresh();
                    },
                }),
                badge(`${items.length}`),
            ),
        );
        if (!isCollapsed) {
            nodes.push(...items.map((channel) => row(channel, activeName, onNavigate)));
        }
    }
    nodes.push(...ungrouped.map((channel) => row(channel, activeName, onNavigate)));
    return nodes;
}

async function channelDetail(name, ctx) {
    try {
        const [detail, recent] = await Promise.all([
            api.getChannel(name),
            api.getMessages({ channel: name, limit: 20 }),
        ]);
        const messages = recent.messages
            .slice()
            .reverse()
            .map((message) =>
                el(
                    'div',
                    { className: 'message' },
                    el(
                        'div',
                        { className: 'message-head' },
                        el('span', { className: 'message-sender', text: message.sender }),
                        badge(message.msg_type),
                        el('span', { className: 'message-time', text: formatTime(message.timestamp) }),
                    ),
                    el(
                        'details',
                        {},
                        el('summary', { className: 'message-payload', text: message.payload.slice(0, 160) }),
                        el('pre', { className: 'message-payload', text: message.payload }),
                    ),
                ),
            );
        return detailPane({
            title: detail.name,
            subtitle: `${formatCount(detail.message_count)} messages · last ${formatDateTime(detail.last_message_time)}`,
            onClose: () => navigate('channels'),
            children: [
                el(
                    'div',
                    { className: 'detail-section' },
                    el('div', { className: 'detail-section-title', text: 'Senders' }),
                    el(
                        'div',
                        { className: 'chip-row' },
                        detail.senders.length
                            ? detail.senders.map((sender) => badge(sender, 'accent'))
                            : el('span', { className: 'muted', text: 'None' }),
                    ),
                ),
                el(
                    'div',
                    { className: 'detail-section' },
                    el('div', { className: 'detail-section-title', text: 'Message types' }),
                    el(
                        'div',
                        { className: 'chip-row' },
                        detail.msg_types.map((entry) => badge(`${entry.msg_type} · ${entry.count}`)),
                    ),
                ),
                el(
                    'div',
                    { className: 'detail-section' },
                    el('div', { className: 'detail-section-title', text: 'Recent messages' }),
                    messages.length ? el('div', { className: 'message-list' }, messages) : emptyState('No messages'),
                ),
            ],
        });
    } catch (error) {
        return detailPane({
            title: name,
            onClose: () => navigate('channels'),
            children: errorState(error, () => ctx.refresh()),
        });
    }
}

export async function render(route, ctx) {
    let channels = state.channels;
    try {
        channels = await api.getChannels();
        state.channels = channels;
    } catch (error) {
        return { main: errorState(error, () => ctx.refresh()), detail: null };
    }

    const agents = agentsOf(channels);
    const results = el('div', {});
    const renderResults = () => {
        const filtered = channels.filter(matches);
        if (!filtered.length) {
            replacement(results, emptyState('No channels match', 'Adjust the filters or publish a message.'));
            return;
        }
        if (viewMode === 'tree') {
            replacement(results, el('div', { className: 'list', role: 'list' }, groupedNodes(filtered, route.id, ctx.navigate, ctx.refresh)));
        } else {
            replacement(
                results,
                dataTable({
                    columns: [
                        {
                            label: 'Channel',
                            render: (channel) => el('strong', { className: 'mono', text: channel.name }),
                        },
                        { label: 'Type', render: (channel) => badge(typeOf(channel.name)) },
                        { label: 'Messages', className: 'num', render: (channel) => formatCount(channel.message_count) },
                        { label: 'Senders', className: 'num', render: (channel) => formatCount(channel.sender_count) },
                        { label: 'Subs', className: 'num', render: (channel) => formatCount(channel.subscription_count) },
                        { label: 'Last', render: (channel) => formatDateTime(channel.last_message_time) },
                        {
                            label: '',
                            render: (channel) =>
                                button({
                                    label: 'Open',
                                    variant: 'secondary',
                                    onClick: () => ctx.navigate('channels', { id: channel.name }),
                                }),
                        },
                    ],
                    rows: filtered,
                    rowKey: (channel) => channel.name,
                }),
            );
        }
    };

    const modeButton = (mode, label) =>
        button({
            label,
            variant: viewMode === mode ? 'primary' : 'secondary',
            onClick: () => {
                viewMode = mode;
                ctx.refresh();
            },
        });

    const main = el(
        'div',
        {},
        pageHeader('Channels', {
            subtitle: `${channels.length} channels total`,
            actions: [{ label: 'Messages', icon: 'message', onClick: () => ctx.navigate('messages') }],
        }),
        toolbar([
            searchField({
                placeholder: 'Search channels…',
                value: filters.search,
                onInput: (value) => {
                    filters.search = value.trim().toLowerCase();
                    renderResults();
                },
            }),
            selectField({
                label: 'Type',
                value: filters.type,
                options: [
                    { value: 'all', label: 'All types' },
                    { value: 'custom', label: 'Custom' },
                    { value: 'dm', label: 'DM' },
                    { value: 'notebook', label: 'Notebook' },
                    { value: 'memory', label: 'Memory' },
                    { value: 'broadcast', label: 'Broadcast' },
                    { value: 'system', label: 'System' },
                ],
                onChange: (value) => {
                    filters.type = value;
                    renderResults();
                },
            }),
            selectField({
                label: 'Agent',
                value: filters.agent,
                options: [
                    { value: '', label: 'All agents' },
                    ...agents.map((agent) => ({ value: agent, label: agent })),
                ],
                onChange: (value) => {
                    filters.agent = value;
                    renderResults();
                },
            }),
            el(
                'label',
                { className: 'checkbox-field' },
                el('input', {
                    type: 'checkbox',
                    checked: filters.system,
                    on: {
                        change: (event) => {
                            filters.system = event.target.checked;
                            renderResults();
                        },
                    },
                }),
                'System channels',
            ),
            el('div', { className: 'grow' }),
            modeButton('flat', 'Flat'),
            modeButton('tree', 'Grouped'),
        ]),
        results,
    );
    renderResults();

    let detail = null;
    if (route.id) detail = await channelDetail(route.id, ctx);
    return { main, detail };
}
