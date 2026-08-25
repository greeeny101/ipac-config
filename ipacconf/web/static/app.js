const $ = (s) => document.querySelector(s);
let CODES = null, PROFILE = null, SAVED = [], UPLOAD = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data;
}
function post(path, body) {
  return api(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
}
function banner(sel, html, cls) {
  $(sel).innerHTML = html ? `<div class="banner ${cls||''}">${html}</div>` : '';
}
function say(html, cls) { banner('#status', html, cls); }
function saySaved(html, cls) { banner('#savedStatus', html, cls); }
function notesHtml(notes) {
  if (!notes || !notes.length) return '';
  return `<ul class="notes">${notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>`;
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function optionsHtml(selected) {
  let html = `<option value=""${selected ? '' : ' selected'}>- none -</option>`;
  for (const group of CODES.groups) {
    html += `<optgroup label="${esc(group.label)}">`;
    for (const code of group.codes) {
      const sel = code === selected ? ' selected' : '';
      html += `<option value="${esc(code)}"${sel}>${esc(code)}</option>`;
    }
    html += '</optgroup>';
  }
  if (selected && !CODES.groups.some(g => g.codes.includes(selected))) {
    html += `<option value="${esc(selected)}" selected>${esc(selected)} (raw)</option>`;
  }
  return html;
}

function renderPins(changed) {
  const byName = {};
  for (const pin of (PROFILE.pins || [])) byName[pin.name] = pin;
  const marked = new Set((changed || []).map(c => `${c.pin}.${c.field}`));
  let html = '';
  for (const group of CODES.pin_groups) {
    html += `<h2>${esc(group.label)}</h2><table><tr>
      <th>pin</th><th>action</th><th>alternate (shifted)</th><th>is shift key</th></tr>`;
    for (const name of group.pins) {
      const pin = byName[name] || {};
      const mark = (field) => marked.has(`${name}.${field}`) ? ' class="changed"' : '';
      // A profile may label a pin with what it means on the panel ("GP1 South
      // (A / Cross)"). The board only knows the code, so the label sits beside
      // the pin rather than replacing anything in the select.
      const desc = pin.description
        ? `<div class="desc">${esc(pin.description)}</div>` : '';
      html += `<tr><td class="pin">${esc(name)}${desc}</td>
        <td><select${mark('action')} data-pin="${name}" data-field="action">${optionsHtml(pin.action || '')}</select></td>
        <td><select${mark('alternate_action')} data-pin="${name}" data-field="alternate_action">${optionsHtml(pin.alternate_action || '')}</select></td>
        <td><input${mark('shift')} type="checkbox" data-pin="${name}" data-field="shift" ${pin.shift ? 'checked' : ''}></td>
      </tr>`;
    }
    html += '</table>';
  }
  $('#pins').innerHTML = html;
  repaintLive();  // a re-render must not drop what is being held down
}

function collect() {
  const pins = {};
  for (const el of document.querySelectorAll('#pins [data-pin]')) {
    const name = el.dataset.pin;
    pins[name] = pins[name] || { name };
    pins[name][el.dataset.field] = el.type === 'checkbox' ? el.checked : el.value;
  }
  // Descriptions have no form field of their own, so carry them across or a
  // download - or a round trip through import - would drop them.
  for (const pin of (PROFILE.pins || [])) {
    if (pin.description && pins[pin.name]) pins[pin.name].description = pin.description;
  }
  return {
    schemaVersion: 2.0, resourceType: 'ipac2-pins', deviceClass: 'ipac2',
    debounce: PROFILE.debounce || 'standard',
    paclink: !!PROFILE.paclink,
    pins: Object.values(pins),
  };
}

function renderChanges(result, target) {
  target = target || '#status';
  const what = result.source ? ` ${esc(result.source)}` : '';
  if (!result.changes.length) {
    return banner(target, `No change - the board already matches${what || ' this'}.`
      + notesHtml(result.notes), 'ok');
  }
  const rows = result.changes.map(c =>
    `[${String(c.offset).padStart(3)}] ${c.meaning.padEnd(18)} ${c.before} -> ${c.after}`).join('\n');
  const head = result.written
    ? `<strong>Written${what}.</strong> ${result.backup ? 'Backup: ' + esc(result.backup) : ''}`
    : `<strong>${result.changes.length} byte(s) would change.</strong>`;
  banner(target, `${head}${notesHtml(result.notes)}<pre>${esc(rows)}</pre>`,
         result.written ? 'ok' : '');
  if (result.warning) {
    $(target).insertAdjacentHTML('afterbegin',
      `<div class="banner warn">${esc(result.warning)}</div>`);
  }
  // Dinput takes a write and never commits it. Say so before the button is
  // pressed, not after the next power cycle has thrown the work away.
  if (result.flash_warning) {
    $(target).insertAdjacentHTML('afterbegin',
      `<div class="banner warn">${esc(result.flash_warning)}</div>`);
  }
  // The board picks its mode from what it is sent. Say which way it is
  // expected to go, so "nothing happened" can be told apart from "it wrote
  // fine and stayed in the mode it was already in".
  if (result.mode_note) {
    $(target).insertAdjacentHTML('afterbegin',
      `<div class="banner">${esc(result.mode_note)}</div>`);
  }
  // Assigning gamepad actions to the six pins the mode hotkeys use leaves no
  // way back except the power-up backdoor. Say it before the button, not
  // after the panel has stopped responding to Start1+P1SW4.
  if (result.hotkey_warning) {
    $(target).insertAdjacentHTML('afterbegin',
      `<div class="banner warn">${esc(result.hotkey_warning)}</div>`);
  }
  // Xinput has no config interface: this would be the last write we can make.
  if (result.xinput_warning) {
    $(target).insertAdjacentHTML('afterbegin',
      `<div class="banner warn">${esc(result.xinput_warning)}</div>`);
  }
}

async function loadDevice() {
  try {
    const d = await api('/api/device');
    $('#device').innerHTML = `<dl>
      <dt>board</dt><dd>${esc(d.name)} (${esc(d.vendor)}:${esc(d.product)})${d.fake ? ' <em>fake</em>' : ''}</dd>
      <dt>mode</dt><dd><strong>${esc(d.mode)}</strong></dd>
      <dt>firmware</dt><dd>${esc(d.firmware)} - ${esc(d.firmware_note)}</dd>
      <dt>gamepad</dt><dd>${d.supports_gamepad ? 'supported' : 'not in this firmware'}</dd>
      <dt>node</dt><dd>${esc(d.path)} (interface ${d.interface})</dd></dl>`;
  } catch (err) {
    $('#device').innerHTML = `<div class="banner err">${esc(err.message)}</div>`;
  }
}

async function loadConfig() {
  try {
    PROFILE = await api('/api/config');
    renderPins();
  } catch (err) {
    $('#pins').innerHTML = `<div class="banner err">${esc(err.message)}</div>`;
  }
}

async function send(dry) {
  say('working...');
  try {
    renderChanges(await post('/api/config', {
      profile: collect(),
      dry_run: dry,
      xinput: $('#xinput').checked,
    }));
    if (!dry) { loadConfig(); loadSaved(); }
  } catch (err) {
    say(esc(err.message), 'err');
  }
}

// -- live input
//
// The board is a keyboard, so a press raises an input event on the cabinet.
// The server reads /dev/input, names the board action behind the event and
// resolves it to pins against the config it last read; all that is left here
// is lighting up the right cell. A tap is far too short to see, so a
// highlight is held for a minimum time regardless of when the release lands.

let WATCH = null;
const HELD = new Map();
const LIVE_MIN_MS = 450;
const LIVE_MAX_MS = 6000;  // a release we never saw must not light a row forever

function paintLive(pin, field, on) {
  const cell = document.querySelector(
    `#pins [data-pin="${pin}"][data-field="${field}"]`);
  if (!cell) return;
  cell.classList.toggle('live', on);
  const row = cell.closest('tr');
  if (row) row.classList.toggle('live', on || !!row.querySelector('.live'));
}

function repaintLive() {
  for (const held of HELD.values()) paintLive(held.pin, held.field, true);
}

function holdPin(p) {
  const key = `${p.pin}.${p.field}`;
  const existing = HELD.get(key);
  if (existing) clearTimeout(existing.timer);
  HELD.set(key, {
    pin: p.pin, field: p.field, since: Date.now(),
    timer: setTimeout(() => releasePin(p, true), LIVE_MAX_MS),
  });
  paintLive(p.pin, p.field, true);
}

function releasePin(p, now) {
  const key = `${p.pin}.${p.field}`;
  const held = HELD.get(key);
  if (!held) return;
  clearTimeout(held.timer);
  const left = now ? 0 : Math.max(0, LIVE_MIN_MS - (Date.now() - held.since));
  if (left) {
    held.timer = setTimeout(() => releasePin(p, true), left);
    return;
  }
  HELD.delete(key);
  paintLive(p.pin, p.field, false);
}

function clearLive() {
  for (const held of HELD.values()) {
    clearTimeout(held.timer);
    paintLive(held.pin, held.field, false);
  }
  HELD.clear();
}

function describePins(e) {
  if (e.pins && e.pins.length) {
    const names = e.pins.map(
      p => p.field === 'action' ? p.pin : `${p.pin} (shifted)`).join(', ');
    return [names + (e.pins.length > 1 ? '  <- shared by several pins' : ''), false];
  }
  if (e.name) return ['no pin carries this code', true];
  if (e.muted) return ['not an action the board can store; hiding the rest of these', true];
  return ['not an action the board can store', true];
}

function logInput(e) {
  const [where, missed] = describePins(e);
  const hex = e.code === null || e.code === undefined
    ? '' : ` (0x${e.code.toString(16).padStart(2, '0')})`;
  const what = (e.name || `${e.kind} ${e.raw}`) + hex;
  const line = document.createElement('div');
  if (missed) line.className = 'miss';
  line.textContent = [
    new Date(e.ts * 1000).toLocaleTimeString(),
    e.held ? 'down' : 'up  ',
    what.padEnd(20),
    where.padEnd(30),
    e.node,
  ].join('  ');
  const log = $('#inputLog');
  log.insertBefore(line, log.firstChild);
  while (log.children.length > 40) log.removeChild(log.lastChild);
}

function onInput(e) {
  logInput(e);
  for (const pin of e.pins || []) {
    if (e.held) holdPin(pin); else releasePin(pin, false);
  }
}

function watchingText(info) {
  const bits = [];
  if (info.fake) bits.push('<strong>scripted input</strong> - no real hardware');
  const names = (info.devices || []).map(d => esc(
    d.node + (d.player ? ` (player ${d.player})` : ''))).join(', ');
  bits.push(names ? `watching ${names}` : 'watching nothing');
  if (info.grab) bits.push('<strong>presses are not reaching Batocera</strong>');
  if (info.matching === false) {
    bits.push('the board\'s config could not be read, so presses cannot be '
              + 'matched to pins - codes only');
  }
  return bits.join(' &middot; ');
}

function inputSay(html, cls) { banner('#inputStatus', html, cls); }

// The board is a keyboard, so with this page open on the cabinet its presses
// also arrive here as ordinary keystrokes: arrows and space scroll the page,
// space and Enter fire whatever button has focus, and arrows land on a focused
// pin dropdown and silently change what it says. The evdev grab only covers
// the nodes the monitor matched, so it is not an answer on its own. Swallow
// the lot for as long as the stream is open - there is nothing on this page to
// type into - but leave ctrl/cmd/alt combos alone so reload and close still
// work.
function swallowKeys(ev) {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  ev.preventDefault();
}

function watchKeys(on) {
  for (const type of ['keydown', 'keypress']) {
    if (on) window.addEventListener(type, swallowKeys, true);
    else window.removeEventListener(type, swallowKeys, true);
  }
}

async function preflight() {
  const info = await api('/api/input/devices');
  if (!info.fake && !info.devices.some(d => d.ours) && !$('#allDevices').checked) {
    throw new Error(info.note || 'no input device belongs to the board. '
      + 'If it is plugged in and `list` finds it, try "watch every input '
      + 'device" to see what the panel is actually talking to.');
  }
  const wanted = { grab: $('#grab').checked, all: $('#allDevices').checked };
  if (info.running && info.options
      && (info.options.grab !== wanted.grab || info.options.all !== wanted.all)) {
    throw new Error('another browser is already watching with different '
      + `options (exclusive ${info.options.grab ? 'on' : 'off'}, `
      + `all devices ${info.options.all ? 'on' : 'off'}). Match those, or stop `
      + 'watching there first.');
  }
  return info;
}

async function startWatching() {
  inputSay('starting...');
  try {
    await preflight();
  } catch (err) {
    return inputSay(esc(err.message), 'err');
  }
  const params = new URLSearchParams();
  if ($('#grab').checked) params.set('grab', '1');
  if ($('#allDevices').checked) params.set('all', '1');

  let opened = false;
  const source = new EventSource('/api/input/stream?' + params);
  WATCH = source;
  source.addEventListener('watching', (msg) => {
    opened = true;
    inputSay(watchingText(JSON.parse(msg.data)), 'ok');
  });
  source.addEventListener('fault', (msg) => {
    inputSay(esc(JSON.parse(msg.data).error), 'err');
    stopWatching();
  });
  watchKeys(true);
  if (document.activeElement) document.activeElement.blur();
  source.onmessage = (msg) => onInput(JSON.parse(msg.data));
  source.onerror = () => {
    // EventSource retries by itself unless the response was never usable,
    // which is how a refused stream arrives here.
    if (source.readyState === EventSource.CLOSED || !opened) {
      inputSay('the server refused the stream - check its log', 'err');
      stopWatching();
    } else {
      inputSay('connection lost, reconnecting...', 'warn');
    }
  };
  $('#watch').textContent = 'Stop watching';
}

function stopWatching() {
  if (WATCH) { WATCH.close(); WATCH = null; }
  watchKeys(false);
  clearLive();
  $('#watch').textContent = 'Start watching';
}

function toggleWatching() {
  if (WATCH) { stopWatching(); inputSay(''); } else { startWatching(); }
}

// -- saved configurations

function savedUrl(id, suffix) {
  const [source, ...rest] = String(id).split('/');
  return `/api/saved/${encodeURIComponent(source)}/`
    + `${encodeURIComponent(rest.join('/'))}${suffix || ''}`;
}
function holdsText(e) {
  if (e.error) return 'unreadable: ' + e.error;
  const bits = [`${e.pins} pins`];
  if (e.macros) bits.push(`${e.macros} macros`);
  if (e.firmware) bits.push(`fw ${e.firmware}`);
  return bits.join(', ');
}

function renderSaved() {
  if (!SAVED.length) {
    $('#saved').innerHTML = '<p class="muted">Nothing saved yet. Every write to '
      + 'the board leaves a backup here.</p>';
    return;
  }
  let html = '<table><tr><th>file</th><th>saved</th><th>holds</th><th></th></tr>';
  for (const e of SAVED) {
    const title = e.label
      ? `<span class="name">${esc(e.label)}</span><div class="muted">${esc(e.name)}</div>`
      : `<span class="name">${esc(e.name)}</span>`;
    let buttons = '';
    if (!e.error) {
      buttons += `<button class="small" data-act="load" data-id="${esc(e.id)}">Load into form</button>`;
      if (e.has_raw) {
        buttons += `<button class="small" data-act="restore" data-id="${esc(e.id)}">Restore exactly</button>`;
        buttons += `<button class="small" data-act="compare" data-id="${esc(e.id)}">Compare to board</button>`;
      }
    }
    buttons += `<button class="small" data-act="download" data-id="${esc(e.id)}">Download</button>`;
    if (e.writable) {
      // An unreadable file cannot be relabelled - only removed.
      if (!e.error) {
        buttons += `<button class="small" data-act="label" data-id="${esc(e.id)}">Label</button>`;
      }
      buttons += `<button class="small danger" data-act="delete" data-id="${esc(e.id)}">Delete</button>`;
    }
    html += `<tr><td>${title} <span class="badge">${esc(e.source)}</span></td>
      <td class="when">${esc(e.mtime ? new Date(e.mtime * 1000).toLocaleString() : '')}</td>
      <td class="muted">${esc(holdsText(e))}</td>
      <td><div class="row">${buttons}</div></td></tr>`;
  }
  $('#saved').innerHTML = html + '</table>';
}

async function loadSaved() {
  try {
    SAVED = (await api('/api/saved')).saved;
    renderSaved();
  } catch (err) {
    $('#saved').innerHTML = `<div class="banner err">${esc(err.message)}</div>`;
  }
}

// Merge a saved profile into the form. The server does the merging so that
// "pins it does not name keep what they have" means the same thing here as it
// does for `apply` on the command line.
async function importInto(source) {
  saySaved('working...');
  try {
    const res = await post('/api/import', Object.assign({ base: collect() }, source));
    PROFILE = res.profile;
    renderPins(res.changed);
    const n = res.changed.length;
    saySaved(`<strong>Loaded ${esc(res.source)} into the form.</strong> `
      + (n ? `${n} field(s) highlighted below. ` : 'It matches the form already. ')
      + 'Nothing reaches the board until you press <em>Write to board</em>.'
      + notesHtml(res.notes), n ? '' : 'ok');
  } catch (err) {
    saySaved(esc(err.message), 'err');
  }
}

async function restoreExactly(source, name, dry) {
  if (!dry && !confirm(`Restore ${name} exactly?\n\n`
      + 'This writes all 256 bytes back to the board - every pin, plus any '
      + 'macros - replacing what is on it now. The current config is backed '
      + 'up first.')) return;
  saySaved('working...');
  try {
    renderChanges(await post('/api/restore',
      Object.assign({ dry_run: dry, xinput: $('#xinput').checked },
                    source)), '#savedStatus');
    if (!dry) { loadConfig(); loadSaved(); }
  } catch (err) {
    saySaved(esc(err.message), 'err');
  }
}

async function relabel(id, current) {
  const label = prompt('Name this saved config (blank to clear):', current || '');
  if (label === null) return;
  try {
    SAVED = (await post(savedUrl(id, '/label'), { label })).saved;
    renderSaved();
  } catch (err) {
    saySaved(esc(err.message), 'err');
  }
}

async function removeSaved(id, name) {
  if (!confirm(`Delete ${name} from the cabinet? This cannot be undone.`)) return;
  try {
    const res = await api(savedUrl(id), { method: 'DELETE' });
    SAVED = res.saved;
    renderSaved();
    saySaved(`Deleted ${esc(res.deleted)}.`, 'ok');
  } catch (err) {
    saySaved(esc(err.message), 'err');
  }
}

function renderUpload() {
  if (!UPLOAD) { $('#uploadRow').innerHTML = ''; return; }
  const raw = !!UPLOAD.profile.raw;
  $('#uploadRow').innerHTML = `<div class="row" style="margin-top:.5rem">
    <span class="name">${esc(UPLOAD.name)}</span>
    <button class="small" id="upLoad">Load into form</button>
    ${raw
      ? '<button class="small" id="upRestore">Restore exactly</button>'
        + '<button class="small" id="upCompare">Compare to board</button>'
      : '<span class="muted">no raw bytes in this file - form only</span>'}
  </div>`;
  const src = { profile: UPLOAD.profile, name: UPLOAD.name };
  $('#upLoad').onclick = () => importInto(src);
  if (raw) {
    $('#upRestore').onclick = () => restoreExactly(src, UPLOAD.name, false);
    $('#upCompare').onclick = () => restoreExactly(src, UPLOAD.name, true);
  }
}

$('#saved').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const id = btn.dataset.id;
  const entry = SAVED.find(e => e.id === id) || { name: id };
  switch (btn.dataset.act) {
    case 'load': return importInto({ source: id });
    case 'restore': return restoreExactly({ source: id }, entry.name, false);
    case 'compare': return restoreExactly({ source: id }, entry.name, true);
    case 'download': window.location = savedUrl(id, '/download'); return;
    case 'label': return relabel(id, entry.label);
    case 'delete': return removeSaved(id, entry.name);
  }
});

$('#upload').onchange = async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  UPLOAD = null;
  if (!file) return renderUpload();
  try {
    const profile = JSON.parse(await file.text());
    if (!profile || typeof profile !== 'object' || Array.isArray(profile)) {
      throw new Error('not a profile object');
    }
    UPLOAD = { name: file.name, profile };
    saySaved('');
  } catch (err) {
    saySaved(`Could not read ${esc(file.name)} as a profile: ${esc(err.message)}`, 'err');
  }
  renderUpload();
};

$('#read').onclick = () => { say(''); loadConfig(); };
$('#preview').onclick = () => send(true);
$('#write').onclick = () => {
  if (confirm('Write this configuration to the board?')) send(false);
};
$('#clear').onclick = () => {
  // A blank board is how you tell two pins carrying the same code apart: clear
  // everything, put one action back, and whatever arrives came from that pin.
  // The shift-key checkboxes are left alone on purpose - clearing Start1's
  // would take the hold-to-switch-mode combos with it.
  const fields = document.querySelectorAll('#pins select[data-pin]');
  if (!fields.length) return say('the pin table has not loaded yet.', 'err');
  if (!confirm('Set every pin to none and write that to the board?\n\n'
      + 'The panel will do nothing until you fill pins back in. Shift-key '
      + 'flags are kept, and the board is backed up first.')) return;
  for (const el of fields) el.value = '';
  send(false);
};
$('#download').onclick = () => {
  const blob = new Blob([JSON.stringify(collect(), null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'ipac2-profile.json';
  a.click();
};

$('#watch').onclick = toggleWatching;
for (const id of ['#grab', '#allDevices']) {
  // Both options are properties of the connection, so changing one while
  // watching means opening a new stream.
  $(id).onchange = () => { if (WATCH) { stopWatching(); startWatching(); } };
}
window.addEventListener('beforeunload', stopWatching);

(async () => {
  CODES = await api('/api/codes');
  await loadDevice();
  await loadConfig();
  await loadSaved();
})();
