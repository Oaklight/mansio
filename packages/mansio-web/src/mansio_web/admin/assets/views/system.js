import { api } from '../api.js';
import { esc } from '../util.js';

/* ============== System Tab ============== */
export async function loadSystem() {
    try {
        const data = await api.getSystemInfo();
        const b = data.backend || {};
        const container = document.getElementById('systemInfo');
        let backendRows = '';
        if (b.type === 'sqlite') {
            backendRows = `
                <tr><td>DB Path</td><td style="font-family:monospace">${esc(b.db_path)}</td></tr>
                <tr><td>Journal Mode</td><td>${esc(b.journal_mode)}</td></tr>
                <tr><td>DB Size</td><td>${b.db_size_mb} MB (${(b.db_size_bytes || 0).toLocaleString()} bytes)</td></tr>
                ${b.file_size_bytes ? `<tr><td>File Size</td><td>${b.file_size_mb} MB</td></tr>` : ''}
                ${b.wal_size_bytes ? `<tr><td>WAL Size</td><td>${b.wal_size_mb} MB</td></tr>` : ''}
                <tr><td>Freelist Pages</td><td>${b.freelist_pages}</td></tr>
            `;
        } else if (b.type === 'memory') {
            backendRows = `<tr><td>Estimated Size</td><td>${b.estimated_size_mb} MB</td></tr>`;
        }
        container.innerHTML = `
            <div class="stats-grid" style="grid-template-columns:repeat(3,1fr)">
                <div class="stat-card">
                    <div class="stat-label">Version</div>
                    <div class="stat-value" style="font-size:1.5rem">${esc(data.version)}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Backend</div>
                    <div class="stat-value" style="font-size:1.5rem">${esc(b.type || 'unknown')}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Tokens</div>
                    <div class="stat-value" style="font-size:1.5rem">${data.tokens.configured}</div>
                </div>
            </div>
            <div class="table-container">
                <div class="table-title" style="padding-bottom:8px">Backend Details</div>
                <table>
                    <thead><tr><th>Property</th><th>Value</th></tr></thead>
                    <tbody>
                        <tr><td>Type</td><td><span class="badge">${esc(b.type)}</span></td></tr>
                        <tr><td>Total Messages</td><td>${(b.total_messages || 0).toLocaleString()}</td></tr>
                        <tr><td>Total Channels</td><td>${b.total_channels == null ? 'unknown' : b.total_channels}</td></tr>
                        ${backendRows}
                    </tbody>
                </table>
            </div>`;
    } catch (e) {
        document.getElementById('systemInfo').textContent = 'Failed to load system info';
    }
}
