import { api } from '../api.js';
import { esc, formatTime, getChartColors } from '../util.js';

/* ============== Dashboard ============== */
export async function loadDashboard() {
    try {
        const [stats, tp] = await Promise.all([api.getStats(), api.getThroughput()]);

        document.getElementById('statMessages').textContent = stats.total_messages.toLocaleString();
        document.getElementById('statChannels').textContent = stats.total_channels;
        document.getElementById('statSenders').textContent = stats.total_senders;
        document.getElementById('statSubs').textContent = stats.active_subscriptions;

        // Channel breakdown table
        const tbody = document.getElementById('channelBreakdown');
        tbody.innerHTML = stats.channel_breakdown.filter(ch => !ch.channel.startsWith('_system:')).map(ch => `
            <tr>
                <td><strong>${esc(ch.channel)}</strong></td>
                <td>${ch.message_count.toLocaleString()}</td>
                <td>${ch.sender_count}</td>
                <td style="color:var(--text-secondary);font-size:0.8rem">${formatTime(ch.last_message_time)}</td>
            </tr>
        `).join('');

        // Throughput chart
        drawThroughputChart(tp.buckets);
    } catch (e) {
        console.error('Dashboard load error:', e);
    }
}

/* ============== Throughput Chart ============== */
export function drawThroughputChart(buckets) {
    const canvas = document.getElementById('throughputChart');
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    const W = rect.width;
    const H = rect.height;

    ctx.clearRect(0, 0, W, H);

    const counts = buckets.map(b => b.count);
    const maxCount = Math.max(...counts, 1);

    const padLeft = 40, padRight = 10, padTop = 10, padBottom = 24;
    const chartW = W - padLeft - padRight;
    const chartH = H - padTop - padBottom;

    // Theme-aware chart colors
    const cc = getChartColors();

    // Grid lines
    ctx.strokeStyle = cc.grid;
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
        const y = padTop + (chartH / 4) * i;
        ctx.beginPath();
        ctx.moveTo(padLeft, y);
        ctx.lineTo(W - padRight, y);
        ctx.stroke();

        ctx.fillStyle = cc.label;
        ctx.font = '10px sans-serif';
        ctx.textAlign = 'right';
        const val = Math.round(maxCount * (1 - i / 4));
        ctx.fillText(val.toString(), padLeft - 6, y + 3);
    }

    // X-axis labels
    ctx.fillStyle = cc.label;
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'center';
    for (let i = 0; i < 60; i += 10) {
        const x = padLeft + (i / 59) * chartW;
        ctx.fillText((60 - i) + 's', x, H - 4);
    }
    ctx.fillText('now', padLeft + chartW, H - 4);

    // Line
    ctx.strokeStyle = cc.line;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    for (let i = 0; i < counts.length; i++) {
        const x = padLeft + (i / (counts.length - 1)) * chartW;
        const y = padTop + chartH - (counts[i] / maxCount) * chartH;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Fill area
    ctx.lineTo(padLeft + chartW, padTop + chartH);
    ctx.lineTo(padLeft, padTop + chartH);
    ctx.closePath();
    ctx.fillStyle = cc.fill;
    ctx.fill();
}
