import test from 'node:test';
import assert from 'node:assert/strict';

import { setActiveNav } from '../../src/mansio_web/admin/assets/ui.js';

let listenerCalls = 0;

function link(name) {
    const classes = new Set();
    return {
        dataset: { nav: name },
        classList: {
            toggle(className, force) {
                if (force) classes.add(className);
                else classes.delete(className);
            },
            contains(className) {
                return classes.has(className);
            },
        },
        addEventListener() {
            listenerCalls += 1;
        },
    };
}

function root(links) {
    return { querySelectorAll: () => links };
}

function active(links) {
    return links.map((item) => item.classList.contains('active'));
}

test('setActiveNav marks exactly one link active', () => {
    const links = ['overview', 'channels', 'messages'].map(link);
    setActiveNav('channels', root(links));
    assert.deepEqual(active(links), [false, true, false]);
});

test('setActiveNav moves the highlight and survives a re-render', () => {
    const links = ['overview', 'channels'].map(link);
    setActiveNav('overview', root(links));
    setActiveNav('channels', root(links));
    assert.deepEqual(active(links), [false, true]);

    const rerendered = ['overview', 'channels'].map(link);
    setActiveNav('channels', root(rerendered));
    assert.deepEqual(active(rerendered), [false, true]);
});

test('setActiveNav is a no-op before the nav is rendered and adds no listeners', () => {
    listenerCalls = 0;
    assert.doesNotThrow(() => setActiveNav('overview', root([])));
    const links = ['overview'].map(link);
    setActiveNav('overview', root(links));
    assert.deepEqual(active(links), [true]);
    assert.equal(listenerCalls, 0);
});
