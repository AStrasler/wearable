const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const input = '"><img src=x onerror=alert(1)>&\\'';
const escaped = '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;&amp;&#39;';

function loadDashboard(file, devices = []) {
  const elements = new Map();
  const listeners = new Map();
  function element(selector) {
    if (!elements.has(selector)) {
      elements.set(selector, {
        children: [],
        style: {},
        classList: { add() {}, remove() {} },
        prepend(child) { this.children.unshift(child); },
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; },
        add(child) { this.children.push(child); },
        addEventListener(type, callback) { listeners.set(`${selector}:${type}`, callback); }
      });
    }
    return elements.get(selector);
  }

  const context = vm.createContext({
    document: {
      querySelector: selector => selector === '.brow' ? null : element(selector),
      querySelectorAll: () => [],
      createElement: () => element(Symbol())
    },
    fetch: async route => {
      if (new URL(route, 'http://localhost/').pathname === '/v1/devices') {
        return { ok: true, json: async () => devices };
      }
      // Leave unrelated startup requests pending. These tests exercise rendering only.
      return new Promise(() => {});
    },
    localStorage: { getItem: () => 'test-key' },
    URL,
    Option: class {
      constructor(text, value) { this.text = text; this.value = value; }
    },
    WebSocket: class {},
    location: { host: 'localhost' },
    window: {
      location: { href: 'http://localhost/', origin: 'http://localhost' },
      addEventListener() {}
    },
    performance: { now: () => 1000 },
    setInterval() {},
  });

  const code = readFileSync(path.join(__dirname, '..', file), 'utf8');
  const run = (script) => vm.runInContext(script, context);
  run(code);
  return { context, element, listeners, run };
}

test('ingest dashboard renders uploaded device IDs and event labels as literal text', async () => {
  const { context, element, run } = loadDashboard(
    'server/ingest/app/static/app.js', [{ device_id: input }]
  );
  await new Promise(resolve => setImmediate(resolve));
  context.events = [
    { kind: input, ts: '2026-01-01T00:00:00Z' },
    { kind: 'Activity(12)', ts: '2026-01-01T00:00:00Z' }
  ];
  run('renderEvents(events)');
  assert.ok(element('#events').innerHTML.includes(escaped));
  assert.ok(element('#events').innerHTML.includes('>Activity</span>'));
  assert.ok(!element('#events').innerHTML.includes('<img'));

  const select = element('#device-sel');
  assert.equal(select.children.length, 1);
  assert.equal(select.children[0].text, input);
  assert.equal(select.children[0].value, input);

  context.batches = [{ batch_id: input, byte_size: 1024, packet_count: 2 }];
  run('renderBatches(batches)');
  const batches = element('#batch-list').innerHTML;
  assert.ok(batches.includes(`data-id="${escaped}"`));
  assert.ok(batches.includes('>2 pkt</span>'));
  assert.ok(!batches.includes('<img'));
});

for (const [name, file, render, rawKey] of [
  ['live', 'dashboard/static/app.js', 'renderInspector(packet)', 'raw'],
  ['ingest', 'server/ingest/app/static/app.js', 'inspectFrame(packet, false)', 'hex']
]) {
  test(`${name} packet inspector escapes raw bytes, parsed values, attributes and hover text`, () => {
    const { context, element, listeners, run } = loadDashboard(file);
    context.packet = {
      [rawKey]: Buffer.from('<b>raw&</b>').toString('hex'),
      type_name: 'CONSOLE_LOGS',
      char: 'strap',
      len_bytes: 11,
      ts: 1,
      fields: [{ name: input, value: input, note: input, off: 0, len: 11, cat: 'text' }],
      parsed: { [input]: input, log: input }
    };
    run(render);
    assert.ok(element('#hexgrid').innerHTML.includes('&lt;b&gt;raw&amp;&lt;/b&gt;'));
    assert.ok(!element('#hexgrid').innerHTML.includes('<b>raw'));
    assert.ok(element('#fields').innerHTML.includes(`title="${escaped}">${escaped}</span>`));
    assert.ok(element('#parsed').innerHTML.includes(`<b>${escaped}</b><span>${escaped}</span>`));
    if (name === 'live') run('latest.CONSOLE_LOGS = packet; selected = "CONSOLE_LOGS"');
    listeners.get('#hexgrid:mouseover')({
      target: { closest: () => ({ dataset: { field: '0' } }) }
    });
    assert.ok(element('#tooltip').innerHTML.includes(escaped));
    assert.ok(!element('#tooltip').innerHTML.includes('<img'));

    context.categories = ['text', input];
    run('renderLegend(categories)');
    const legend = element('#legend').children;
    assert.equal(legend[0].children[1], 'text');
    assert.equal(legend[1].children[1], input);

    if (name === 'live') {
      context.packetType = input;
      run('latest[packetType] = packet; counts[packetType] = 1; renderTabs()');
      assert.ok(element('#type-tabs').innerHTML.includes(`data-t="${escaped}"`));
      assert.ok(!element('#type-tabs').innerHTML.includes('<img'));
      run('streamRow(packet); sys(packet.parsed.log)');
      for (const row of element('#log').children) {
        assert.ok(row.innerHTML.includes(escaped));
        assert.ok(!row.innerHTML.includes('<img'));
      }
    }
  });
}
