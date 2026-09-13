/* AINode Pocket UI. No framework, no build step, no CDN. */
'use strict';

var state = null;
var view = 'overview';
var modelDevice = null;
var benchPoll = null;
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
  if (name === 'chat') fillChatModels();
}

/* ── overview ────────────────────────────────────────────────────── */
function refresh() {
  return api('/api/state').then(function (payload) {
    state = payload;
    paintTop();
    paintOverview();
    if (view === 'chat') fillChatModels();
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
  var subParts = [device.address || device.gateway];
  if (firmware.tiiny_os) subParts.push('TiinyOS ' + firmware.tiiny_os);
  if (firmware.serial) subParts.push(firmware.serial);
  card.appendChild(el('div', 'device-sub', subParts.join('  ·  ')));

  if (!device.online) {
    card.appendChild(el('div', 'turn-err', device.error || 'unreachable'));
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

  if (memory.utilization_percent === 0) {
    card.appendChild(el('div', 'device-note',
      'NPU utilisation reads 0% on this firmware even mid-generation, so it is not ' +
      'shown as a load signal. Memory and unit accounting are live.'));
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
    var address = $('#add-address').value.trim();
    if (!address) return toast('enter an address', true);
    api('/api/discover', { method: 'POST', body: { address: address } })
      .then(function (payload) { renderFound(payload.found); })
      .catch(function (err) { toast(err.message, true); });
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
    row.appendChild(el('span', 'dot'));
    row.appendChild(el('code', null, hit.address));
    row.appendChild(el('span', 'meta',
      (hit.device.device_name || '?') + '  ·  ' + (hit.device.sn || '')));
    var add = el('button', 'btn small', 'Add');
    add.onclick = function () { addDevice(hit.address, $('#add-key').value.trim()); };
    row.appendChild(add);
    box.appendChild(row);
  });
}

function addDevice(address, key) {
  if (!address) return toast('enter an address', true);
  api('/api/devices', { method: 'POST', body: { address: address, key: key || '' } })
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
    tr.appendChild(el('td', 'num', bytes(model.size)));
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
    var value = Number(payload.progress || 0);
    fill.firstChild.style.width = value + '%';
    label.textContent = value.toFixed(0) + '%' +
      (payload.speed_human ? '  ' + payload.speed_human : '');
  };
  source.onerror = function () { source.close(); };
}

/* ── chat ────────────────────────────────────────────────────────── */
function readyModels() {
  var out = [];
  (state ? state.devices : []).forEach(function (device) {
    (device.running || []).forEach(function (modelId) {
      if (out.indexOf(modelId) === -1) out.push(modelId);
    });
  });
  return out.sort();
}

function firstReadyModel() {
  var models = readyModels();
  return models.length ? models[0] : null;
}

function fillChatModels() {
  var select = $('#chat-model');
  var previous = select.value;
  var models = readyModels();
  select.textContent = '';
  if (!models.length) {
    select.appendChild(el('option', null, 'no loaded model'));
    select.disabled = true;
    $('#chat-route').textContent = 'Load a model on the Models page first.';
    return;
  }
  select.disabled = false;
  models.forEach(function (modelId) {
    var option = el('option', null, modelId);
    option.value = modelId;
    select.appendChild(option);
  });
  if (models.indexOf(previous) !== -1) select.value = previous;
  $('#chat-route').textContent = 'routed to whichever device has it loaded';
}

function turn(who, cls) {
  var box = el('div', 'turn ' + cls);
  var head = el('div', 'turn-who');
  head.appendChild(el('span', null, who));
  box.appendChild(head);
  box.appendChild(el('div', 'turn-body'));
  return box;
}

function send(text) {
  if (chatBusy) return;
  var model = $('#chat-model').value;
  if (!model || $('#chat-model').disabled) return toast('no loaded model to ask', true);
  var log = $('#chat-log');
  if (log.querySelector('.chat-empty')) log.textContent = '';
  var mine = turn('you', 'user');
  mine.querySelector('.turn-body').textContent = text;
  log.appendChild(mine);
  var theirs = turn(short(model), 'assistant');
  log.appendChild(theirs);
  log.scrollTop = log.scrollHeight;
  chatBusy = true;
  $('#chat-send').disabled = true;

  var body = { model: model, max_tokens: 700,
               messages: [{ role: 'user', content: text }] };
  var streaming = $('#chat-stream').checked;
  var target = theirs.querySelector('.turn-body');
  var done = function () {
    chatBusy = false;
    $('#chat-send').disabled = false;
    log.scrollTop = log.scrollHeight;
    refresh();
  };

  if (!streaming) {
    api('/v1/chat/completions', { method: 'POST', body: body })
      .then(function (payload) {
        var choice = (payload.choices || [{}])[0] || {};
        target.textContent = (choice.message || {}).content || '';
        stamp(theirs, payload.ainode_pocket);
      })
      .catch(function (err) {
        target.className = 'turn-body turn-err';
        target.textContent = err.message;
      })
      .then(done);
    return;
  }

  body.stream = true;
  fetch('/v1/chat/completions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (resp) {
    if (!resp.ok) {
      return resp.json().then(function (payload) {
        throw new Error((payload.error && payload.error.message) || ('HTTP ' + resp.status));
      });
    }
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    var thinking = null;
    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done) return;
        buffer += decoder.decode(chunk.value, { stream: true });
        var parts = buffer.split('\n');
        buffer = parts.pop();
        parts.forEach(function (line) {
          line = line.trim();
          if (line.indexOf('data:') !== 0) return;
          var data = line.slice(5).trim();
          if (data === '[DONE]') return;
          var frame = {};
          try { frame = JSON.parse(data); } catch (err) { return; }
          if (frame.error) {
            target.className = 'turn-body turn-err';
            target.textContent = frame.error.message || 'device error';
            return;
          }
          var delta = ((frame.choices || [{}])[0] || {}).delta || {};
          if (delta.reasoning_content) {
            if (!thinking) {
              thinking = el('div', 'turn-think');
              theirs.appendChild(thinking);
            }
            thinking.textContent += delta.reasoning_content;
          }
          if (delta.content) target.textContent += delta.content;
          log.scrollTop = log.scrollHeight;
        });
        return pump();
      });
    }
    return pump();
  }).catch(function (err) {
    target.className = 'turn-body turn-err';
    target.textContent = err.message;
  }).then(done);
}

function stamp(node, info) {
  if (!info) return;
  node.querySelector('.turn-who').appendChild(
    el('span', 'meta', 'via ' + (info.device_name || info.device)));
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
  if (previous) select.value = previous;
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
  log.scrollTop = log.scrollHeight;
  if (run.state === 'running' && !benchPoll) {
    benchPoll = setInterval(function () {
      api('/api/bench').then(function (payload) {
        if (!payload.current || payload.current.state !== 'running') {
          clearInterval(benchPoll);
          benchPoll = null;
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
  $('#bench-run').onclick = function () {
    var device = $('#bench-device').value;
    if (!device) return toast('register a device first', true);
    var only = [];
    var boxes = $('#bench-tests').querySelectorAll('input');
    for (var i = 0; i < boxes.length; i++) {
      if (boxes[i].checked) only.push(boxes[i].value);
    }
    this.disabled = true;
    api('/api/bench', { method: 'POST',
                        body: { device: device, label: $('#bench-label').value.trim() || 'run',
                                only: only } })
      .then(function (payload) { paintBenchRun(payload.current); })
      .catch(function (err) { toast(err.message, true); $('#bench-run').disabled = false; });
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
  refresh().then(function () {
    var wanted = params.get('view');
    if (wanted && ['overview', 'models', 'chat', 'bench'].indexOf(wanted) !== -1) {
      show(wanted);
    }
    // ?q= sends one real request on load. Handy for a demo or a screenshot:
    // the answer below comes from the endpoint, not from a fixture.
    var question = params.get('q');
    if (question) {
      show('chat');
      send(question);
    }
  });
  setInterval(function () {
    if (!chatBusy && document.visibilityState === 'visible') refresh();
  }, 6000);
}

document.addEventListener('DOMContentLoaded', boot);
