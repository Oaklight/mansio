import { state } from './state.js';
import { loadDashboard } from './views/dashboard.js';
import { loadChannels } from './views/channels.js';
import { loadMessages } from './views/messages.js';
import { loadSubscriptions } from './views/subscriptions.js';
import { loadTokens } from './views/tokens.js';
import { loadSystem } from './views/system.js';

/** Re-run the loader for the active tab. */
export function refreshCurrentTab() {
    switch (state.currentTab) {
        case 'dashboard': loadDashboard(); break;
        case 'channels': loadChannels(); break;
        case 'messages':
            if (document.getElementById('msgAutoRefresh').checked) loadMessages();
            break;
        case 'subscriptions': loadSubscriptions(); break;
        case 'tokens': loadTokens(); break;
        case 'system': loadSystem(); break;
    }
}
