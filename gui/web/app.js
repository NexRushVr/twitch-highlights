/* Twitch Highlights — frontend logic.
 * Talks to Python via window.pywebview.api.<method>(...); receives unsolicited
 * progress events via window.onProgress(event). No framework, no build step. */

'use strict';

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

/* Known pipeline phases (labels refined live from phase_start events). */
const PHASES = [
  { key: 'source', label: 'Resolving source' },
  { key: 'audio', label: 'Extracting audio' },
  { key: 'transcribe', label: 'Transcribing with Whisper' },
  { key: 'llm', label: 'Selecting clips' },
  { key: 'peaks', label: 'Cross-referencing audio peaks' },
  { key: 'clip', label: 'Cutting clips' },
  { key: 'caption', label: 'Burning captions' },
];

let currentSource = 'kick';
let currentRunDir = null;

const run = {
  active: false,
  estimatedTotal: null,
  virtualT0: null,
  lastOverall: 0,
  timer: null,
};

/* ------------------------------------------------------------------ utils */
function fmt(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}

let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 3200);
}

function setPill(text, cls) {
  const p = $('statusPill');
  p.textContent = text;
  p.className = 'pill ' + cls;
}

/* ------------------------------------------------------------------ nav */
document.querySelectorAll('.nav-item').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach((b) => b.classList.remove('active'));
    document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
    btn.classList.add('active');
    const view = btn.dataset.view;
    $('view-' + view).classList.add('active');
    if (view === 'results') loadRuns();
    if (view === 'settings') loadConfig();
  });
});

/* ------------------------------------------------------------------ run form */
$('sourceSeg').querySelectorAll('button').forEach((btn) => {
  btn.addEventListener('click', () => {
    $('sourceSeg').querySelectorAll('button').forEach((b) => b.classList.remove('active'));
    btn.classList.add('active');
    currentSource = btn.dataset.source;
    document.querySelectorAll('.source-input').forEach((el) => {
      el.classList.toggle('hidden', el.dataset.for !== currentSource);
    });
  });
});

$('clipMode').addEventListener('change', () => {
  const phrase = $('clipMode').value === 'phrase';
  $('phraseWrap').classList.toggle('hidden', !phrase);
  $('maxClipsWrap').classList.toggle('hidden', phrase);
});

$('browseBtn').addEventListener('click', async () => {
  const res = await api().pick_file();
  if (res && res.path) $('localPath').value = res.path;
});

$('startBtn').addEventListener('click', startRun);
$('cancelBtn').addEventListener('click', async () => {
  $('cancelBtn').disabled = true;
  await api().cancel_run();
});

function gatherOpts() {
  const mode = $('clipMode').value;
  const opts = {
    source_type: currentSource,
    clip_mode: mode,
    force: $('forceChk').checked,
    verbose: $('verboseChk').checked,
    start_time: $('startTime').value,
    end_time: $('endTime').value,
  };
  if (currentSource === 'kick') opts.channel = $('kickChannel').value;
  else if (currentSource === 'vodvod') opts.channel = $('vodvodChannel').value;
  else if (currentSource === 'twitch') opts.url = $('twitchUrl').value;
  else if (currentSource === 'local') opts.path = $('localPath').value;
  if (mode === 'phrase') opts.trigger_phrase = $('triggerPhrase').value;
  else opts.max_clips = parseInt($('maxClips').value, 10) || 10;
  return opts;
}

async function startRun() {
  const res = await api().start_run(gatherOpts());
  if (res.status !== 'started') {
    toast(res.message || 'Could not start.');
    return;
  }
  enterRunningUI(res.command);
}

function enterRunningUI(command) {
  run.active = true;
  run.estimatedTotal = null;
  run.virtualT0 = null;
  run.lastOverall = 0;
  $('startBtn').disabled = true;
  $('cancelBtn').classList.remove('hidden');
  $('cancelBtn').disabled = false;
  $('runCmd').textContent = command || '';
  setPill('Running', 'pill-run');

  $('monitor').classList.remove('hidden');
  $('runResult').classList.add('hidden');
  $('phaseLabel').textContent = 'Resolving source…';
  $('etaLine').textContent = '';
  $('overallPct').textContent = '0%';
  const fill = $('overallFill');
  fill.style.width = '0%';
  fill.classList.add('indeterminate');

  renderPhases(0);
  clearInterval(run.timer);
  run.timer = setInterval(tick, 250);
}

function renderPhases(activeIndex) {
  const ol = $('phaseList');
  ol.innerHTML = '';
  PHASES.forEach((p, i) => {
    const idx = i + 1;
    let state = 'pending';
    if (idx < activeIndex) state = 'done';
    else if (idx === activeIndex) state = 'active';
    const li = document.createElement('li');
    li.className = 'phase ' + state;
    li.dataset.idx = idx;
    li.innerHTML =
      `<span class="dot">${state === 'done' ? '✓' : ''}</span>` +
      `<span class="plabel">${p.label}</span>` +
      `<span class="ptime"></span>`;
    ol.appendChild(li);
  });
}

function setBar(frac) {
  frac = Math.max(0, Math.min(frac, 1));
  $('overallFill').style.width = (frac * 100).toFixed(1) + '%';
  $('overallPct').textContent = Math.round(frac * 100) + '%';
}

function tick() {
  if (!run.estimatedTotal || run.virtualT0 == null) return;
  const elapsed = (performance.now() - run.virtualT0) / 1000;
  let frac = elapsed / run.estimatedTotal;
  frac = Math.max(run.lastOverall, Math.min(frac, 0.99));
  setBar(frac);
  const remaining = run.estimatedTotal - elapsed;
  $('etaLine').textContent = `~${fmt(remaining)} left of ~${fmt(run.estimatedTotal)}`;
}

/* ------------------------------------------------------------ progress feed */
window.onProgress = function (ev) {
  if (!ev || !ev.type) return;
  switch (ev.type) {
    case 'run_started':
      break;
    case 'set_total':
      run.estimatedTotal = ev.estimated_total;
      run.virtualT0 = performance.now() - (ev.elapsed || 0) * 1000;
      $('overallFill').classList.remove('indeterminate');
      tick();
      break;
    case 'phase_start':
      markPhase(ev.index, ev.label);
      if (run.estimatedTotal == null && typeof ev.overall === 'number') {
        $('overallFill').classList.remove('indeterminate');
        setBar(Math.max(run.lastOverall, ev.overall));
      }
      break;
    case 'phase_end':
      if (typeof ev.overall === 'number') run.lastOverall = Math.max(run.lastOverall, ev.overall);
      finishPhase(ev.index, ev.phase_elapsed);
      if (run.estimatedTotal == null) setBar(run.lastOverall);
      break;
    case 'run_end':
      endRun(ev);
      break;
  }
};

function markPhase(index, label) {
  $('phaseLabel').textContent = label ? label + '…' : 'Working…';
  const ol = $('phaseList');
  ol.querySelectorAll('.phase').forEach((li) => {
    const idx = parseInt(li.dataset.idx, 10);
    if (idx < index) {
      li.className = 'phase done';
      li.querySelector('.dot').textContent = '✓';
    } else if (idx === index) {
      li.className = 'phase active';
      li.querySelector('.dot').textContent = '';
      if (label) li.querySelector('.plabel').textContent = label;
    }
  });
}

function finishPhase(index, elapsed) {
  const li = $('phaseList').querySelector(`.phase[data-idx="${index}"]`);
  if (!li) return;
  li.className = 'phase done';
  li.querySelector('.dot').textContent = '✓';
  if (typeof elapsed === 'number') li.querySelector('.ptime').textContent = fmt(elapsed);
}

function endRun(ev) {
  run.active = false;
  clearInterval(run.timer);
  $('startBtn').disabled = false;
  $('cancelBtn').classList.add('hidden');
  $('overallFill').classList.remove('indeterminate');
  setPill('Ready', 'pill-ok');

  const box = $('runResult');
  box.classList.remove('hidden', 'ok', 'skip', 'err');
  const outcome = ev.outcome;

  if (outcome === 'success') {
    setBar(1);
    $('phaseLabel').textContent = 'Done';
    $('etaLine').textContent = '';
    box.classList.add('ok');
    box.innerHTML = `<strong>Done — clips are ready.</strong>
      <div class="rr-actions">
        <button class="btn btn-primary" id="rrView">View clips</button>
        <button class="btn btn-ghost" id="rrFolder">Open folder</button>
      </div>`;
    wireResultButtons(ev.manifest);
  } else if (outcome === 'skipped') {
    setBar(1);
    $('phaseLabel').textContent = 'Already generated';
    $('etaLine').textContent = '';
    box.classList.add('skip');
    box.innerHTML = `<strong>Already generated.</strong>
      <div>${esc(ev.message || 'Clips for this VOD-date already exist. Tick “Force re-run” to regenerate.')}</div>
      <div class="rr-actions">
        <button class="btn btn-primary" id="rrView">View clips</button>
        <button class="btn btn-ghost" id="rrFolder">Open folder</button>
      </div>`;
    wireResultButtons(ev.manifest);
  } else if (outcome === 'cancelled') {
    $('phaseLabel').textContent = 'Cancelled';
    box.classList.add('skip');
    box.innerHTML = `<strong>Run cancelled.</strong><div>${esc(ev.message || '')}</div>`;
  } else {
    $('phaseLabel').textContent = 'Error';
    box.classList.add('err');
    box.innerHTML = `<strong>The run failed (exit ${ev.returncode}).</strong>
      <div>Common fixes: make sure the Ollama app is running and the channel / URL is correct.</div>
      ${ev.error ? `<pre>${esc(ev.error)}</pre>` : ''}`;
  }
}

function wireResultButtons(manifest) {
  const view = $('rrView'), folder = $('rrFolder');
  if (view) view.addEventListener('click', () => {
    // Switching to the Results tab repopulates the run list and selects the
    // newest run (the one that just finished), so no extra load is needed.
    document.querySelector('.nav-item[data-view="results"]').click();
  });
  if (folder) folder.addEventListener('click', async () => {
    if (manifest) await api().open_containing(manifest);
  });
}

/* ------------------------------------------------------------------ results */
async function loadRuns() {
  const { runs } = await api().list_runs();
  const sel = $('runSelect');
  sel.innerHTML = '';
  if (!runs || !runs.length) {
    $('resultsGrid').innerHTML = '';
    $('resultsEmpty').classList.remove('hidden');
    currentRunDir = null;
    return;
  }
  $('resultsEmpty').classList.add('hidden');
  runs.forEach((r) => {
    const o = document.createElement('option');
    o.value = r.manifest;
    o.textContent = `${r.label}  (${r.count} clips)`;
    sel.appendChild(o);
  });
  loadResults(runs[0].manifest);
}

$('runSelect').addEventListener('change', (e) => loadResults(e.target.value));
$('refreshRuns').addEventListener('click', loadRuns);
$('openRunFolder').addEventListener('click', async () => {
  if (currentRunDir) await api().open_containing(currentRunDir);
});

async function loadResults(manifest) {
  const data = await api().load_results(manifest);
  currentRunDir = data.dir;
  const grid = $('resultsGrid');
  grid.innerHTML = '';
  if (data.error) { toast(data.error); return; }
  if (!data.entries.length) {
    $('resultsEmpty').classList.remove('hidden');
    return;
  }
  $('resultsEmpty').classList.add('hidden');
  data.entries.forEach((c) => grid.appendChild(clipCard(c)));
  // keep dropdown in sync if called from elsewhere
  if (manifest) $('runSelect').value = manifest;
}

function clipCard(c) {
  const div = document.createElement('div');
  div.className = 'clip-card' + (c.exists ? '' : ' missing');
  const score = (typeof c.score === 'number') ? `<span class="score">★ ${c.score.toFixed(2)}</span>` : '';
  const dur = (typeof c.duration === 'number') ? `${Math.round(c.duration)}s` : '';
  div.innerHTML = `
    <div class="cc-top">
      <span class="cc-name" title="${esc(c.name)}">${esc(c.name)}</span>
      ${c.reason ? `<span class="badge">${esc(c.reason)}</span>` : ''}
    </div>
    <div class="cc-desc">${esc(c.description || '')}</div>
    <div class="cc-meta">${score}${dur ? `<span>${dur}</span>` : ''}</div>
    <div class="cc-actions">
      <button class="btn btn-primary cc-open">Open</button>
      <button class="btn btn-ghost cc-folder">Folder</button>
    </div>`;
  div.querySelector('.cc-open').addEventListener('click', async () => {
    const target = c.captioned || c.file;
    const res = await api().open_path(target);
    if (res.status !== 'ok') toast(res.message || 'Could not open.');
  });
  div.querySelector('.cc-folder').addEventListener('click', () => api().open_containing(c.file));
  return div;
}

/* ------------------------------------------------------------------ preflight */
$('recheckBtn').addEventListener('click', runPreflight);

async function runPreflight() {
  const btn = $('recheckBtn');
  btn.disabled = true;
  $('preflightVerdict').textContent = 'Checking… (importing torch can take a few seconds)';
  $('preflightList').innerHTML = '';
  try {
    const res = await api().preflight();
    if (res.error) { $('preflightVerdict').textContent = res.error; return; }
    renderPreflight(res);
  } finally {
    btn.disabled = false;
  }
}

function renderPreflight(res) {
  $('preflightVerdict').textContent = res.ok
    ? 'Looks ready.'
    : 'Some checks need attention (see FAIL items).';
  const list = $('preflightList');
  list.innerHTML = '';
  let section = null;
  res.items.forEach((it) => {
    if (it.section && it.section !== section) {
      section = it.section;
      const h = document.createElement('div');
      h.className = 'check-section';
      h.textContent = section;
      list.appendChild(h);
    }
    const row = document.createElement('div');
    row.className = 'check-row';
    const label = { ok: 'OK', warn: 'WARN', fail: 'FAIL', info: '…' }[it.level] || '';
    row.innerHTML = `<span class="chip ${it.level}">${label}</span><span>${esc(it.text)}</span>`;
    list.appendChild(row);
  });
}

/* ------------------------------------------------------------------ settings */
const CFG_BOOL = ['burn_subtitles', 'cleanup_source', 'verbose'];
const CFG_NUM = ['max_clips'];
const CFG_KEYS = ['whisper_model', 'whisper_device', 'llm_backend', 'ollama_model',
  'openai_model', 'clip_mode', 'trigger_phrase', 'max_clips', 'quality',
  ...CFG_BOOL];

async function loadConfig() {
  const { config } = await api().get_config();
  CFG_KEYS.forEach((k) => {
    const el = $('cfg-' + k);
    if (!el) return;
    if (CFG_BOOL.includes(k)) el.checked = !!config[k];
    else if (config[k] != null) el.value = config[k];
  });
}

$('saveCfgBtn').addEventListener('click', async () => {
  const updates = {};
  CFG_KEYS.forEach((k) => {
    const el = $('cfg-' + k);
    if (!el) return;
    if (CFG_BOOL.includes(k)) updates[k] = el.checked;
    else if (CFG_NUM.includes(k)) updates[k] = parseInt(el.value, 10) || 0;
    else updates[k] = el.value;
  });
  const res = await api().save_config(updates);
  $('cfgMsg').textContent = res.status === 'saved' ? 'Saved ✓' : (res.message || 'Error');
  if (res.status === 'saved') setTimeout(() => ($('cfgMsg').textContent = ''), 2500);
});

/* nightly */
function nightlyCfg(register) {
  const lines = (id) => $(id).value.split('\n').map((s) => s.trim()).filter(Boolean);
  const max = parseInt($('nl-max').value, 10) || 10;
  return {
    vodvod_channels: lines('nl-vodvod'),
    kick_channels: lines('nl-kick'),
    time: $('nl-time').value.trim(),
    max_clips_vodvod: max,
    max_clips_kick: max,
    register,
  };
}

$('writeNightlyBtn').addEventListener('click', () => doNightly(false));
$('registerNightlyBtn').addEventListener('click', () => doNightly(true));
$('unregisterNightlyBtn').addEventListener('click', async () => {
  const res = await api().unschedule_nightly();
  renderNightly(res);
  refreshStatus();
});

async function doNightly(register) {
  const res = await api().schedule_nightly(nightlyCfg(register));
  renderNightly(res);
  refreshStatus();
}

function renderNightly(res) {
  const box = $('nightlyMsg');
  if (res.status === 'registered') {
    box.innerHTML = `Registered ✓ — runs daily at ${esc(res.time)}.`;
  } else if (res.status === 'written') {
    box.innerHTML = `Wrote <code>${esc(res.path)}</code>. To register manually, run:<pre>${esc(res.command)}</pre>`;
  } else if (res.status === 'needs_elevation') {
    box.innerHTML = `Wrote the script, but registering needs an Administrator shell. Paste this into an elevated PowerShell:<pre>${esc(res.command)}</pre>`;
  } else if (res.status === 'removed') {
    box.innerHTML = 'Scheduled task removed.';
  } else {
    box.innerHTML = `<span style="color:var(--fail)">${esc(res.message || 'Error')}</span>`;
  }
}

/* ------------------------------------------------------------------ status */
async function refreshStatus() {
  const s = await api().get_status();
  if (s.running) setPill('Running', 'pill-run');
  else if (s.venv_ready) setPill('Ready', 'pill-ok');
  else setPill('Setup needed', 'pill-bad');
  $('unregisterNightlyBtn').classList.toggle('hidden', !s.nightly_registered);
  if (!s.venv_ready) {
    $('startBtn').disabled = true;
    toast('Setup not finished — run install.bat first.');
  }
}

/* ------------------------------------------------------------------ helpers */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ------------------------------------------------------------------ boot */
function init() {
  refreshStatus();
}
if (window.pywebview && window.pywebview.api) init();
else window.addEventListener('pywebviewready', init);
