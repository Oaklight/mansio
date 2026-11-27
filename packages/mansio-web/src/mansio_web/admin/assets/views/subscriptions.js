// Subscriptions: active subscription counts per channel.

import { api } from '../api.js';
import { formatCount } from '../format.js';
import { card, dataTable, el, emptyState, errorState, pageHeader, statGrid } from '../ui.js';

export async function render(route, ctx) {
    let data;
    try {
        data = await api.getSubscriptions();
    } catch (error) {
        return { main: errorState(error, () => ctx.refresh()), detail: null };
    }

    const main = el(
        'div',
        {},
        pageHeader('Subscriptions', { subtitle: 'Live SSE subscribers attached to the bus' }),
        statGrid([{ label: 'Active subscriptions', value: formatCount(data.total) }]),
        card(
            'By channel',
            data.channels.length
                ? dataTable({
                      columns: [
                          {
                              label: 'Channel',
                              render: (row) => el('strong', { className: 'mono', text: row.channel }),
                          },
                          {
                              label: 'Subscribers',
                              className: 'num',
                              render: (row) => formatCount(row.count),
                          },
                          {
                              label: 'Subscription IDs',
                              render: (row) =>
                                  el('span', { className: 'mono muted', text: row.subscription_ids.join(', ') }),
                          },
                      ],
                      rows: data.channels,
                      rowKey: (row) => row.channel,
                  })
                : emptyState('No active subscriptions', 'Agents subscribe over the SSE endpoint.'),
        ),
    );
    return { main, detail: null };
}
