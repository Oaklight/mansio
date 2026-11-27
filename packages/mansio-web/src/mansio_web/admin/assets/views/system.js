// System: version, backend details and token count.

import { api } from '../api.js';
import { card, el, errorState, kvTable, pageHeader, statGrid } from '../ui.js';

function backendRows(backend) {
    const rows = [['Type', backend.type || 'unknown']];
    if (backend.total_messages !== undefined) rows.push(['Messages', String(backend.total_messages)]);
    if (backend.total_channels !== undefined && backend.total_channels !== null) {
        rows.push(['Channels', String(backend.total_channels)]);
    }
    if (backend.type === 'sqlite') {
        rows.push(['Database', backend.db_path || '—']);
        rows.push(['Journal mode', backend.journal_mode || '—']);
        if (backend.db_size_mb !== undefined) rows.push(['Database size', `${backend.db_size_mb} MB`]);
        if (backend.file_size_bytes) rows.push(['File size', `${backend.file_size_mb} MB`]);
        if (backend.wal_size_bytes) rows.push(['WAL size', `${backend.wal_size_mb} MB`]);
        if (backend.freelist_pages !== undefined) rows.push(['Freelist pages', String(backend.freelist_pages)]);
    } else if (backend.type === 'memory' && backend.estimated_size_mb !== undefined) {
        rows.push(['Estimated size', `${backend.estimated_size_mb} MB`]);
    }
    return rows;
}

export async function render(route, ctx) {
    let data;
    try {
        data = await api.getSystem();
    } catch (error) {
        return { main: errorState(error, () => ctx.refresh()), detail: null };
    }

    const main = el(
        'div',
        {},
        pageHeader('System', { subtitle: 'Process and backend information' }),
        statGrid([
            { label: 'Version', value: data.version },
            { label: 'Backend', value: data.backend.type || 'unknown' },
            { label: 'Tokens', value: String(data.tokens.configured) },
        ]),
        card('Backend', kvTable(backendRows(data.backend))),
    );
    return { main, detail: null };
}
