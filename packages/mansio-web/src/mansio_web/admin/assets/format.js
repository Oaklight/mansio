// Pure formatting helpers. No DOM access beyond what is passed in.

const senderColors = {};
const palette = [
    '#2B8A7E', '#C2922E', '#7C3AED', '#DB2777', '#2563EB',
    '#D97706', '#059669', '#DC2626', '#7C3AED', '#9333EA',
];

export function senderColor(sender) {
    if (!senderColors[sender]) {
        const index = Object.keys(senderColors).length % palette.length;
        senderColors[sender] = palette[index];
    }
    return senderColors[sender];
}

export function formatTime(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function formatDateTime(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleString();
}

export function formatUptime(seconds) {
    if (seconds === undefined || seconds === null) return '—';
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days > 0) return `${days}d ${hours}h ${minutes}m`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
}

export function formatCount(value) {
    return typeof value === 'number' ? value.toLocaleString() : '—';
}

export function shortId(id) {
    return typeof id === 'string' && id.length > 8 ? `${id.slice(0, 8)}…` : String(id);
}
