// Overview: counters, throughput chart, channel breakdown.

import { api } from '../api.js';
import { formatCount, formatDateTime } from '../format.js';
import { card, chartCanvas, dataTable, emptyState, el, errorState, pageHeader, statGrid } from '../ui.js';

function chartColors() {
    const styles = getComputedStyle(document.documentElement);
    return {
        grid: styles.getPropertyValue('--border').trim() || '#E8E5E0',
        label: styles.getPropertyValue('--text-secondary').trim() || '#81807C',
        line: styles.getPropertyValue('--accent').trim() || '#2B8A7E',
        fill: styles.getPropertyValue('--accent-light').trim() || 'rgba(43,138,126,0.08)',
    };
}

function drawChart(canvas, buckets) {
    const context = canvas.getContext('2d');
    if (!context) return;
    const ratio = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(rect.width, 1) * ratio;
    canvas.height = Math.max(rect.height, 1) * ratio;
    context.scale(ratio, ratio);
    const width = rect.width;
    const height = rect.height;
    context.clearRect(0, 0, width, height);

    const counts = buckets.map((bucket) => bucket.count);
    const maxCount = Math.max(...counts, 1);
    const padLeft = 38;
    const padRight = 12;
    const padTop = 12;
    const padBottom = 22;
    const chartWidth = width - padLeft - padRight;
    const chartHeight = height - padTop - padBottom;
    const colors = chartColors();

    context.strokeStyle = colors.grid;
    context.lineWidth = 0.5;
    context.fillStyle = colors.label;
    context.font = '10px sans-serif';
    context.textAlign = 'right';
    for (let index = 0; index <= 4; index += 1) {
        const y = padTop + (chartHeight / 4) * index;
        context.beginPath();
        context.moveTo(padLeft, y);
        context.lineTo(width - padRight, y);
        context.stroke();
        context.fillText(String(Math.round(maxCount * (1 - index / 4))), padLeft - 6, y + 3);
    }

    context.textAlign = 'center';
    for (let index = 0; index < 60; index += 10) {
        const x = padLeft + (index / 59) * chartWidth;
        context.fillText(`${60 - index}s`, x, height - 4);
    }
    context.fillText('now', padLeft + chartWidth, height - 4);

    if (counts.length < 2) return;
    context.strokeStyle = colors.line;
    context.lineWidth = 2;
    context.lineJoin = 'round';
    context.beginPath();
    counts.forEach((count, index) => {
        const x = padLeft + (index / (counts.length - 1)) * chartWidth;
        const y = padTop + chartHeight - (count / maxCount) * chartHeight;
        if (index === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
    });
    context.stroke();
    context.lineTo(padLeft + chartWidth, padTop + chartHeight);
    context.lineTo(padLeft, padTop + chartHeight);
    context.closePath();
    context.fillStyle = colors.fill;
    context.fill();
}

function build(stats, throughput) {
    const canvas = chartCanvas();
    const breakdown = stats.channel_breakdown.filter(
        (entry) => !entry.channel.startsWith('_system:'),
    );
    const main = el(
        'div',
        {},
        pageHeader('Overview', {
            subtitle: `Bus health at a glance · updated ${new Date().toLocaleTimeString()}`,
        }),
        statGrid([
            { label: 'Messages', value: formatCount(stats.total_messages) },
            { label: 'Channels', value: formatCount(stats.total_channels) },
            { label: 'Senders', value: formatCount(stats.total_senders) },
            { label: 'Subscriptions', value: formatCount(stats.active_subscriptions) },
        ]),
        card(`Throughput · last ${throughput.window_seconds}s`, canvas),
        card(
            'Channel breakdown',
            breakdown.length
                ? dataTable({
                      columns: [
                          {
                              label: 'Channel',
                              render: (row) => el('strong', { className: 'mono', text: row.channel }),
                          },
                          {
                              label: 'Messages',
                              className: 'num',
                              render: (row) => formatCount(row.message_count),
                          },
                          {
                              label: 'Senders',
                              className: 'num',
                              render: (row) => formatCount(row.sender_count),
                          },
                          {
                              label: 'Last message',
                              render: (row) => formatDateTime(row.last_message_time),
                          },
                      ],
                      rows: breakdown,
                      rowKey: (row) => row.channel,
                  })
                : emptyState('No channels yet', 'Publish a message to create the first channel.'),
        ),
    );
    // Draw after the node is in the document.
    requestAnimationFrame(() => drawChart(canvas, throughput.buckets));
    return { main, detail: null };
}

export async function render() {
    try {
        const [stats, throughput] = await Promise.all([api.getStats(), api.getThroughput()]);
        return build(stats, throughput);
    } catch (error) {
        return { main: errorState(error), detail: null };
    }
}
