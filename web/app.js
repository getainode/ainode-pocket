/* AINode Pocket UI. No framework, no build step, no CDN. */
'use strict';

var state = null;
var view = 'overview';
var modelDevice = null;
var benchPoll = null;
// A run is in flight. The state refresh repaints the bench controls every few
// seconds and must not hand the Run button back while the suite is working.
var benchBusy = false;
var chatBusy = false;

/* ── helpers ─────────────────────────────────────────────────────── */
function $(sel) { return document.querySelector(sel); }
function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function bytes(n) {
  n = Number(n || 0);
  var units = ['B', 'KB', 'MB', 'GB', 'TB'];
  for (var i = 0; i < units.length; i++) {
    if (n < 1024) return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
    n /= 1024;
  }
  return n.toFixed(1) + ' PB';
}
function pct(n) { return (Number(n || 0)).toFixed(0) + '%'; }
function short(id) { return String(id || '').split('/').pop(); }

function toast(message, bad) {
  var box = $('#toast');
  box.textContent = message;
  box.className = 'toast' + (bad ? ' bad' : '');
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(function () { box.hidden = true; }, bad ? 7000 : 3200);
}

function api(path, options) {
  options = options || {};
  if (demo) return demoApi(path, options);
  var init = { method: options.method || 'GET' };
  if (options.body) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(options.body);
  }
  return fetch(path, init).then(function (resp) {
    return resp.text().then(function (text) {
      var payload = {};
      try { payload = text ? JSON.parse(text) : {}; } catch (err) { payload = { raw: text }; }
      if (!resp.ok) {
        var message = (payload.error && payload.error.message) || ('HTTP ' + resp.status);
        throw new Error(message);
      }
      return payload;
    });
  });
}

function bar(percent, warnAt, badAt) {
  var wrap = el('div', 'bar');
  var fill = el('span');
  var value = Math.max(0, Math.min(100, Number(percent || 0)));
  fill.style.width = value + '%';
  if (badAt && value >= badAt) fill.className = 'bad';
  else if (warnAt && value >= warnAt) fill.className = 'warn';
  wrap.appendChild(fill);
  return wrap;
}

function gauge(label, value, percent, warnAt, badAt) {
  var box = el('div');
  var head = el('div', 'gauge-label');
  head.appendChild(el('span', null, label));
  var strong = el('b', null, value);
  head.appendChild(strong);
  box.appendChild(head);
  box.appendChild(bar(percent, warnAt || 75, badAt || 90));
  return box;
}

/* ── navigation ──────────────────────────────────────────────────── */
function show(name) {
  view = name;
  var items = document.querySelectorAll('.nav-item');
  for (var i = 0; i < items.length; i++) {
    items[i].classList.toggle('active', items[i].dataset.view === name);
  }
  ['overview', 'models', 'chat', 'bench'].forEach(function (key) {
    $('#view-' + key).hidden = key !== name;
  });
  $('#view-title').textContent = { overview: 'Overview', models: 'Models',
                                   chat: 'Chat', bench: 'Bench' }[name];
  if (name === 'models') loadModels();
  if (name === 'bench') loadBench();
  if (name === 'chat') chatEnter();
}

/* ── overview ────────────────────────────────────────────────────── */
function refresh() {
  return api('/api/state').then(function (payload) {
    state = payload;
    paintTop();
    paintOverview();
    if (view === 'chat') { fillChatModels(); loadInstances(); }
    fillBenchDevices();
  }).catch(function (err) { toast(err.message, true); });
}

function paintTop() {
  var summary = state.summary;
  var stats = $('#topbar-stats');
  stats.textContent = '';
  [['devices', summary.online + '/' + summary.devices],
   ['models', String(summary.models)],
   ['loaded', String(summary.loaded)]].forEach(function (pair) {
    var span = el('span', null, pair[0] + ' ');
    span.appendChild(el('b', null, pair[1]));
    stats.appendChild(span);
  });
  $('#version').textContent = 'v' + state.version;
  $('#footer-version').textContent = state.version;
  $('#mini-endpoint').textContent = state.endpoint.base_url;
  $('#endpoint-url').textContent = state.endpoint.base_url;
  var model = firstReadyModel();
  $('#curl-snippet').textContent =
    'curl ' + state.endpoint.base_url + '/chat/completions \\\n' +
    "  -H 'Content-Type: application/json' \\\n" +
    '  -d \'{"model": "' + (model || '<model id>') + '",\n' +
    '       "messages": [{"role": "user", "content": "hello"}],\n' +
    '       "max_tokens": 512}\'';
}

function paintOverview() {
  // The device grid rebuilds from scratch here, which would blow away
  // whatever someone is mid-typing into an unlock password field -- the
  // 6-second poll firing mid-keystroke reads as "the page keeps refreshing
  // and eating my password." Skip the rebuild for this tick while that
  // field has focus; the summary strip above still updates every time.
  if (document.activeElement && document.activeElement.classList.contains('unlock-pw')) {
    return;
  }
  var summary = state.summary;
  var strip = $('#summary');
  strip.textContent = '';
  [['Devices online', summary.online + ' / ' + summary.devices, 'registered'],
   ['Models in fleet', String(summary.models), summary.models_ready + ' ready to serve'],
   ['Loaded now', String(summary.loaded), 'across every device'],
   ['NPU units used', npuTotals(), 'residency, not compute']
  ].forEach(function (row) {
    var card = el('div', 'stat');
    card.appendChild(el('div', 'stat-label', row[0]));
    var value = el('div', 'stat-value green', row[1]);
    card.appendChild(value);
    card.appendChild(el('div', 'stat-sub', row[2]));
    strip.appendChild(card);
  });

  var grid = $('#devices');
  grid.textContent = '';
  if (!state.devices.length) {
    grid.appendChild(el('div', 'empty',
      'No devices yet. Add one above, or run: python3 ainode-pocket --device <address>'));
    return;
  }
  state.devices.forEach(function (device) { grid.appendChild(deviceCard(device)); });
}

function npuTotals() {
  var used = 0, total = 0;
  (state.devices || []).forEach(function (device) {
    if (device.npu_units) { used += device.npu_units.used; total += device.npu_units.total; }
  });
  return total ? used + ' / ' + total : '-';
}

function unlockForm(device) {
  // A box that rebooted comes back locked -- /data unmounted, the gateway
  // answering an instant error, no TiinyOS needed to fix it. One password,
  // sent straight to the device's own account API. See
  // ~/code/tiiny/tools/README-unlock.md for what this call actually does.
  var wrap = el('div', 'unlock-row');
  var input = document.createElement('input');
  input.type = 'password';
  input.placeholder = 'Tiiny password, to unlock';
  input.className = 'unlock-pw';
  var button = el('button', 'btn small', 'Unlock');
  var go = function () {
    var password = input.value;
    if (!password) return;
    button.disabled = true;
    api('/api/devices/unlock', { method: 'POST',
      body: { id: device.id, password: password } })
      .then(function () {
        input.value = '';
        toast('unlocked ' + device.name);
        refresh();
      })
      .catch(function (err) { toast(err.message, true); })
      .then(function () { button.disabled = false; });
  };
  input.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') go(); });
  button.onclick = go;
  wrap.appendChild(input);
  wrap.appendChild(button);
  return wrap;
}

function deviceCard(device) {
  var card = el('div', 'device' + (device.online ? '' : ' offline'));
  var head = el('div', 'device-head');
  head.appendChild(el('span', 'dot' + (device.online ? '' : ' off')));
  head.appendChild(el('span', 'device-name', device.name));
  var actions = el('div', 'device-actions');
  var forget = el('button', 'btn small danger', 'Forget');
  forget.onclick = function () {
    api('/api/devices?id=' + encodeURIComponent(device.id), { method: 'DELETE' })
      .then(function () { toast('removed ' + device.name); refresh(); })
      .catch(function (err) { toast(err.message, true); });
  };
  actions.appendChild(forget);
  head.appendChild(actions);
  card.appendChild(head);

  var firmware = device.firmware || {};
  var route = device.transport || {};
  var planes = route.planes || {};
  var subParts = [];
  Object.keys(planes).forEach(function (name) {
    subParts.push(name + ' ' + planes[name] + (name === route.plane ? ' *' : ''));
  });
  if (!subParts.length) subParts.push(device.address || device.gateway);
  if (firmware.tiiny_os) subParts.push('TiinyOS ' + firmware.tiiny_os);
  if (firmware.serial) subParts.push(firmware.serial);
  card.appendChild(el('div', 'device-sub', subParts.join('  ·  ')));

  if (!device.online) {
    card.appendChild(el('div', 'turn-err', device.error || 'unreachable'));
    card.appendChild(unlockForm(device));
    return card;
  }

  var units = device.npu_units || {};
  var memory = device.npu_memory || {};
  var storage = device.storage || {};
  var cpu = device.cpu || {};
  var gauges = el('div', 'gauge-row');
  gauges.appendChild(gauge('NPU units', units.used + ' / ' + units.total, units.percent));
  gauges.appendChild(gauge('NPU memory',
    (memory.used_mb / 1024).toFixed(1) + ' / ' + (memory.total_mb / 1024).toFixed(1) + ' GiB',
    memory.percent));
  gauges.appendChild(gauge('Storage',
    bytes(storage.free_bytes) + ' free', storage.percent));
  gauges.appendChild(gauge('CPU', pct(cpu.percent) + ' of ' + cpu.cores + ' cores',
    cpu.percent));
  card.appendChild(gauges);

  var lock = device.lock || {};
  var meta = el('div', 'kv');
  var lockText = lock.busy
    ? 'in use' + (lock.held_for ? ' for ' + lock.held_for + 's' : '')
    : 'idle';
  if (lock.waiting) lockText += ', ' + lock.waiting + ' waiting';
  // Which address and which transport this device is actually being reached
  // on. Worth showing: on firmware with port 8800 closed the gateway is only
  // reachable by host header on port 80, and USB is preferred over DHCP.
  meta.appendChild(el('dt', null, 'gateway'));
  var via = route.gateway === 'vhost'
    ? 'via host header on :80' : (route.gateway === 'direct'
      ? 'via port 8800' : 'not determined yet');
  if (route.plane) via += '  ·  over ' + route.plane;
  if (route.usb_linked === false && planes.usb) via += ' (usb cable not in this host)';
  meta.appendChild(el('dd', null, via));
  meta.appendChild(el('dt', null, 'lock'));
  meta.appendChild(el('dd', null, lockText + (lock.shared ? ' (shared)' : ' (this process only)')));
  meta.appendChild(el('dt', null, 'thermals'));
  var thermal = device.thermal || {};
  if (thermal.available) {
    meta.appendChild(el('dd', null, thermal.readings.map(function (r) {
      return [r.temp_c !== null ? r.temp_c + ' C' : null,
              r.power_w !== null ? r.power_w + ' W' : null].filter(Boolean).join('  ');
    }).join('  ·  ')));
  } else {
    meta.appendChild(el('dd', null, thermal.reason || 'not reported'));
  }
  card.appendChild(meta);

  var loaded = el('div', 'loaded-list');
  var instances = device.instances || [];
  if (!(device.running || []).length) {
    loaded.appendChild(el('div', 'empty', 'Nothing loaded. Inference needs a loaded model.'));
  } else {
    (device.running || []).forEach(function (modelId) {
      var found = null;
      instances.forEach(function (inst) { if (inst.model_id === modelId) found = inst; });
      var row = el('div', 'loaded-row');
      row.appendChild(el('span', 'dot'));
      row.appendChild(el('code', null, modelId));
      var bits = [];
      if (found && found.npu_usage) bits.push(found.npu_usage + ' units');
      if (found && found.port) bits.push('port ' + found.port);
      row.appendChild(el('span', 'meta', bits.join('  ·  ')));
      loaded.appendChild(row);
    });
  }
  card.appendChild(loaded);

  // Some firmware pins this at zero even mid-generation, and some reports a
  // real figure. Only carry the caveat when the number is actually stuck.
  if (memory.utilization_percent === 0) {
    card.appendChild(el('div', 'device-note',
      'NPU utilisation reads 0% on this firmware even mid-generation, so it is ' +
      'not shown as a load signal. Memory and unit accounting are live.'));
  } else if (memory.utilization_percent) {
    card.appendChild(el('div', 'device-note',
      'NPU utilisation ' + Number(memory.utilization_percent).toFixed(1) + '%'));
  }
  return card;
}

/* ── add a device ────────────────────────────────────────────────── */
function wireAdd() {
  $('#show-add').onclick = function () {
    var card = $('#add-card');
    card.hidden = !card.hidden;
  };
  $('#do-discover').onclick = function () {
    // With no address this is the automatic sweep: a UDP broadcast plus any
    // USB link this host is plugged into.
    var address = $('#add-address').value.trim();
    var button = this;
    button.disabled = true;
    button.textContent = address ? 'Checking' : 'Looking';
    api('/api/discover', { method: 'POST', body: address ? { address: address } : { auto: true } })
      .then(function (payload) {
        renderFound(payload.found);
        if (!address) toast(payload.found.length + ' device(s) found');
      })
      .catch(function (err) { toast(err.message, true); })
      .then(function () {
        button.disabled = false;
        button.textContent = 'Find devices';
      });
  };
  $('#do-scan').onclick = function () {
    var subnet = $('#add-subnet').value.trim();
    if (!subnet) return toast('enter a subnet like 192.168.100', true);
    var button = this;
    button.disabled = true;
    button.textContent = 'Scanning';
    api('/api/discover', { method: 'POST', body: { subnet: subnet } })
      .then(function (payload) {
        renderFound(payload.found);
        toast(payload.found.length + ' device(s) found');
      })
      .catch(function (err) { toast(err.message, true); })
      .then(function () { button.disabled = false; button.textContent = 'Scan'; });
  };
  $('#do-add').onclick = function () {
    addDevice($('#add-address').value.trim(), $('#add-key').value.trim());
  };
  $('#copy-endpoint').onclick = function () {
    var text = $('#endpoint-url').textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(text);
    toast('copied ' + text);
  };
}

function renderFound(found) {
  var box = $('#add-result');
  box.textContent = '';
  if (!found || !found.length) { box.appendChild(el('div', 'empty', 'nothing found')); return; }
  found.forEach(function (hit) {
    var row = el('div', 'loaded-row');
    row.appendChild(el('span', 'dot' + (hit.known ? '' : ' off')));
    row.appendChild(el('code', null, hit.name || hit.serial));
    // A box answers on USB and Wi-Fi at once and reports both, so show both:
    // it registers once, with every address it has.
    var where = Object.keys(hit.addresses || {}).map(function (name) {
      return name + ' ' + hit.addresses[name];
    }).join('  ');
    row.appendChild(el('span', 'meta', where + (hit.known ? '  already added' : '')));
    if (!hit.known) {
      var add = el('button', 'btn small', 'Add');
      add.onclick = function () {
        addDevice(hit.address, $('#add-key').value.trim(), hit.planes, hit.name);
      };
      row.appendChild(add);
    }
    box.appendChild(row);
  });
}

function addDevice(address, key, planes, name) {
  if (!address) return toast('enter an address', true);
  api('/api/devices', { method: 'POST',
                        body: { address: address, key: key || '',
                                planes: planes || null, name: name || null } })
    .then(function (payload) {
      if (payload.telemetry && payload.telemetry.error) {
        toast('added, but it did not answer: ' + payload.telemetry.error, true);
      } else {
        toast('added ' + payload.device.name);
      }
      $('#add-card').hidden = true;
      $('#add-result').textContent = '';
      refresh();
    })
    .catch(function (err) { toast(err.message, true); });
}

/* ── models ──────────────────────────────────────────────────────── */
function loadModels() {
  var tabs = $('#model-tabs');
  tabs.textContent = '';
  var devices = state ? state.devices : [];
  if (!devices.length) {
    $('#installed-table').textContent = '';
    $('#catalog-table').textContent = '';
    $('#npu-budget').textContent = '';
    tabs.appendChild(el('div', 'empty', 'Register a device first.'));
    return;
  }
  if (!modelDevice || !devices.some(function (d) { return d.id === modelDevice; })) {
    modelDevice = devices[0].id;
  }
  devices.forEach(function (device) {
    var tab = el('button', 'tab' + (device.id === modelDevice ? ' active' : ''), device.name);
    tab.onclick = function () { modelDevice = device.id; loadModels(); };
    tabs.appendChild(tab);
  });

  var device = null;
  devices.forEach(function (d) { if (d.id === modelDevice) device = d; });
  var budget = $('#npu-budget');
  budget.textContent = '';
  if (device && device.npu_units) {
    var card = el('div', 'card');
    var header = el('div', 'card-header');
    header.appendChild(el('div', 'card-title', 'NPU budget'));
    header.appendChild(el('span', 'muted',
      device.npu_units.available + ' of ' + device.npu_units.total + ' units free'));
    card.appendChild(header);
    card.appendChild(bar(device.npu_units.percent, 80, 95));
    card.appendChild(el('p', 'muted',
      'Units are memory residency, not a compute reservation. Several models can be ' +
      'resident at once; only one of them runs at a time.'));
    budget.appendChild(card);
  }

  api('/api/models?device=' + encodeURIComponent(modelDevice) + '&force=1')
    .then(function (payload) { paintInstalled(payload.models); })
    .catch(function (err) { toast(err.message, true); });
  api('/api/catalog?device=' + encodeURIComponent(modelDevice))
    .then(function (payload) { paintCatalog(payload.catalog); })
    .catch(function (err) { toast(err.message, true); });
}

function table(node, headers, rows) {
  node.textContent = '';
  var thead = el('thead');
  var tr = el('tr');
  headers.forEach(function (name) { tr.appendChild(el('th', null, name)); });
  thead.appendChild(tr);
  node.appendChild(thead);
  var tbody = el('tbody');
  rows.forEach(function (cells) { tbody.appendChild(cells); });
  node.appendChild(tbody);
}

function paintInstalled(models) {
  $('#installed-count').textContent = models.length + ' model(s)';
  var rows = models.map(function (model) {
    var tr = el('tr');
    var name = el('td', 'id');
    name.appendChild(el('div', null, model.model_id));
    tr.appendChild(name);
    tr.appendChild(el('td', null, model.type));
    tr.appendChild(el('td', 'num', model.params));
    tr.appendChild(el('td', 'num', bytes(model.size)));
    tr.appendChild(el('td', 'num', model.npu_usage || '-'));
    var stateCell = el('td');
    if (model.loaded) stateCell.appendChild(el('span', 'pill', 'loaded'));
    else if (model.status === 'error') stateCell.appendChild(el('span', 'pill bad', 'error'));
    else stateCell.appendChild(el('span', 'pill flat', 'on disk'));
    tr.appendChild(stateCell);
    var act = el('td', 'act');
    if (model.loaded) {
      act.appendChild(action('Unload', 'unload', model.model_id));
    } else {
      act.appendChild(action('Load', 'load', model.model_id, true));
      act.appendChild(action('Delete', 'delete', model.model_id, false, true));
    }
    tr.appendChild(act);
    return tr;
  });
  table($('#installed-table'),
        ['Model', 'Type', 'Params', 'Size', 'NPU', 'State', ''], rows);
}

function action(label, kind, modelId, primary, danger) {
  var button = el('button', 'btn small' + (primary ? ' primary' : '') + (danger ? ' danger' : ''),
                  label);
  button.onclick = function () {
    button.disabled = true;
    api('/api/models/' + kind, { method: 'POST',
                                 body: { device: modelDevice, model: modelId } })
      .then(function () {
        toast(label.toLowerCase() + ' requested for ' + short(modelId));
        return refresh();
      })
      .then(function () { loadModels(); })
      .catch(function (err) { toast(err.message, true); button.disabled = false; });
  };
  return button;
}

function paintCatalog(rows) {
  var body = rows.map(function (model) {
    var tr = el('tr');
    tr.appendChild(el('td', 'id', model.model_id));
    tr.appendChild(el('td', null, model.type));
    tr.appendChild(el('td', 'num', model.params));
    // The live catalog reports no size for a model that is not installed.
    tr.appendChild(el('td', 'num', model.size ? bytes(model.size) : '-'));
    tr.appendChild(el('td', 'num', model.npu_usage || '-'));
    var act = el('td', 'act');
    if (model.installed) {
      act.appendChild(el('span', 'pill flat', 'installed'));
    } else {
      var cell = el('div', 'progress');
      var button = el('button', 'btn small primary', 'Download');
      button.onclick = function () { download(model.model_id, cell, button); };
      cell.appendChild(button);
      act.appendChild(cell);
    }
    tr.appendChild(act);
    return tr;
  });
  table($('#catalog-table'), ['Model', 'Type', 'Params', 'Size', 'NPU', ''], body);
}

function download(modelId, cell, button) {
  button.disabled = true;
  button.textContent = 'Starting';
  var url = '/api/models/events?device=' + encodeURIComponent(modelDevice) +
            '&model=' + encodeURIComponent(modelId);
  var source = new EventSource(url);
  var fill = bar(0);
  var label = el('span', null, '0%');
  cell.textContent = '';
  cell.appendChild(fill);
  cell.appendChild(label);
  source.onmessage = function (event) {
    if (event.data === '[DONE]') {
      source.close();
      toast(short(modelId) + ' downloaded');
      loadModels();
      return;
    }
    var payload = {};
    try { payload = JSON.parse(event.data); } catch (err) { return; }
    if (payload.error) {
      source.close();
      toast(payload.error, true);
      return;
    }
    // The frame body of the device's download stream is not in any recorded
    // spec, so read it defensively: try the field names the progress endpoint
    // uses, then the ones OpenAIModel declares, and fall back to showing
    // whatever status text arrived rather than a bar stuck at zero.
    var value = payload.progress;
    if (value === undefined) value = payload.global_progress;
    if (value === undefined) value = payload.stage_progress;
    if (value === undefined) {
      label.textContent = payload.status || payload.message || 'working';
      return;
    }
    value = Number(value) || 0;
    fill.firstChild.style.width = value + '%';
    label.textContent = value.toFixed(0) + '%' +
      (payload.speed_human ? '  ' + payload.speed_human : '');
  };
  source.onerror = function () { source.close(); };
}

/* ── chat ────────────────────────────────────────────────────────── */
// Only models that can chat AND are loaded somewhere. A device holds speech,
// embedding, reranking and image models too, and the running list on its own
// does not tell them apart, so the server works it out and sends the answer in
// state.chat_models. Offering a text-to-speech model here is what produced the
// device's own "does not support chat" error in the middle of a conversation.
function readyModels() {
  return (state && state.chat_models ? state.chat_models : []).map(
    function (row) { return row.model_id; });
}

function firstReadyModel() {
  var models = readyModels();
  return models.length ? models[0] : null;
}

// Entering the tab paints all three columns: the card for whatever the picker
// has selected, the conversation, and the rail of what is loaded right now.
function chatEnter() {
  fillChatModels();
  renderConversation();
  renderConvList();
  loadInstances();
}

function fillChatModels() {
  var select = $('#chat-model');
  var previous = select.value;
  var rows = (state && state.chat_models) ? state.chat_models : [];
  var note = $('#chat-route');
  select.textContent = '';
  note.textContent = '';
  if (!rows.length) {
    select.appendChild(el('option', null, 'no chat model loaded'));
    select.disabled = true;
    note.appendChild(document.createTextNode(
      'No chat model is loaded, so there is nothing here to talk to. Load one on the '));
    var link = el('button', 'linklike', 'Models page');
    link.type = 'button';
    link.onclick = function () { show('models'); };
    note.appendChild(link);
    note.appendChild(document.createTextNode(', or in the panel on the right.'));
    cardModel = null;
    var card = $('#model-card');
    card.textContent = '';
    card.appendChild(el('div', 'empty', 'No model selected.'));
    return;
  }
  select.disabled = false;
  // Grouped by where the model is loaded, because on a fleet the useful
  // question is which box will answer. A model loaded on two devices gets its
  // own group, since the endpoint chooses between them and the person does not.
  var groups = [];
  var byWhere = {};
  rows.forEach(function (row) {
    if (!byWhere[row.where]) {
      byWhere[row.where] = el('optgroup');
      byWhere[row.where].label = row.where;
      groups.push(byWhere[row.where]);
    }
    var option = el('option', null, row.model_id);
    option.value = row.model_id;
    byWhere[row.where].appendChild(option);
  });
  groups.forEach(function (group) { select.appendChild(group); });
  select.value = readyModels().indexOf(previous) !== -1 ? previous : rows[0].model_id;
  note.textContent = rows.length === 1
    ? 'routed to ' + rows[0].where
    : 'routed to whichever device has it loaded';
  chatModelChanged();
}
/* ── markdown and highlighting ───────────────────────────────────── */
// Adapted from the AINode command center's renderer
// (ainode/web/static/js/app.js, formatMarkdown, same author and licence),
// with three changes it needed here: markdown links are lifted out before the
// bare-URL autolinker runs (in the original the autolinker ate the URL first
// and left [text](<a ...>) on screen), real <ul>/<ol> replace the bullet
// glyph, and blank lines become paragraphs instead of a run of <br>.
//
// Everything the model wrote is escaped before any tag is assembled: code
// spans and links are pulled out into slots, the remainder goes through the
// text-node escaper, and only then is markup added. The slots are rejoined
// last, so no path exists for model output to reach innerHTML unescaped.
function escapeHtml(text) {
  var node = document.createElement('div');
  node.textContent = (text === undefined || text === null) ? '' : String(text);
  return node.innerHTML;
}

function escapeAttr(text) {
  return escapeHtml(text).replace(/"/g, '&quot;');
}

function renderMarkdown(text) {
  if (!text) return '';
  var slots = [];
  // \u0001 is the slot marker, so it is stripped from the model's text first:
  // otherwise a model could write one and address a slot of its own.
  var src = String(text).replace(/\u0001/g, '');

  src = src.replace(/```([A-Za-z0-9+#_.-]*)[ \t]*\r?\n?([\s\S]*?)```/g,
                    function (match, lang, code) {
    slots.push(codeBlock(lang, code.replace(/\n$/, '')));
    return '\u0001B' + (slots.length - 1) + '\u0001';
  });
  // A fence that has not closed yet is a block that is still streaming in, so
  // it is rendered as code rather than left on screen as literal backticks.
  src = src.replace(/```([A-Za-z0-9+#_.-]*)[ \t]*\r?\n?([\s\S]*)$/,
                    function (match, lang, code) {
    slots.push(codeBlock(lang, code));
    return '\u0001B' + (slots.length - 1) + '\u0001';
  });
  src = src.replace(/`([^`\n]+)`/g, function (match, code) {
    slots.push('<code class="inline-code">' + escapeHtml(code) + '</code>');
    return '\u0001I' + (slots.length - 1) + '\u0001';
  });
  src = src.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
                    function (match, label, url) {
    slots.push('<a class="chat-link" target="_blank" rel="noopener noreferrer" href="'
               + escapeAttr(url) + '">' + escapeHtml(label) + '</a>');
    return '\u0001I' + (slots.length - 1) + '\u0001';
  });

  var html = escapeHtml(src);
  html = html.replace(/(https?:\/\/[^\s<]+[^\s<.,;:?!)])/g, function (url) {
    return '<a class="chat-link" target="_blank" rel="noopener noreferrer" href="'
           + url.replace(/"/g, '&quot;') + '">' + url + '</a>';
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
             .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
             .replace(/~~([^~]+)~~/g, '<del>$1</del>');
  html = markdownBlocks(html);
  return html.replace(/\u0001([BI])(\d+)\u0001/g, function (match, kind, index) {
    return slots[Number(index)] || '';
  });
}

// Headings, lists and paragraphs, walked line by line so a list is a real list
// and a blank line ends a paragraph rather than stacking <br>.
function markdownBlocks(html) {
  var lines = html.split('\n');
  var out = [];
  var para = [];
  var items = null;
  var itemTag = null;

  function flushPara() {
    if (para.length) { out.push('<p>' + para.join('<br>') + '</p>'); para = []; }
  }
  function flushList() {
    if (items) { out.push('<' + itemTag + '>' + items.join('') + '</' + itemTag + '>'); }
    items = null; itemTag = null;
  }

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].replace(/\s+$/, '');
    var trimmed = line.trim();
    if (!trimmed) { flushPara(); flushList(); continue; }
    if (/^\u0001B\d+\u0001$/.test(trimmed)) {
      flushPara(); flushList(); out.push(trimmed); continue;
    }
    var heading = /^(#{1,5})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushPara(); flushList();
      var level = Math.min(heading[1].length + 1, 5);
      out.push('<h' + level + ' class="chat-h">' + heading[2] + '</h' + level + '>');
      continue;
    }
    if (/^([-*_])(?:\s*\1){2,}$/.test(trimmed)) { flushPara(); flushList(); continue; }
    var bullet = /^[-*+]\s+(.*)$/.exec(trimmed);
    var number = /^(\d+)[.)]\s+(.*)$/.exec(trimmed);
    if (bullet || number) {
      flushPara();
      var tag = bullet ? 'ul' : 'ol';
      if (items && itemTag !== tag) flushList();
      if (!items) { items = []; itemTag = tag; }
      items.push('<li>' + (bullet ? bullet[1] : number[2]) + '</li>');
      continue;
    }
    flushList();
    para.push(trimmed);
  }
  flushPara();
  flushList();
  return out.join('');
}

function codeBlock(lang, code) {
  var label = String(lang || '').toLowerCase().replace(/[^a-z0-9+#_.-]/g, '') || 'text';
  return '<div class="code-block-wrapper">' +
    '<div class="code-block-header">' +
      '<span class="code-block-lang">' + escapeHtml(label) + '</span>' +
      '<button class="code-copy-btn" type="button">Copy</button>' +
    '</div>' +
    '<pre class="code-block"><code class="language-' + escapeHtml(label) + '">' +
    highlight(code, label) + '</code></pre></div>';
}

// The reference app labels the language and leaves the code grey. This is the
// one place Pocket goes further, because a benchmarking utility shows a lot of
// python and shell: a small in-house tokeniser, no library, no CDN. A language
// it does not know is escaped and left alone rather than guessed at.
var HL_ALIAS = { py: 'python', python3: 'python', js: 'javascript', jsx: 'javascript',
                 ts: 'javascript', typescript: 'javascript', node: 'javascript',
                 sh: 'bash', shell: 'bash', zsh: 'bash', console: 'bash',
                 yml: 'yaml', htm: 'html', xml: 'html', svg: 'html' };

var HL_RULES = {
  python: [
    ['str', /(?:[rbuf]{0,2})(?:"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')/],
    ['com', /#[^\n]*/],
    ['kw', /\b(?:def|class|return|if|elif|else|for|while|in|not|and|or|is|None|True|False|import|from|as|with|try|except|finally|raise|yield|lambda|pass|break|continue|global|nonlocal|assert|del|async|await|self)\b/],
    ['fn', /\b(?:print|len|range|str|int|float|dict|list|set|tuple|open|enumerate|zip|sum|min|max|abs|round|sorted|isinstance|getattr|setattr|json|time)\b/],
    ['num', /\b\d[\d_]*(?:\.\d+)?(?:[eE][-+]?\d+)?\b/]
  ],
  javascript: [
    ['str', /(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)/],
    ['com', /\/\/[^\n]*|\/\*[\s\S]*?\*\//],
    ['kw', /\b(?:var|let|const|function|return|if|else|for|while|do|break|continue|new|this|typeof|instanceof|null|undefined|true|false|class|extends|import|export|from|async|await|try|catch|finally|throw|switch|case|default|in|of)\b/],
    ['fn', /\b(?:console|document|window|JSON|Math|Object|Array|Promise|fetch|setTimeout|parseInt|parseFloat)\b/],
    ['num', /\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b/]
  ],
  bash: [
    ['str', /(?:"(?:\\.|[^"\\])*"|'[^']*')/],
    ['com', /#[^\n]*/],
    ['var', /\$\{[^}\n]*\}|\$[A-Za-z_][\w]*/],
    ['kw', /\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|function|return|export|local|source|set|echo|cd|exit)\b/],
    ['fn', /\b(?:python3|python|curl|git|grep|sed|awk|jq|make|npm|node|pip3|pip)\b/],
    ['num', /\b\d+\b/]
  ],
  json: [
    ['key', /"(?:\\.|[^"\\])*"(?=\s*:)/],
    ['str', /"(?:\\.|[^"\\])*"/],
    ['kw', /\b(?:true|false|null)\b/],
    ['num', /-?\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b/]
  ],
  html: [
    ['com', /<!--[\s\S]*?-->/],
    ['str', /"[^"\n]*"|'[^'\n]*'/],
    ['tag', /<\/?[A-Za-z][\w:-]*|\/?>/],
    ['attr', /\b[A-Za-z-]+(?=\s*=)/]
  ],
  css: [
    ['com', /\/\*[\s\S]*?\*\//],
    ['str', /"[^"\n]*"|'[^'\n]*'/],
    ['kw', /@[\w-]+|--[\w-]+/],
    ['attr', /[-a-zA-Z]+(?=\s*:)/],
    ['num', /-?\b\d*\.?\d+(?:px|em|rem|%|vh|vw|s|ms|deg)?\b|#[0-9a-fA-F]{3,8}\b/]
  ],
  yaml: [
    ['com', /#[^\n]*/],
    ['str', /"[^"\n]*"|'[^'\n]*'/],
    ['key', /^[ \t-]*[\w.-]+(?=\s*:)/],
    ['num', /\b\d+(?:\.\d+)?\b/]
  ]
};

var hlCache = {};
function hlRegex(name, rules) {
  if (hlCache[name]) return hlCache[name];
  var parts = rules.map(function (rule) { return '(' + rule[1].source + ')'; });
  hlCache[name] = new RegExp(parts.join('|'), 'gm');
  return hlCache[name];
}

function highlight(code, lang) {
  var name = HL_ALIAS[lang] || lang;
  var rules = HL_RULES[name];
  if (!rules) return escapeHtml(code);
  var re = hlRegex(name, rules);
  re.lastIndex = 0;
  var out = '';
  var last = 0;
  var match;
  while ((match = re.exec(code)) !== null) {
    if (!match[0]) { re.lastIndex++; continue; }
    if (match.index > last) out += escapeHtml(code.slice(last, match.index));
    var cls = rules[0][0];
    for (var g = 1; g < match.length; g++) {
      if (match[g] !== undefined) { cls = rules[g - 1][0]; break; }
    }
    out += '<span class="tok-' + cls + '">' + escapeHtml(match[0]) + '</span>';
    last = match.index + match[0].length;
  }
  return out + escapeHtml(code.slice(last));
}

/* ── chat: conversations ─────────────────────────────────────────── */
// The last twenty conversations live in localStorage, with the stats bar of
// every answer saved alongside its text. A benchmark you cannot reread is not
// much of a benchmark.
var CONV_KEY = 'ainode_pocket_chats';
var CONV_LIMIT = 20;
var conversations = [];
var currentConv = null;
var live = null;
var cardModel = null;
var loadShape = '';
var instanceWatch = null;

function readConversations() {
  if (demo) return;
  try {
    var saved = JSON.parse(localStorage.getItem(CONV_KEY) || '[]');
    conversations = (saved instanceof Array) ? saved.slice(0, CONV_LIMIT) : [];
  } catch (err) { conversations = []; }
}

function writeConversations() {
  if (demo) return;
  if (conversations.length > CONV_LIMIT) conversations.length = CONV_LIMIT;
  try { localStorage.setItem(CONV_KEY, JSON.stringify(conversations)); }
  catch (err) { /* a full or blocked store is not worth a toast mid-answer */ }
}

function newConversation() {
  currentConv = { id: 'conv_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
                  title: 'New chat', created_at: Date.now(),
                  model: $('#chat-model').value || '', messages: [] };
  conversations.unshift(currentConv);
  if (conversations.length > CONV_LIMIT) conversations.length = CONV_LIMIT;
  writeConversations();
  renderConversation();
  renderConvList();
}

function openConversation(id) {
  conversations.forEach(function (conv) { if (conv.id === id) currentConv = conv; });
  if (currentConv && currentConv.model) {
    var select = $('#chat-model');
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === currentConv.model) { select.value = currentConv.model; break; }
    }
    chatModelChanged();
  }
  renderConversation();
  renderConvList();
}

function dropConversation(id) {
  conversations = conversations.filter(function (conv) { return conv.id !== id; });
  if (currentConv && currentConv.id === id) currentConv = null;
  writeConversations();
  renderConversation();
  renderConvList();
}

function saveCurrent() {
  if (!currentConv) return;
  currentConv.model = $('#chat-model').value || currentConv.model;
  for (var i = 0; i < currentConv.messages.length; i++) {
    var msg = currentConv.messages[i];
    if (msg.role === 'user') {
      currentConv.title = msg.content.slice(0, 34) + (msg.content.length > 34 ? '...' : '');
      break;
    }
  }
  writeConversations();
}

function renderConvList() {
  var list = $('#conversation-list');
  list.textContent = '';
  if (!conversations.length) {
    list.appendChild(el('div', 'conv-empty', 'No conversations yet.'));
    return;
  }
  conversations.forEach(function (conv) {
    var row = el('div', 'conv-item' + (currentConv && conv.id === currentConv.id ? ' active' : ''));
    var body = el('div', 'conv-item-content');
    body.appendChild(el('div', 'conv-item-title', conv.title || 'New chat'));
    var when = new Date(conv.created_at);
    body.appendChild(el('div', 'conv-item-date',
      when.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + '  ' +
      when.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })));
    row.appendChild(body);
    var kill = el('button', 'conv-delete', '×');
    kill.type = 'button';
    kill.title = 'Delete';
    kill.onclick = function (event) { event.stopPropagation(); dropConversation(conv.id); };
    row.appendChild(kill);
    row.onclick = function () { openConversation(conv.id); };
    list.appendChild(row);
  });
}

/* ── chat: turns, stats and thinking ─────────────────────────────── */
function renderConversation() {
  var turns = $('#chat-turns');
  turns.textContent = '';
  var messages = currentConv ? currentConv.messages : [];
  $('#chat-empty').hidden = messages.length > 0;
  messages.forEach(function (msg) { turns.appendChild(messageNode(msg)); });
  var log = $('#chat-log');
  log.scrollTop = log.scrollHeight;
}

function messageNode(msg) {
  var box = el('div', 'turn ' + (msg.role === 'user' ? 'user' : 'assistant'));
  var head = el('div', 'turn-who');
  head.appendChild(el('span', null, msg.role === 'user' ? 'you' : short(msg.model || 'assistant')));
  box.appendChild(head);
  if (msg.role === 'user') {
    var mine = el('div', 'turn-body');
    mine.textContent = msg.content;
    box.appendChild(mine);
    return box;
  }
  var think = el('details', 'turn-think');
  var summary = el('summary', null, 'thinking');
  think.appendChild(summary);
  var thinkBody = el('div', 'turn-think-body');
  think.appendChild(thinkBody);
  think.__summary = summary;
  think.__body = thinkBody;
  box.appendChild(think);
  var body = el('div', 'turn-body turn-md');
  // Its own line under the answer, because a stream that died part way has both
  // an answer and an error and they are not the same thing.
  var oops = el('div', 'turn-err turn-err-tail');
  oops.hidden = true;
  var stats = el('div', 'stats-slot');
  box.appendChild(body);
  box.appendChild(oops);
  box.appendChild(stats);
  box.__think = think;
  box.__body = body;
  box.__err = oops;
  box.__stats = stats;
  paintMessage(box, msg);
  return box;
}

function paintMessage(box, msg) {
  var think = box.__think;
  if (msg.reasoning) {
    think.hidden = false;
    think.__body.textContent = msg.reasoning;
    // Under fifty milliseconds there is no tenth of a second to print, and
    // "thinking, 0.0 s" reads as a measurement of nothing rather than as the
    // bare label the code already has for a duration nobody timed.
    think.__summary.textContent = Number(msg.think_s) >= 0.05
      ? 'thinking, ' + Number(msg.think_s).toFixed(1) + ' s' : 'thinking';
  } else {
    think.hidden = true;
  }
  if (msg.error && !msg.content) {
    box.__body.className = 'turn-body turn-err';
    box.__body.textContent = msg.error;
  } else if (!msg.content && msg.note) {
    // An empty answer is a result, not a blank bubble: say what happened.
    box.__body.className = 'turn-body card-note';
    box.__body.textContent = msg.note;
  } else {
    box.__body.className = 'turn-body turn-md';
    box.__body.innerHTML = renderMarkdown(msg.content);
  }
  // The gateway's timeout sentence ends "the tokens already streamed above are
  // real", and overwriting the answer with it left nothing above for it to
  // point at. When both exist, both are shown.
  box.__err.hidden = !(msg.error && msg.content);
  if (msg.error && msg.content) box.__err.textContent = msg.error;
  box.__stats.textContent = '';
  if (msg.stats) {
    box.__stats.appendChild(statsBar(msg.stats));
  } else if (msg.stats_missing) {
    // The route promises stats on both paths. When none arrive, show the hole
    // rather than filling it with numbers the browser guessed.
    var missing = el('div', 'stats-bar');
    missing.appendChild(chip('stats', 'none returned', true));
    box.__stats.appendChild(missing);
  }
}

function millis(value) {
  if (value === undefined || value === null || isNaN(Number(value))) return '-';
  var ms = Number(value);
  return ms < 1000 ? Math.round(ms) + ' ms' : (ms / 1000).toFixed(2) + ' s';
}

function chip(label, value, warn, title) {
  var span = el('span', 'stat-chip' + (warn ? ' warn' : ''), label);
  span.appendChild(el('b', null, value));
  if (title) span.title = title;
  return span;
}

// The reference's three live chips (TTFT, tok/s, tokens) in the same order,
// plus the four a fleet needs: the wall clock, the prompt side of the ledger,
// which box and model actually answered, and why generation stopped. Every
// number is the server's; nothing here is measured in the browser.
function statsBar(stats) {
  var bar = el('div', 'stats-bar');
  var inTokens = stats.prompt_tokens === undefined || stats.prompt_tokens === null
    ? '-' : String(stats.prompt_tokens);
  if (stats.cached_tokens) inTokens += ' (' + stats.cached_tokens + ' cached)';
  var where = (stats.device && (stats.device.name || stats.device.id)) || 'unknown device';
  if (stats.model) where += ' · ' + short(stats.model);
  bar.appendChild(chip('TTFT', millis(stats.ttft_ms),
    false, 'prefill ' + millis(stats.prefill_ms) +
    (stats.prefill_tok_s ? ' at ' + Number(stats.prefill_tok_s).toFixed(1) + ' tok/s' : '')));
  bar.appendChild(chip('decode', stats.decode_tok_s
    ? Number(stats.decode_tok_s).toFixed(1) + ' tok/s' : '-'));
  bar.appendChild(chip('total', millis(stats.total_ms)));
  bar.appendChild(chip('in', inTokens));
  bar.appendChild(chip('out', stats.out_tokens === undefined || stats.out_tokens === null
    ? '-' : String(stats.out_tokens)));
  bar.appendChild(chip('on', where));
  // "length" is not a footnote on a reasoning model: it means the answer was
  // cut off, and on this firmware a modest cap is spent entirely on thinking.
  // The label is the field, finish_reason, rather than one of its two values,
  // which is what had the commonest answer reading "stop stop".
  bar.appendChild(chip('finish', stats.finish_reason || '-', stats.finish_reason === 'length',
    stats.finish_reason === 'length'
      ? 'the token budget ran out before the model finished' : ''));
  return bar;
}

/* ── chat: the model card ────────────────────────────────────────── */
function chatModelChanged() {
  var model = $('#chat-model').value;
  if (!model || model === cardModel) return;
  cardModel = model;
  var box = $('#model-card');
  box.textContent = '';
  box.appendChild(el('div', 'empty', 'reading the model record...'));
  api('/api/model_card?model=' + encodeURIComponent(model))
    .then(function (card) { if (card.model === cardModel) paintModelCard(card); })
    .catch(function (err) {
      box.textContent = '';
      box.appendChild(el('div', 'turn-err', err.message));
    });
}

// The device sends input and output as single words ("Text" / "Vector") while
// the route declares lists, so both shapes are read and neither is invented.
function capText(value) {
  if (value === undefined || value === null) return '-';
  if (value instanceof Array) return value.length ? value.join(', ') : '-';
  return String(value);
}

function paintModelCard(card) {
  var box = $('#model-card');
  box.textContent = '';
  var head = el('div', 'card-header');
  head.appendChild(el('div', 'card-title', 'Model card'));
  head.appendChild(el('span', 'pill' + (card.can_chat ? '' : ' flat'),
                      card.can_chat ? 'chat' : 'no chat'));
  box.appendChild(head);

  var caps = card.capabilities || null;
  var rows = el('dl', 'kv');
  [['name', card.name || short(card.model)],
   ['id', card.model],
   ['type', card.type || '-'],
   ['params', card.params || '-'],
   ['size', card.size_bytes ? bytes(card.size_bytes) : '-'],
   ['npu', card.npu_usage ? card.npu_usage + ' units' : '-'],
   ['input', caps ? capText(caps.input) : '-'],
   ['output', caps ? capText(caps.output) : '-']
  ].forEach(function (row) {
    rows.appendChild(el('dt', null, row[0]));
    rows.appendChild(el('dd', null, row[1]));
  });
  box.appendChild(rows);

  var where = el('div', 'model-where');
  var loaded = card.loaded_on || [];
  if (!loaded.length) {
    where.appendChild(el('div', 'card-note',
      'Not loaded on any device. Nothing can be routed to it until it is.'));
  } else {
    loaded.forEach(function (row) {
      var line = el('div', 'model-where-row');
      line.appendChild(el('span', 'dot' + (row.status === 'running' ? '' : ' off')));
      line.appendChild(el('span', null, row.device_name || row.device_id));
      line.appendChild(el('span', 'meta', row.status || 'loaded'));
      where.appendChild(line);
    });
  }
  box.appendChild(where);

  if (card.desc) box.appendChild(el('div', 'model-desc', card.desc));
  if (card.catalog_url) {
    var link = el('a', 'card-link', 'Catalogue entry');
    link.href = card.catalog_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    box.appendChild(link);
  }
}

/* ── chat: the instances rail ────────────────────────────────────── */
var railDevices = [];

function loadInstances() {
  return api('/api/instances')
    .then(paintInstances)
    .catch(function (err) {
      var list = $('#instances-list');
      list.textContent = '';
      list.appendChild(el('div', 'turn-err', err.message));
    });
}

function paintInstances(payload) {
  var devices = payload.devices || [];
  var instances = payload.instances || [];
  railDevices = devices;
  $('#instances-count').textContent = instances.length + ' loaded';

  var box = $('#instances-devices');
  box.textContent = '';
  devices.forEach(function (device) {
    // The route declares "reachable"; the rest of the app has always called
    // the same thing "online". Read whichever arrived rather than inventing a
    // second liveness idea.
    var up = device.reachable === undefined ? device.online !== false : device.reachable;
    var row = el('div', 'rail-device');
    var head = el('div', 'rail-device-head');
    head.appendChild(el('span', 'dot' + (up ? '' : ' off')));
    head.appendChild(el('span', null, device.device_name || device.device_id));
    head.appendChild(el('b', null, (device.npu_used === undefined ? '-' : device.npu_used) +
                                   ' / ' + (device.npu_total === undefined ? '-' : device.npu_total)));
    row.appendChild(head);
    if (device.npu_total) {
      row.appendChild(bar(100 * Number(device.npu_used || 0) / Number(device.npu_total), 80, 95));
    }
    box.appendChild(row);
  });

  var list = $('#instances-list');
  list.textContent = '';
  if (!instances.length) {
    list.appendChild(el('div', 'instances-empty',
      'Nothing loaded. Inference needs a loaded model.'));
  }
  instances.forEach(function (inst) {
    var loading = inst.status && inst.status !== 'running';
    var card = el('div', 'instance-card' + (loading ? ' loading' : ''));
    var name = el('div', 'instance-model', short(inst.model));
    name.title = inst.model;
    card.appendChild(name);
    var meta = el('div', 'instance-meta');
    meta.appendChild(el('span', null, (inst.npu_usage || '?') + ' units'));
    meta.appendChild(el('span', null, inst.device_name || inst.device_id));
    if (inst.instance_id) meta.appendChild(el('span', null, short(inst.instance_id)));
    card.appendChild(meta);
    var foot = el('div', 'instance-footer');
    foot.appendChild(el('span', 'instance-status' + (loading ? ' loading' : ''),
                        inst.status || 'running'));
    var kill = el('button', 'instance-delete', 'Unload');
    kill.type = 'button';
    kill.onclick = function () {
      kill.disabled = true;
      unload(inst.device_id, inst.model);
    };
    foot.appendChild(kill);
    card.appendChild(foot);
    list.appendChild(card);
  });

  fillLoadDevices(devices);
  updateLoadHint();
}

function fillLoadDevices(devices) {
  var select = $('#load-device');
  var shape = devices.map(function (device) { return device.device_id; }).join(',');
  if (select.getAttribute('data-shape') === shape) return;
  select.setAttribute('data-shape', shape);
  var previous = select.value;
  select.textContent = '';
  devices.forEach(function (device) {
    var option = el('option', null, device.device_name || device.device_id);
    option.value = device.device_id;
    select.appendChild(option);
  });
  if (previous && shape.indexOf(previous) !== -1) select.value = previous;
  fillLoadModels(true);
}

// Only a model that is installed on this device, can chat, and is not already
// loaded. The installed list is the same one the Models page reads.
function fillLoadModels(force) {
  var deviceId = $('#load-device').value;
  var select = $('#load-model');
  if (!deviceId) {
    select.textContent = '';
    select.appendChild(el('option', null, 'no device'));
    select.disabled = true;
    $('#load-btn').disabled = true;
    return;
  }
  if (!force && loadShape.indexOf(deviceId + '|') === 0) return;
  api('/api/models?device=' + encodeURIComponent(deviceId))
    .then(function (payload) {
      var rows = (payload.models || []).filter(function (row) {
        return row.chat && !row.loaded;
      });
      var shape = deviceId + '|' + rows.map(function (row) { return row.model_id; }).join(',');
      if (shape === loadShape) return;
      loadShape = shape;
      var previous = select.value;
      select.textContent = '';
      if (!rows.length) {
        select.appendChild(el('option', null, 'nothing idle to load'));
        select.disabled = true;
        $('#load-btn').disabled = true;
      } else {
        select.disabled = false;
        $('#load-btn').disabled = false;
        rows.forEach(function (row) {
          var option = el('option', null,
                          row.model_id + '  ·  ' + (row.npu_usage || '?') + ' units');
          option.value = row.model_id;
          option.setAttribute('data-npu', String(row.npu_usage || 0));
          select.appendChild(option);
        });
        var ids = rows.map(function (row) { return row.model_id; });
        select.value = ids.indexOf(previous) !== -1 ? previous : ids[0];
      }
      updateLoadHint();
    })
    .catch(function (err) { toast(err.message, true); });
}

// Pocket refuses an over-budget load here as well as at the server, because
// the device does not: it accepts the start, reports loading, and drops the
// instance a moment later without ever saying no.
function updateLoadHint() {
  var hint = $('#load-hint');
  var select = $('#load-model');
  var deviceId = $('#load-device').value;
  var device = null;
  railDevices.forEach(function (row) { if (row.device_id === deviceId) device = row; });
  hint.className = 'load-hint';
  if (!device) { hint.textContent = 'Register a device first.'; return; }
  var name = device.device_name || device.device_id;
  var free = Number(device.npu_total || 0) - Number(device.npu_used || 0);
  var option = select.options[select.selectedIndex];
  if (select.disabled || !option || !option.value) {
    hint.textContent = free + ' of ' + (device.npu_total || '?') +
                       ' NPU units free on ' + name + '.';
    return;
  }
  var need = Number(option.getAttribute('data-npu') || 0);
  if (need && need > free) {
    hint.className = 'load-hint warn';
    hint.textContent = short(option.value) + ' asks for ' + need + ' NPU units and only ' +
      free + ' are free on ' + name + '. The device would accept the load and roll it ' +
      'back a moment later without saying so, so it is refused here.';
    $('#load-btn').disabled = true;
    return;
  }
  $('#load-btn').disabled = false;
  hint.textContent = short(option.value) + ' asks for ' + (need || '?') +
                     ' of the ' + free + ' units free on ' + name + '.';
}

function startLoad() {
  var deviceId = $('#load-device').value;
  var model = $('#load-model').value;
  if (!deviceId || !model) return toast('pick a device and a model', true);
  $('#load-btn').disabled = true;
  api('/api/instances/load', { method: 'POST',
                               body: { device_id: deviceId, model: model } })
    .then(function () {
      toast('loading ' + short(model));
      watchInstances(deviceId, model, true);
    })
    .catch(function (err) { toast(err.message, true); })
    .then(function () { $('#load-btn').disabled = false; });
}

function unload(deviceId, model) {
  api('/api/instances/unload', { method: 'POST',
                                 body: { device_id: deviceId, model: model } })
    .then(function () {
      toast('unloading ' + short(model));
      watchInstances(deviceId, model, false);
    })
    .catch(function (err) { toast(err.message, true); loadInstances(); });
}

// A load is only proven by the instance turning up as running. The device
// reports "loading" either way, so the panel keeps polling until it is
// running or gone, and says which happened.
function watchInstances(deviceId, model, wantRunning) {
  var tries = 0;
  if (instanceWatch) clearTimeout(instanceWatch);
  function step() {
    instanceWatch = setTimeout(function () {
      tries++;
      api('/api/instances').then(function (payload) {
        paintInstances(payload);
        var found = null;
        (payload.instances || []).forEach(function (row) {
          if (row.device_id === deviceId && row.model === model) found = row;
        });
        if (wantRunning && found && found.status === 'running') {
          loadShape = '';
          fillLoadModels(true);
          return toast(short(model) + ' is running on this device');
        }
        if (wantRunning && !found && tries > 1) {
          loadShape = '';
          fillLoadModels(true);
          return toast(short(model) + ' never came up: the device rolled the load back, ' +
                       'which is what it does when the NPU budget does not fit.', true);
        }
        if (!wantRunning && !found) {
          loadShape = '';
          fillLoadModels(true);
          return toast(short(model) + ' unloaded');
        }
        if (tries < 20) step();
      }).catch(function () { if (tries < 20) step(); });
    }, 1500);
  }
  step();
}

/* ── chat: sending ───────────────────────────────────────────────── */
function chatConfig() {
  var temperature = parseFloat($('#chat-temp').value);
  if (isNaN(temperature)) temperature = 0.7;
  var maxTokens = parseInt($('#chat-max-tokens').value, 10);
  if (isNaN(maxTokens) || maxTokens < 1) maxTokens = 700;
  return { temperature: temperature, max_tokens: maxTokens,
           system: $('#chat-system').value.trim(),
           thinking: $('#chat-thinking').checked,
           stream: $('#chat-stream').checked };
}

// The whole conversation goes up, minus anything that failed and minus the
// empty turn being written right now. Reasoning is never sent back: it is the
// model's scratch paper, and it would be charged as prompt tokens.
function historyFor(conv) {
  var out = [];
  conv.messages.forEach(function (msg) {
    if (msg.error || !msg.content) return;
    out.push({ role: msg.role, content: msg.content });
  });
  return out;
}

function scrollLog() {
  var log = $('#chat-log');
  if (log.scrollHeight - log.scrollTop - log.clientHeight < 160) {
    log.scrollTop = log.scrollHeight;
  }
}

function send(text, maxOverride) {
  if (chatBusy) return;
  var model = $('#chat-model').value;
  if (!model || $('#chat-model').disabled) {
    return toast('no chat model is loaded. Load one on the Models page.', true);
  }
  if (!currentConv) newConversation();
  var config = chatConfig();
  if (maxOverride) config.max_tokens = maxOverride;

  $('#chat-empty').hidden = true;
  var turns = $('#chat-turns');
  var mine = { role: 'user', content: text };
  currentConv.messages.push(mine);
  turns.appendChild(messageNode(mine));
  var reply = { role: 'assistant', model: model, content: '', reasoning: '',
                think_s: null, stats: null, error: null, note: null };
  var body = { model: model, messages: historyFor(currentConv), stream: config.stream,
               thinking: config.thinking, temperature: config.temperature,
               max_tokens: config.max_tokens, system: config.system };
  currentConv.messages.push(reply);
  var node = messageNode(reply);
  turns.appendChild(node);
  live = { msg: reply, node: node, started: Date.now(), streamed: config.stream,
           thinking: config.thinking, finish: null };
  chatBusy = true;
  $('#chat-send').disabled = true;
  saveCurrent();
  renderConvList();
  scrollLog();

  if (demo) return demoChat(body);
  if (!config.stream) {
    api('/api/chat', { method: 'POST', body: body })
      .then(function (payload) {
        var choice = (payload.choices || [{}])[0] || {};
        var message = choice.message || {};
        reply.content = message.content || '';
        if (config.thinking) reply.reasoning = message.reasoning_content || '';
        if (choice.finish_reason) live.finish = choice.finish_reason;
        if (payload.stats) handleStats(payload.stats);
      })
      .catch(function (err) { reply.error = err.message; })
      .then(function () { finishTurn(); });
    return;
  }
  streamChat(body);
}

function streamChat(body) {
  var eventName = '';
  fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                       body: JSON.stringify(body) })
    .then(function (resp) {
      if (!resp.ok) {
        return resp.text().then(function (text) {
          var payload = {};
          try { payload = JSON.parse(text); } catch (err) { payload = {}; }
          throw new Error((payload.error && payload.error.message) || ('HTTP ' + resp.status));
        });
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) return;
          buffer += decoder.decode(chunk.value, { stream: true });
          var parts = buffer.split('\n');
          buffer = parts.pop();
          parts.forEach(function (raw) {
            var line = raw.replace(/\r$/, '');
            // A blank line ends an SSE frame, so the event name only applies
            // to the data lines that follow it.
            if (!line.trim()) { eventName = ''; return; }
            if (line.indexOf('event:') === 0) { eventName = line.slice(6).trim(); return; }
            if (line.indexOf('data:') !== 0) return;
            var data = line.slice(5).trim();
            if (data === '[DONE]') return;
            var frame = null;
            try { frame = JSON.parse(data); } catch (err) { return; }
            // The server relays the device's chunks and appends one frame of
            // its own, named "stats", after the last of them.
            if (eventName === 'stats') handleStats(frame);
            else handleFrame(frame);
          });
          return pump();
        });
      }
      return pump();
    })
    .catch(function (err) { if (live) live.msg.error = err.message; })
    .then(function () { finishTurn(); });
}

function handleFrame(frame) {
  if (!live) return;
  if (frame.error) {
    live.msg.error = frame.error.message || String(frame.error);
    repaintLive();
    return;
  }
  if (frame.stats) handleStats(frame.stats);
  // The final usage chunk carries "choices": [], and the opening chunk carries
  // content: null, so neither the choice nor the delta may be assumed.
  var choice = (frame.choices || [])[0] || {};
  var delta = choice.delta || {};
  if (choice.finish_reason) live.finish = choice.finish_reason;
  var touched = false;
  if (live.thinking && typeof delta.reasoning_content === 'string' && delta.reasoning_content) {
    live.msg.reasoning += delta.reasoning_content;
    touched = true;
  }
  if (typeof delta.content === 'string' && delta.content) {
    // The first word of the answer is when thinking stopped.
    if (!live.msg.content && live.msg.reasoning && live.msg.think_s === null) {
      live.msg.think_s = (Date.now() - live.started) / 1000;
    }
    live.msg.content += delta.content;
    touched = true;
  }
  if (touched) repaintLive();
}

function handleStats(stats) {
  if (!live || !stats) return;
  live.msg.stats = stats;
  if (stats.finish_reason) live.finish = stats.finish_reason;
  repaintLive();
}

function repaintLive() {
  paintMessage(live.node, live.msg);
  var think = live.node.__think;
  // Open while it is the only thing on screen, folded away once the answer
  // starts: the reasoning is context, not the reply.
  if (!think.hidden) think.open = !live.msg.content;
  scrollLog();
}

function finishTurn() {
  if (live) {
    var msg = live.msg;
    var node = live.node;
    // A stream that carried reasoning and never reached an answer thought right
    // up to the end, so the whole request is the thinking time. Nothing watches
    // that boundary when the reply arrives in one piece: there the request is
    // prefill plus reasoning plus the entire answer, and labelling that
    // "thinking" made the same model and the same question report two different
    // durations depending on whether Stream was ticked. The device does not
    // split its timings at the end of the chain of thought either, so there is
    // no honest number to print and the label stays bare.
    if (msg.reasoning && msg.think_s === null && live.streamed) {
      msg.think_s = (Date.now() - live.started) / 1000;
    }
    if (msg.stats && !msg.stats.finish_reason && live.finish) {
      msg.stats.finish_reason = live.finish;
    }
    if (!msg.content && !msg.error) {
      msg.note = live.finish === 'length'
        ? 'No answer text came back. The token budget was spent on thinking, which is ' +
          'what a reasoning model does with a tight cap: raise Max tokens, or turn ' +
          'Thinking off.'
        : 'The model returned an empty answer.';
    }
    if (!msg.stats && !msg.error) msg.stats_missing = true;
    live = null;
    paintMessage(node, msg);
    if (node.__think && !node.__think.hidden) node.__think.open = !msg.content;
  }
  chatBusy = false;
  $('#chat-send').disabled = false;
  saveCurrent();
  renderConvList();
  scrollLog();
  refresh();
}

function wireChat() {
  $('#chat-form').onsubmit = function (event) {
    event.preventDefault();
    var input = $('#chat-input');
    var text = input.value.trim();
    if (!text) return;
    input.value = '';
    send(text);
  };
  $('#chat-input').onkeydown = function (event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      $('#chat-form').dispatchEvent(new Event('submit'));
    }
  };
  $('#chat-model').onchange = function () { chatModelChanged(); saveCurrent(); };
  $('#new-chat').onclick = function () { newConversation(); };
  $('#load-device').onchange = function () { fillLoadModels(true); };
  $('#load-model').onchange = updateLoadHint;
  $('#load-btn').onclick = startLoad;
  // One listener for every copy button the renderer will ever write, so the
  // markdown can be redrawn on each delta without rebinding anything.
  $('#chat-log').addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('.code-copy-btn') : null;
    if (!button) return;
    var wrapper = button.closest('.code-block-wrapper');
    var code = wrapper ? wrapper.querySelector('code') : null;
    if (!code || !navigator.clipboard) return;
    navigator.clipboard.writeText(code.textContent).then(function () {
      button.textContent = 'Copied';
      button.classList.add('copied');
      setTimeout(function () {
        button.textContent = 'Copy';
        button.classList.remove('copied');
      }, 1400);
    });
  });
}

/* ── chat: fixtures for ?demo=1 ──────────────────────────────────── */
// With ?demo=1 every route this view reads is answered from a fixture inside
// the page, so the three columns, the stats bar, the thinking block, the
// markdown and a load in flight can all be seen with no device and no server.
// Nothing here is written to localStorage and nothing leaves the browser.
var demo = null;

var DEMO_REASONING = [
  'The user is asking about decode throughput on the Tiiny. I should keep the answer ',
  'short and give them the three things that actually move the number: prompt length, ',
  'the cache, and what else is resident on the NPU. A code sample helps here, and they ',
  'will want the curl equivalent too since this is a benchmarking tool.'
].join('');

var DEMO_ANSWER = [
  '## Decode throughput on the Tiiny',
  '',
  'The **8B** model settles around *22 tok/s* once the prompt is warm. The number in',
  "the bar below is the device's own `predicted_per_second`, not a browser guess.",
  '',
  'Three things move it:',
  '',
  '1. Prompt length, because prefill is charged once and then amortised',
  '2. Whether the prompt was cached, which the `in` chip reports',
  '3. What else is resident, since NPU units are residency and not compute',
  '',
  '- Units are memory residency, not a compute reservation',
  '- Several models can be resident; only one of them runs at a time',
  '- A load that does not fit is rolled back without a word',
  '',
  '```python',
  'import json',
  'import urllib.request',
  '',
  '',
  'def ask(prompt, max_tokens=120):',
  '    """One turn against the local gateway."""',
  '    body = {"model": "Qwen/Qwen3-8B", "max_tokens": max_tokens,',
  '            "messages": [{"role": "user", "content": prompt}]}',
  '    req = urllib.request.Request("/api/chat", json.dumps(body).encode())',
  '    with urllib.request.urlopen(req) as resp:  # 240 s is the gateway cap',
  '        return json.load(resp)["stats"]',
  '```',
  '',
  'Or from a shell, reading the same numbers this page shows:',
  '',
  '```bash',
  '# every field below comes from the device, not from Pocket',
  'curl -s localhost:8430/api/chat \\',
  '  -H \'Content-Type: application/json\' \\',
  '  -d \'{"model": "Qwen/Qwen3-8B", "max_tokens": 120,',
  '       "messages": [{"role": "user", "content": "hi"}]}\' | jq .stats',
  '```',
  '',
  'The Bench tab runs the same path in a loop and saves the result.'
].join('\n');

function demoSetup() {
  demo = {
    devices: [{ device_id: 'tiiny-0001', device_name: 'Tiiny', npu_used: 84,
                npu_total: 100, reachable: true }],
    instances: [
      { device_id: 'tiiny-0001', device_name: 'Tiiny', model: 'Qwen/Qwen3-8B',
        npu_usage: 28, status: 'running', instance_id: 'inst-a1c4' },
      { device_id: 'tiiny-0001', device_name: 'Tiiny', model: 'Qwen/Qwen3-Embedding-0.6B',
        npu_usage: 6, status: 'running', instance_id: 'inst-b2d7' },
      { device_id: 'tiiny-0001', device_name: 'Tiiny', model: 'Qwen/Qwen3-4B',
        npu_usage: 50, status: 'loading', instance_id: 'inst-c3e9' }
    ],
    installed: [
      { model_id: 'Qwen/Qwen3-8B', type: 'Text Generation', params: '8B',
        size: 4254394943, npu_usage: 28, status: 'running', chat: true, loaded: true },
      { model_id: 'Qwen/Qwen3-4B', type: 'Text Generation', params: '4B',
        size: 2483027968, npu_usage: 50, status: 'loading', chat: true, loaded: true },
      { model_id: 'Qwen/Qwen3-1.7B', type: 'Text Generation', params: '1.7B',
        size: 1073741824, npu_usage: 12, status: 'ready', chat: true, loaded: false },
      { model_id: 'Qwen/Qwen3-14B', type: 'Text Generation', params: '14B',
        size: 8589934592, npu_usage: 40, status: 'ready', chat: true, loaded: false },
      { model_id: 'Qwen/Qwen3-Embedding-0.6B', type: 'Embedding', params: '0.6B',
        size: 639582208, npu_usage: 6, status: 'running', chat: false, loaded: true },
      { model_id: 'hexgrad/Kokoro-82M', type: 'Text to Speech', params: '82M',
        size: 327155712, npu_usage: 4, status: 'ready', chat: false, loaded: false }
    ]
  };
  conversations = [demoConversation()];
  currentConv = conversations[0];
}

function demoConversation() {
  return {
    id: 'conv_demo', title: 'What decode rate should I expect', created_at: Date.now(),
    model: 'Qwen/Qwen3-8B',
    messages: [
      { role: 'user', content: 'What decode rate should I expect from the 8B on this box, and how do I measure it myself?' },
      { role: 'assistant', model: 'Qwen/Qwen3-8B', content: DEMO_ANSWER,
        reasoning: DEMO_REASONING, think_s: 4.2, error: null, note: null,
        stats: { ttft_ms: 318, prefill_ms: 122.5, prefill_tok_s: 146.97,
                 decode_tok_s: 22.76, prompt_tokens: 19, out_tokens: 214,
                 cached_tokens: 1, total_ms: 9641, finish_reason: 'stop',
                 device: { id: 'tiiny-0001', name: 'Tiiny' }, model: 'Qwen/Qwen3-8B' } }
    ]
  };
}

function demoApi(path, options) {
  return new Promise(function (resolve, reject) {
    setTimeout(function () {
      var payload = null;
      try { payload = demoRoute(path, options || {}); }
      catch (err) { return reject(err); }
      if (payload === null) return reject(new Error('no route for ' + path));
      resolve(payload);
    }, 30);
  });
}

function demoRoute(path, options) {
  var body = options.body || {};
  var device = demo.devices[0];
  if (path === '/api/state') return demoState();
  if (path.indexOf('/api/model_card') === 0) {
    return demoCard(decodeURIComponent((path.split('model=')[1] || '')));
  }
  if (path === '/api/instances') {
    return { instances: demo.instances.slice(), devices: demo.devices.slice() };
  }
  if (path === '/api/instances/load') {
    var row = null;
    demo.installed.forEach(function (entry) { if (entry.model_id === body.model) row = entry; });
    if (!row) throw new Error(body.model + ' is not installed on ' + device.device_name + '.');
    if (!row.chat) throw new Error(row.model_id + ' cannot chat, so loading it here would not help.');
    if (device.npu_used + row.npu_usage > device.npu_total) {
      throw new Error(row.model_id + ' needs ' + row.npu_usage + ' NPU units and only ' +
                      (device.npu_total - device.npu_used) + ' are free.');
    }
    var instance = { device_id: device.device_id, device_name: device.device_name,
                     model: row.model_id, npu_usage: row.npu_usage, status: 'loading',
                     instance_id: 'inst-' + Math.random().toString(36).slice(2, 6) };
    demo.instances.push(instance);
    device.npu_used += row.npu_usage;
    row.loaded = true;
    setTimeout(function () { instance.status = 'running'; row.status = 'running'; }, 3200);
    return { ok: true };
  }
  if (path === '/api/instances/unload') {
    demo.instances = demo.instances.filter(function (entry) {
      if (entry.model !== body.model || entry.device_id !== body.device_id) return true;
      device.npu_used -= entry.npu_usage;
      return false;
    });
    demo.installed.forEach(function (entry) {
      if (entry.model_id === body.model) { entry.loaded = false; entry.status = 'ready'; }
    });
    return { ok: true };
  }
  if (path.indexOf('/api/models?') === 0) return { models: demo.installed.slice() };
  if (path.indexOf('/api/catalog') === 0) return { catalog: [] };
  if (path.indexOf('/api/bench') === 0) return { history: [], current: null };
  return null;
}

function demoState() {
  var device = demo.devices[0];
  var running = demo.instances.map(function (entry) { return entry.model; });
  return {
    version: '0.1.3',
    endpoint: { base_url: '127.0.0.1:8430/v1' },
    summary: { devices: 1, online: 1, models: demo.installed.length,
               models_ready: 1, loaded: running.length },
    chat_models: [{ model_id: 'Qwen/Qwen3-8B', where: 'Tiiny', devices: ['tiiny-0001'] }],
    devices: [{
      id: device.device_id, name: device.device_name, online: true,
      address: '172.17.7.177', gateway: '172.17.7.177', error: null,
      firmware: { tiiny_os: '1.4.2', serial: 'TN-0001' },
      transport: { plane: 'lan', gateway: 'vhost', planes: { lan: '172.17.7.177' },
                   usb_linked: false },
      npu_units: { used: device.npu_used, total: device.npu_total,
                   available: device.npu_total - device.npu_used, percent: device.npu_used },
      npu_memory: { used_mb: 18432, total_mb: 32768, percent: 56, utilization_percent: 0 },
      storage: { free_bytes: 341234567890, percent: 38 },
      cpu: { percent: 22, cores: 12 },
      lock: { busy: false, waiting: 0, shared: true, held_for: 0 },
      thermal: { available: false, reason: 'temp_c and power_w read null on this firmware' },
      running: running,
      instances: demo.instances.map(function (entry) {
        return { model_id: entry.model, port: 8901, npu_usage: entry.npu_usage };
      })
    }]
  };
}

function demoCard(modelId) {
  var row = null;
  demo.installed.forEach(function (entry) { if (entry.model_id === modelId) row = entry; });
  if (!row) throw new Error(modelId + ' is not installed on any device.');
  var loaded = [];
  demo.instances.forEach(function (entry) {
    if (entry.model === modelId) {
      loaded.push({ device_id: entry.device_id, device_name: entry.device_name,
                    status: entry.status });
    }
  });
  return {
    model: row.model_id, name: short(row.model_id), type: row.type, params: row.params,
    size_bytes: row.size, npu_usage: row.npu_usage,
    capabilities: { input: 'Text', output: row.chat ? 'Text' : 'Vector' },
    loaded_on: loaded, can_chat: row.chat,
    catalog_url: 'https://huggingface.co/' + row.model_id,
    desc: 'Qwen3 is the latest generation of large language models in the Qwen family. ' +
          'This record is a fixture: with ?demo=1 nothing is read from a device.'
  };
}

function chunkText(text, size) {
  var out = [];
  for (var i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
  return out;
}

function demoChat(body) {
  var reasoning = body.thinking ? DEMO_REASONING : '';
  var answer = DEMO_ANSWER;
  var stats = { ttft_ms: body.thinking ? 2840 : 296, prefill_ms: 131.4,
                prefill_tok_s: 152.3, decode_tok_s: 22.41,
                prompt_tokens: 24 + Math.round(body.messages.length * 12),
                out_tokens: Math.round(answer.length / 3.6) + (reasoning ? 62 : 0),
                cached_tokens: 1, total_ms: 9820, finish_reason: 'stop',
                device: { id: 'tiiny-0001', name: 'Tiiny' }, model: body.model };
  if (!body.stream) {
    live.msg.content = answer;
    live.msg.reasoning = reasoning;
    // No think_s: a reply that arrives in one piece has nothing that saw when
    // the chain of thought stopped, and the fixture shows what the real route
    // shows or it is not worth having.
    handleStats(stats);
    return finishTurn();
  }
  var queue = [];
  chunkText(reasoning, 26).forEach(function (piece) {
    queue.push({ choices: [{ index: 0, finish_reason: null,
                             delta: { reasoning_content: piece } }] });
  });
  chunkText(answer, 26).forEach(function (piece) {
    queue.push({ choices: [{ index: 0, finish_reason: null, delta: { content: piece } }] });
  });
  queue.push({ choices: [{ index: 0, finish_reason: 'stop', delta: {} }] });
  var index = 0;
  function tick() {
    if (!live) return;
    if (index >= queue.length) { handleStats(stats); return finishTurn(); }
    handleFrame(queue[index++]);
    setTimeout(tick, 16);
  }
  setTimeout(tick, 140);
}

/* ── bench ───────────────────────────────────────────────────────── */
function fillBenchDevices() {
  var select = $('#bench-device');
  var previous = select.value;
  select.textContent = '';
  (state ? state.devices : []).forEach(function (device) {
    var option = el('option', null, device.name);
    option.value = device.id;
    select.appendChild(option);
  });
  if (previous && [].some.call(select.options,
                               function (o) { return o.value === previous; })) {
    select.value = previous;
  }
  fillBenchModels();
}

// Every section of the suite is a chat completion, so the list is the loaded
// chat models on the chosen box and nothing else. Same source as the Chat
// page's picker: state.chat_models, which the server builds from what the
// device says each model is. Benchmarking an embedding model produced a saved
// run whose every section read "failed".
function benchModelsFor(deviceId) {
  return ((state && state.chat_models) ? state.chat_models : []).filter(
    function (row) { return row.devices.indexOf(deviceId) !== -1; });
}

function fillBenchModels() {
  var select = $('#bench-model');
  var previous = select.value;
  var note = $('#bench-note');
  var deviceId = $('#bench-device').value;
  var rows = benchModelsFor(deviceId);
  select.textContent = '';
  note.textContent = '';
  if (!rows.length) {
    select.appendChild(el('option', null, 'no chat model loaded'));
    select.disabled = true;
    $('#bench-run').disabled = true;
    note.appendChild(document.createTextNode(
      'Nothing that can be benchmarked is loaded on this device, because every '
      + 'test here is a chat completion. Load a text generation model on the '));
    var link = el('button', 'linklike', 'Models page');
    link.type = 'button';
    link.onclick = function () { show('models'); };
    note.appendChild(link);
    note.appendChild(document.createTextNode('.'));
    return;
  }
  select.disabled = false;
  $('#bench-run').disabled = benchBusy;
  rows.forEach(function (row) {
    var option = el('option', null, row.model_id);
    option.value = row.model_id;
    select.appendChild(option);
  });
  var ids = rows.map(function (row) { return row.model_id; });
  select.value = ids.indexOf(previous) !== -1 ? previous : ids[0];
}

function loadBench() {
  api('/api/bench').then(function (payload) {
    paintBenchHistory(payload.history);
    if (payload.current) paintBenchRun(payload.current);
  }).catch(function (err) { toast(err.message, true); });
}

function paintBenchRun(run) {
  var log = $('#bench-log');
  var text = run.lines.join('\n');
  if (run.state === 'running') text += '\n\nrunning, ' + run.elapsed_s + 's elapsed';
  if (run.state === 'error') text += '\n\nfailed: ' + run.error;
  log.textContent = text || 'starting';
  // Follow a live run, but leave a finished one at the top where its header
  // says which device and model produced the numbers.
  log.scrollTop = run.state === 'running' ? log.scrollHeight : 0;
  if (run.state === 'running') {
    benchBusy = true;
    $('#bench-run').disabled = true;
  }
  if (run.state === 'running' && !benchPoll) {
    benchPoll = setInterval(function () {
      api('/api/bench').then(function (payload) {
        if (!payload.current || payload.current.state !== 'running') {
          clearInterval(benchPoll);
          benchPoll = null;
          benchBusy = false;
          paintBenchHistory(payload.history);
          $('#bench-run').disabled = false;
        }
        if (payload.current) paintBenchRun(payload.current);
      });
    }, 900);
  }
}

function paintBenchHistory(history) {
  $('#bench-count').textContent = history.length + ' saved';
  var rows = history.map(function (row) {
    var tr = el('tr', 'clickable');
    tr.appendChild(el('td', 'id', row.label || row.saved_as));
    tr.appendChild(el('td', null, row.device_name || ''));
    tr.appendChild(el('td', 'id', short(row.model)));
    tr.appendChild(el('td', 'num', row.decode_tok_s || '-'));
    tr.appendChild(el('td', 'num', row.aggregate_tok_s
      ? row.aggregate_tok_s + ' @ ' + row.parallel : '-'));
    tr.appendChild(el('td', 'num', (row.npu_units || '-') + '/' + (row.npu_total || '-')));
    tr.appendChild(el('td', 'id', row.stamp || ''));
    tr.onclick = function () {
      api('/api/bench/result?name=' + encodeURIComponent(row.saved_as))
        .then(function (record) {
          var box = $('#bench-detail');
          box.hidden = false;
          box.textContent = JSON.stringify(record, null, 2);
        });
    };
    return tr;
  });
  table($('#bench-table'),
        ['Label', 'Device', 'Model', 'Decode tok/s', 'Aggregate', 'NPU', 'When'], rows);
}

function wireBench() {
  $('#bench-device').onchange = fillBenchModels;
  $('#bench-run').onclick = function () {
    var device = $('#bench-device').value;
    if (!device) return toast('register a device first', true);
    var only = [];
    var boxes = $('#bench-tests').querySelectorAll('input');
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].checked) only.push(boxes[i].value);
    }
    var model = $('#bench-model').disabled ? '' : $('#bench-model').value;
    if (!model) return toast('no chat model is loaded on this device', true);
    benchBusy = true;
    this.disabled = true;
    api('/api/bench', { method: 'POST',
                        body: { device: device, model: model,
                                label: $('#bench-label').value.trim() || 'run',
                                only: only } })
      .then(function (payload) { paintBenchRun(payload.current); })
      .catch(function (err) {
        benchBusy = false;
        toast(err.message, true);
        $('#bench-run').disabled = false;
      });
  };
}

/* ── boot ────────────────────────────────────────────────────────── */
function boot() {
  var items = document.querySelectorAll('.nav-item');
  for (var i = 0; i < items.length; i++) {
    items[i].onclick = function () { show(this.dataset.view); };
  }
  $('#refresh').onclick = function () {
    refresh().then(function () {
      if (view === 'models') loadModels();
      if (view === 'bench') loadBench();
    });
  };
  wireAdd();
  wireChat();
  wireBench();

  var params = new URLSearchParams(location.search);
  // ?demo=1 answers every route from a fixture inside the page, so the chat
  // view can be read, reviewed and screenshotted with no device and no server.
  if (params.get('demo') === '1') demoSetup();
  readConversations();
  renderConvList();
  refresh().then(function () {
    var wanted = params.get('view');
    if (demo && !wanted) wanted = 'chat';
    if (wanted && ['overview', 'models', 'chat', 'bench'].indexOf(wanted) !== -1) {
      show(wanted);
    }
    // ?q= sends one real request on load, with an optional &max= token budget.
    // Handy for a demo or a screenshot: the answer comes from the endpoint, not
    // from a fixture.
    var question = params.get('q');
    if (question) {
      show('chat');
      if (params.get('stream') === '0') $('#chat-stream').checked = false;
      send(question, parseInt(params.get('max'), 10) || 0);
    }
  });
  // No background poll here on purpose: the page loads state once, and the
  // Refresh button (wired above) is the only thing that fetches again after
  // that. A silent timer refreshing every few seconds is what wiped a
  // half-typed unlock password out from under someone -- the fix isn't a
  // smarter timer, it's not having one.
}

document.addEventListener('DOMContentLoaded', boot);
