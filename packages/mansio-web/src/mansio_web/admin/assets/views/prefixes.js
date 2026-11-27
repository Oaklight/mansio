// Prefixes: registered custom channel prefixes.

import { api } from '../api.js';
import { formatDateTime } from '../format.js';
import {
    button,
    card,
    confirmAction,
    dataTable,
    el,
    emptyState,
    errorState,
    modal,
    pageHeader,
    textField,
    toast,
} from '../ui.js';

function registerDialog(onCreated) {
    const prefix = textField({ label: 'Prefix', placeholder: 'group' });
    const label = textField({ label: 'Label', placeholder: 'Group channels' });
    let dialog;
    dialog = modal({
        title: 'Register prefix',
        body: [
            prefix,
            label,
            el('p', { className: 'muted', text: '2–32 lowercase letters/digits, starting with a letter.' }),
        ],
        actions: [
            { label: 'Cancel', onClick: () => dialog.close() },
            {
                label: 'Register',
                variant: 'primary',
                onClick: async () => {
                    try {
                        await api.createPrefix(
                            prefix.querySelector('input').value.trim(),
                            label.querySelector('input').value.trim(),
                        );
                        dialog.close();
                        toast('Prefix registered');
                        onCreated();
                    } catch (error) {
                        toast(error.message || 'Failed to register prefix', 'error');
                    }
                },
            },
        ],
    });
    dialog.open();
}

export async function render(route, ctx) {
    let data;
    try {
        data = await api.listPrefixes();
    } catch (error) {
        return { main: errorState(error, () => ctx.refresh()), detail: null };
    }

    const remove = async (entry) => {
        const confirmed = await confirmAction(`Delete prefix "${entry.prefix}"?`, {
            title: 'Delete prefix',
            confirmLabel: 'Delete',
        });
        if (!confirmed) return;
        try {
            await api.deletePrefix(entry.prefix);
            toast('Prefix deleted');
            ctx.refresh();
        } catch (error) {
            toast(error.message || 'Failed to delete prefix', 'error');
        }
    };

    const main = el(
        'div',
        {},
        pageHeader('Prefixes', {
            subtitle: data.enabled ? `${data.prefixes.length} custom prefixes` : 'Token store not configured',
            actions: data.enabled
                ? [{ label: 'Register prefix', icon: 'plus', variant: 'primary', onClick: () => registerDialog(() => ctx.refresh()) }]
                : [],
        }),
        card(
            'Registered prefixes',
            data.enabled
                ? dataTable({
                      columns: [
                          { label: 'Prefix', render: (entry) => el('span', { className: 'mono', text: entry.prefix }) },
                          { label: 'Label', render: (entry) => entry.label || el('span', { className: 'muted', text: '—' }) },
                          { label: 'Created', render: (entry) => formatDateTime(entry.created_at) },
                          {
                              label: '',
                              render: (entry) =>
                                  button({
                                      label: 'Delete',
                                      variant: 'danger',
                                      icon: 'trash',
                                      onClick: () => remove(entry),
                                  }),
                          },
                      ],
                      rows: data.prefixes,
                      empty: emptyState(
                          'No custom prefixes',
                          'Register one to allow custom channel namespaces.',
                          data.enabled
                              ? button({ label: 'Register prefix', variant: 'primary', onClick: () => registerDialog(() => ctx.refresh()) })
                              : null,
                      ),
                      rowKey: (entry) => entry.prefix,
                  })
                : emptyState('Prefixes are disabled', 'Start the server with a token store to manage prefixes.'),
        ),
    );
    return { main, detail: null };
}
