import { api } from '../api.js';
import { esc } from '../util.js';

/* ============== Subscriptions Tab ============== */
export async function loadSubscriptions() {
    try {
        const data = await api.getSubscriptions();
        document.getElementById('subTotal').textContent =
            `Total active subscriptions: ${data.total}`;

        const tbody = document.getElementById('subList');
        if (data.channels.length === 0) {
            tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-secondary);text-align:center;padding:40px">No active subscriptions</td></tr>';
            return;
        }
        tbody.innerHTML = data.channels.map(ch => `
            <tr>
                <td><strong>${esc(ch.channel)}</strong></td>
                <td style="font-family:monospace;font-size:0.8rem;word-break:break-all">${ch.subscription_ids.join(', ')}</td>
                <td>${ch.count}</td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('Subscriptions load error:', e);
    }
}
