/* Music Flow 前端逻辑
   设计要点：
   - 用 SSE 接后端事件流，日志实时滚动
   - 流水线卡片由后端 /api/pipeline 驱动（前端不硬编码步骤，保证与后端一致）
   - 两种模式只是「显示/隐藏单步按钮」的区别，底层调的是同一份步骤定义
*/
'use strict';

const $ = s => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

let MODE = 'auto';
let PIPELINE = [];
let STATUS = null;
let CONFIG = null;
const LOG_MAX = 3000;
let logCount = 0;

/* ══════════ 视图切换 ══════════ */
document.querySelectorAll('.nav-btn').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.nav-btn').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    $('#view-' + b.dataset.view).classList.add('active');
    if (b.dataset.view === 'runs') loadRuns();
    if (b.dataset.view === 'library') loadLibrary(false);
    if (b.dataset.view === 'blocked') loadBlocked();
    if (b.dataset.view === 'settings') renderSettings();
    if (b.dataset.view === 'logic') loadLogic();
  };
});

/* ══════════ 模式切换 ══════════ */
function setMode(m) {
  MODE = m;
  document.body.classList.toggle('mode-auto', m === 'auto');
  document.body.classList.toggle('mode-manual', m === 'manual');
  $('#btn-auto').classList.toggle('active', m === 'auto');
  $('#btn-manual').classList.toggle('active', m === 'manual');
  $('#auto-panel').style.display = m === 'auto' ? '' : 'none';
  $('#steps-tip').textContent = m === 'auto'
    ? '省心模式下按顺序自动执行；切到观察模式可单步运行'
    : '观察模式：每一步都可以单独点「运行」，方便定位问题';
  localStorage.setItem('mf_mode', m);
}
$('#btn-auto').onclick = () => setMode('auto');
$('#btn-manual').onclick = () => setMode('manual');

/* ══════════ 流水线卡片 ══════════ */
async function loadPipeline() {
  const r = await fetch('/api/pipeline').then(r => r.json());
  PIPELINE = r.steps;
  renderSteps();
}

function renderSteps() {
  const box = $('#steps');
  box.innerHTML = '';
  PIPELINE.forEach((s, i) => {
    const card = el('div', 'step');
    card.id = 'step-' + s.id;

    const top = el('div', 'step-top');
    top.append(el('span', 'step-name', s.name));
    top.append(el('span', 'step-state', '待运行'));
    card.append(top);
    card.append(el('div', 'step-desc', s.desc));

    const btn = el('button', 'step-run', '运行');
    btn.onclick = () => runStep(s.id);
    card.append(btn);

    box.append(card);
  });
}

function setStepState(id, state, text) {
  const card = $('#step-' + id);
  if (!card) return;
  card.classList.remove('s-running', 's-ok', 's-fail', 's-warn');
  if (state) card.classList.add('s-' + state);
  const st = card.querySelector('.step-state');
  if (st) st.textContent = text || ({
    running: '运行中…', ok: '完成', fail: '失败', warn: '待处理'
  }[state] || '待运行');
}

function markAllIdle() {
  PIPELINE.forEach(s => setStepState(s.id, null, '待运行'));
}

/* ══════════ 日志 ══════════ */
function appendLog(ev) {
  const box = $('#log');
  const onlyErr = $('#onlyerr').checked;
  if (onlyErr && ev.level !== 'error' && ev.level !== 'warn') return;

  const line = el('div', 'ln lv-' + (ev.level || 'out'));
  line.append(el('span', 't', ev.ts || ''));
  line.append(el('span', 'm', ev.message));
  box.append(line);
  logCount++;

  while (logCount > LOG_MAX && box.firstChild) {
    box.removeChild(box.firstChild);
    logCount--;
  }
  if ($('#autoscroll').checked) box.scrollTop = box.scrollHeight;
}

$('#btn-clear').onclick = () => { $('#log').innerHTML = ''; logCount = 0; };
$('#onlyerr').onchange = () => {
  // 重新筛一遍现有内容：简单起见直接清空提示
  $('#log').innerHTML = '';
  logCount = 0;
  appendLog({ ts: '', level: 'warn', message: '已切换筛选；后续日志按新规则显示' });
};

/* ══════════ 运行 ══════════ */
async function runAuto() {
  $('#btn-sync').disabled = true;
  $('#btn-stop').disabled = false;
  markAllIdle();
  await fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'auto' })
  });
}

async function runStep(id) {
  $('#btn-stop').disabled = false;
  await fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'manual', step: id })
  });
}

$('#btn-sync').onclick = runAuto;
$('#btn-stop').onclick = async () => {
  await fetch('/api/stop', { method: 'POST' });
};

/* ══════════ 状态轮询 ══════════ */
async function pollStatus() {
  try {
    const s = await fetch('/api/status').then(r => r.json());
    STATUS = s;
    const lib = s.library || {};
    $('#st-lib').textContent = lib.count != null ? `${lib.count} 首 · ${lib.gb} GB` : '—';

    const st = s.syncthing || {};
    if (!st.ok) {
      $('#st-phone').textContent = '未运行';
      $('#st-sync').textContent = '—';
      $('#st-phone').style.color = 'var(--fg-mute)';
    } else {
      $('#st-phone').textContent = st.phone_connected ? '已连接' : '未连接';
      $('#st-phone').style.color = st.phone_connected ? 'var(--ok)' : 'var(--err)';
      if (st.completion != null) {
        $('#st-sync').textContent = st.completion >= 100 || st.need_items === 0
          ? '已同步' : `${st.completion}% · 剩 ${st.need_mb} MB`;
        $('#st-sync').style.color = (st.completion >= 100 || st.need_items === 0)
          ? 'var(--ok)' : 'var(--warn)';
      }
    }

    const r = s.runner || {};
    $('#btn-sync').disabled = !!r.running;
    $('#btn-stop').disabled = !r.running;
    document.querySelectorAll('.step-run').forEach(b => { b.disabled = !!r.running; });
    if (r.results) {
      Object.entries(r.results).forEach(([k, v]) => {
        setStepState(k, v === 'ok' ? 'ok' : (v === 'warn' ? 'warn' : 'fail'));
      });
    }
    if (r.current) setStepState(r.current, 'running');
  } catch (e) { /* 静默 */ }
}

/* ══════════ SSE ══════════ */
function connectSSE() {
  const es = new EventSource('/api/events');
  es.onopen = () => $('#conn-dot').className = 'dot on';
  es.onerror = () => $('#conn-dot').className = 'dot off';
  es.onmessage = e => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.type === 'log') appendLog(ev);
    else if (ev.type === 'step_start') setStepState(ev.step, 'running');
    else if (ev.type === 'step_end') {
      setStepState(ev.step, ev.ok ? 'ok' : (ev.soft ? 'warn' : 'fail'));
      pollStatus();
    } else if (ev.type === 'run_end') {
      pollStatus();
    }
  };
}

/* ══════════ 设置页 ══════════ */
const FIELDS = [
  { grp: 'download', title: '下载 · 哪些歌会被下载',
    note: '决定抓取什么音质。默认只收无损及以上，避免被有损文件污染曲库。',
    items: [
      { k: 'accept_hires', t: '接受 24bit Hi-Res', d: '关闭后即使平台有 Hi-Res 也不下', type: 'bool' },
      { k: 'accept_lossless', t: '接受 16bit 无损（CD 规格）', d: '关闭后只收 Hi-Res', type: 'bool' },
      { k: 'accept_lossy', t: '接受有损（128k / 320k）', d: '⚠ 打开后低品质文件也会被下载。想补某些只有有损版本的歌时才开', type: 'bool', danger: true },
      { k: 'only_new_lossless', t: '无损只跟新歌', gates: 'lossless_target', d: '打开后：只下载你新收藏里只有无损版本的歌，不去回填历史无损。' +
          '此时下方的「无损目标数量」会被完全忽略（不用管它）。关闭后才按目标数量回填历史。',
        type: 'bool' },
      { k: 'lossless_target', t: '无损目标数量', d: '仅在「无损只跟新歌」关闭时生效：回填多少首历史无损。' +
          '数字越大挖得越深（300 首约多占 5-8 GB）', type: 'num', gatedBy: 'only_new_lossless' },
      { k: 'max_albums', t: '首次回填规模', d: '只在还没建立基线时用于控制首次批量下载的规模。' +
          '你的曲库已建立基线（839 首历史曲目已冻结），所以这个值当前不生效，日常也不用改。',
        type: 'readonly' },
      { k: 'sort_mode', t: '挑选顺序', d: '决定新收藏里优先抓哪些', type: 'select',
        options: [
          { v: 'bitrate', label: '优先高码率 —— 更容易拿到高采样率（推荐）' },
          { v: 'newest',  label: '优先最新收藏 —— 从歌单最新往下抓' },
        ]},
      { k: 'min_bitrate', t: '最低码率', d: '低于这个码率不下载', type: 'select',
        options: [
          { v: 0,    label: '不限（不按码率过滤）' },
          { v: 320,  label: '320 kbps 以上' },
          { v: 900,  label: '900 kbps 以上（约等于无损下限）' },
          { v: 1300, label: '1300 kbps 以上（偏 Hi-Res）' },
          { v: 2500, label: '2500 kbps 以上（高采样率 Hi-Res）' },
        ]},
    ]},
  { grp: 'guard', title: '拦截 · 哪些歌不进同步文件夹',
    note: '安全检查。这里拦下的歌不会下载、也不允许进入同步目录。',
    items: [
      { k: 'block_sample_rates', t: '拦截采样率', d: '可多选。176.4kHz 实测在本机手机链路上会输出噪声炸耳', type: 'multiselect',
        options: [
          { v: 176400, label: '176400 Hz（176.4kHz）—— 实测会炸耳' },
          { v: 192000, label: '192000 Hz（192kHz）—— 实测正常，不建议拦' },
          { v: 96000,  label: '96000 Hz（96kHz）—— 实测正常，不建议拦' },
        ]},
      { k: 'block_channels', t: '拦截声道数', d: '当前曲库全部是立体声，一般保持不拦', type: 'multiselect',
        options: [
          { v: 1, label: '单声道（1ch）' },
          { v: 6, label: '5.1 环绕（6ch）' },
          { v: 8, label: '7.1 环绕（8ch）' },
        ]},
      { k: 'content_mismatch_blocklist', t: '尊重内容错误清单', d: '保留「平台给错内容」的曲目不再重复下载', type: 'bool' },
    ]},
  { grp: 'sync', title: '同步 · 传到手机',
    note: 'Syncthing 的连接信息。一般不用改。',
    items: [
      { k: 'downloads_dir', t: '同步文件夹', d: '手动放歌也放这里，会被自动推给手机', type: 'text' },
      { k: 'folder_id', t: 'Syncthing 文件夹 ID', d: '', type: 'text' },
      { k: 'phone_device', t: '手机设备 ID', d: '', type: 'text' },
      { k: 'api_key', t: 'Syncthing API Key', d: '', type: 'text' },
      { k: 'syncthing_gui', t: 'Syncthing GUI 地址', d: '', type: 'text' },
    ]},
  { grp: 'docker', title: '引擎 · Downloader 与容器',
    note: '下载引擎运行在 Docker 里。',
    items: [
      { k: 'desktop_exe', t: 'Docker Desktop 路径', d: '', type: 'text' },
      { k: 'containers', t: '需要启动的容器', d: '逗号分隔', type: 'list' },
    ]},
];

async function loadConfig() {
  const r = await fetch('/api/config').then(r => r.json());
  CONFIG = r.config;
  $('#cfg-path').textContent = r.path;
  renderSettings();
}

function renderSettings() {
  if (!CONFIG) return;
  const box = $('#settings-form');
  box.innerHTML = '';
  FIELDS.forEach(g => {
    const sec = el('div', 'card grp');
    sec.append(el('h3', null, g.title));
    sec.append(el('span', 'muted', g.note));
    g.items.forEach(it => {
      const row = el('div', 'row');
      const lbl = el('div', 'lbl');
      lbl.append(el('b', null, it.t));
      if (it.d) lbl.append(el('em', null, it.d));
      row.append(lbl);

      const ctl = el('div', 'ctl');
      const val = CONFIG[g.grp] ? CONFIG[g.grp][it.k] : undefined;

      if (it.type === 'readonly') {
        // 只读说明项：显示当前值，明确标出「不生效」，避免用户白调
        const box = el('div', 'roval');
        box.append(el('b', null, String(val)));
        box.append(el('span', 'roval-tag', '当前不生效'));
        ctl.append(box);
      } else if (it.type === 'bool') {
        const sw = el('label', 'switch');
        const cb = el('input'); cb.type = 'checkbox'; cb.checked = !!val;
        cb.dataset.g = g.grp; cb.dataset.k = it.k; cb.dataset.t = 'bool';
        if (it.gates) cb.dataset.gates = it.gates;
        sw.append(cb); sw.append(el('span', 'slider'));
        if (it.gates) {
          // 这个开关会接管别的输入：一变就重算联动状态
          cb.addEventListener('change', applyGating);
        }
        ctl.append(sw);
        if (it.danger) sw.title = '谨慎开启';
      } else if (it.type === 'num') {
        const inp = el('input'); inp.type = 'number'; inp.value = val ?? 0;
        inp.dataset.g = g.grp; inp.dataset.k = it.k; inp.dataset.t = 'num';
        if (it.gatedBy) inp.dataset.gatedBy = it.gatedBy;
        ctl.append(inp);
      } else if (it.type === 'select') {
        const sel = el('select');
        sel.dataset.g = g.grp; sel.dataset.k = it.k; sel.dataset.t = 'select';
        it.options.forEach(o => {
          const op = el('option', null, o.label);
          op.value = String(o.v);
          if (String(val) === String(o.v)) op.selected = true;
          sel.append(op);
        });
        ctl.append(sel);
      } else if (it.type === 'multiselect') {
        // 复选框组：比手打逗号分隔的数字友好得多
        const wrap = el('div', 'msel');
        const cur = Array.isArray(val) ? val.map(String) : [];
        it.options.forEach(o => {
          const lab = el('label', 'msel-item');
          const cb = el('input'); cb.type = 'checkbox';
          cb.value = String(o.v);
          cb.checked = cur.includes(String(o.v));
          cb.dataset.g = g.grp; cb.dataset.k = it.k; cb.dataset.t = 'multiselect';
          lab.append(cb);
          lab.append(el('span', null, o.label));
          wrap.append(lab);
        });
        ctl.append(wrap);
        ctl.style.flexDirection = 'column';
        ctl.style.alignItems = 'flex-start';
      } else if (it.type === 'list') {
        const inp = el('input'); inp.type = 'text';
        inp.value = Array.isArray(val) ? val.join(', ') : (val ?? '');
        inp.dataset.g = g.grp; inp.dataset.k = it.k; inp.dataset.t = 'list';
        ctl.append(inp);
      } else {
        const inp = el('input'); inp.type = 'text'; inp.value = val ?? '';
        inp.dataset.g = g.grp; inp.dataset.k = it.k; inp.dataset.t = 'text';
        ctl.append(inp);
      }
      row.append(ctl);
      sec.append(row);
    });
    box.append(sec);
  });
  applyGating();
}

/* 联动：某个开关打开时，把受它管辖的输入框禁用掉。
   例：无损只跟新歌 = 打开时，「无损目标数量」被完全忽略，
   所以这里不仅禁用输入，还会加一行说明，避免用户以为改了有用。 */
function applyGating() {
  document.querySelectorAll('#settings-form [data-gated-by]').forEach(inp => {
    const gateKey = inp.dataset.gatedBy;
    const gate = document.querySelector(
      '#settings-form input[data-k="' + gateKey + '"][data-t="bool"]');
    if (!gate) return;
    const off = gate.checked;                 // 开关打开 → 目标值不生效
    inp.disabled = off;
    const row = inp.closest('.row');
    let hint = row.querySelector('.gate-hint');
    if (off) {
      if (!hint) {
        hint = el('span', 'gate-hint', '已被上方开关接管，此处改动不生效');
        row.querySelector('.ctl').append(hint);
      }
      row.classList.add('gated');
    } else {
      if (hint) hint.remove();
      row.classList.remove('gated');
    }
  });
}

function collectConfig() {
  const cfg = JSON.parse(JSON.stringify(CONFIG));
  // multiselect 同一个 key 有多个复选框，需要先清空再累加
  const multi = {};
  document.querySelectorAll('#settings-form [data-k]').forEach(inp => {
    const g = inp.dataset.g, k = inp.dataset.k, t = inp.dataset.t;
    if (!cfg[g]) cfg[g] = {};
    if (t === 'bool') cfg[g][k] = inp.checked;
    else if (t === 'num') cfg[g][k] = Number(inp.value) || 0;
    else if (t === 'multiselect') {
      const id = g + '.' + k;
      if (!multi[id]) { multi[id] = []; cfg[g][k] = []; }
      if (inp.checked) {
        const v = /^\d+$/.test(inp.value) ? Number(inp.value) : inp.value;
        cfg[g][k].push(v);
      }
    }
    else if (t === 'select') {
      const raw = inp.value;
      // 数字型选项（如 min_bitrate）还原成数字
      cfg[g][k] = /^-?\d+$/.test(raw) ? Number(raw) : raw;
    }
    else if (t === 'list') {
      cfg[g][k] = inp.value.split(',').map(s => s.trim()).filter(Boolean)
        .map(s => /^\d+$/.test(s) ? Number(s) : s);
    } else cfg[g][k] = inp.value;
  });
  return cfg;
}

$('#btn-save').onclick = async () => {
  const cfg = collectConfig();
  const r = await fetch('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config: cfg })
  }).then(r => r.json());
  if (r.ok) {
    CONFIG = r.config;
    $('#save-msg').textContent = '已保存 ✓';
    setTimeout(() => $('#save-msg').textContent = '', 2500);
    renderSettings();
  } else {
    $('#save-msg').textContent = '保存失败：' + (r.error || '');
    $('#save-msg').style.color = 'var(--err)';
  }
};
$('#btn-reload').onclick = loadConfig;

/* ══════════ 运行逻辑页（内容存后端，不存浏览器） ══════════ */
const LOGIC_SECTIONS = [
  { id: 'overview', title: '这个应用在做什么' },
  { id: 'projects', title: '依赖的开源项目 / 组件' },
  { id: 'pipeline', title: '流水线每一步的原理' },
  { id: 'safety',   title: '安全机制（防止炸耳 / 数据损坏）' },
  { id: 'trouble',  title: '常见故障与排查' },
];

async function loadLogic() {
  const box = $('#logic-body');
  box.innerHTML = '<div class="muted">加载中…</div>';
  let data;
  try {
    data = await fetch('/api/logic').then(r => r.json());
  } catch (e) {
    box.innerHTML = '<div class="muted">读取失败</div>';
    return;
  }
  box.innerHTML = '';
  LOGIC_SECTIONS.forEach(s => {
    const sec = el('div', 'card logic-sec');
    const head = el('div', 'card-head');
    head.append(el('h3', null, s.title));
    head.append(el('span', 'muted', '可编辑'));
    sec.append(head);
    const ta = el('textarea');
    ta.value = (data.sections && data.sections[s.id]) || '';
    ta.dataset.id = s.id;
    sec.append(ta);
    box.append(sec);
  });
  $('#logic-msg').textContent = data.path ? ('保存在 ' + data.path) : '';
}

$('#btn-save-logic').onclick = async () => {
  const sections = {};
  document.querySelectorAll('#logic-body textarea').forEach(t => {
    sections[t.dataset.id] = t.value;
  });
  const r = await fetch('/api/logic', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sections })
  }).then(r => r.json());
  $('#logic-msg').textContent = r.ok ? '已保存 ✓' : '保存失败';
  if (r.ok) setTimeout(loadLogic, 1500);
};

/* ══════════ 曲库统计 ══════════ */
function distRow(label, count, total) {
  const pct = total ? Math.round(count / total * 100) : 0;
  const row = el('div', 'dist-row');
  row.append(el('span', 'dist-lbl', label));
  const track = el('div', 'dist-track');
  const fill = el('div', 'dist-fill');
  fill.style.width = Math.max(pct, count ? 2 : 0) + '%';
  track.append(fill);
  row.append(track);
  row.append(el('span', 'dist-num', count + ' 首 · ' + pct + '%'));
  return row;
}

function rateLabel(r) {
  const n = Number(r);
  if (!n) return String(r);
  const khz = n / 1000;
  const names = {
    44100: 'CD 标准', 48000: '常见数字音频', 88200: 'CD 两倍',
    96000: 'Hi-Res 入门', 176400: '⚠ 实测会炸耳', 192000: 'Hi-Res 高阶',
  };
  return khz + ' kHz' + (names[n] ? '  ·  ' + names[n] : '');
}

async function loadLibrary(force) {
  const grid = $('#lib-grid');
  const sum = $('#lib-summary');
  sum.innerHTML = '';
  grid.innerHTML = '<div class="muted">' +
    (force ? '正在实测每个文件的规格，约需 90 秒…' : '读取中…') + '</div>';

  let d;
  try {
    d = await fetch('/api/library_stats' + (force ? '?force=1' : ''))
      .then(r => r.json());
  } catch (e) {
    grid.innerHTML = '<div class="muted">读取失败</div>';
    return;
  }
  if (!d.ok) {
    grid.innerHTML = '<div class="muted">' + (d.error || '读取失败') + '</div>';
    return;
  }

  sum.innerHTML = '';
  [['曲目总数', d.count + ' 首'],
   ['总容量', d.total_gb + ' GB'],
   ['真 Hi-Res', d.true_hires + ' 首'],
   ['读取异常', d.unreadable + ' 首']].forEach(function (kv) {
    const c = el('div', 'sum-card');
    c.append(el('em', null, kv[0]));
    c.append(el('b', null, kv[1]));
    sum.append(c);
  });

  grid.innerHTML = '';
  const sections = [
    ['采样率分布', d.by_rate, rateLabel],
    ['位深分布', d.by_bits, function (b) { return b === '未知' ? b : b + ' bit'; }],
    ['声道分布', d.by_ch, function (c) {
      return { '1': '单声道', '2': '立体声' }[c] || c + ' 声道';
    }],
    ['容器格式', d.by_codec, function (c) { return String(c).toUpperCase(); }],
  ];
  sections.forEach(function (sec) {
    const card = el('div', 'card');
    const head = el('div', 'card-head');
    head.append(el('h3', null, sec[0]));
    card.append(head);
    const entries = Object.entries(sec[1] || {});
    if (!entries.length) {
      card.append(el('div', 'muted', '无数据'));
    } else {
      entries.forEach(function (kv) {
        card.append(distRow(sec[2](kv[0]), kv[1], d.count));
      });
    }
    grid.append(card);
  });

  $('#lib-msg').textContent = d.cached
    ? '来自缓存（磁盘无变化）'
    : ('扫描完成 · ' + new Date().toLocaleTimeString());
}

$('#btn-scan-lib').onclick = function () { loadLibrary(true); };


/* ══════════ 拦截清单 ══════════ */
async function loadBlocked() {
  const box = $('#blocked-body');
  box.innerHTML = '<div class="muted">加载中…</div>';
  const r = await fetch('/api/blocked').then(r => r.json());
  box.innerHTML = '';
  if (!r.ok) {
    box.append(el('div', 'muted', '读取失败：' + (r.error || '') + '（Docker 可能没启动）'));
    return;
  }
  if (!r.items.length) {
    box.append(el('div', 'muted', '清单为空'));
    return;
  }
  r.items.forEach(name => {
    const it = el('div', 'bl-item');
    // 粗略分类：含「炸耳」相关关键词或已知采样率问题的算 guard
    const isGuard = /Moth To A Flame|boy solo|Madeon|Marshmello、YUNGBLUD|NEFFEX - NO TURNING|АДЛИН|ヨルシカ - 老人と海|Livingston - Glow/.test(name);
    const tag = el('span', 'tag ' + (isGuard ? 'guard' : 'no-src'),
                   isGuard ? '安全检查' : '无音源');
    it.append(tag);
    it.append(el('span', 'nm', name));
    box.append(it);
  });
  box.append(el('div', 'muted', `共 ${r.count} 条`));
}
$('#btn-reload-blocked').onclick = loadBlocked;

/* ══════════ 运行日志 ══════════ */
async function loadRuns() {
  const r = await fetch('/api/runs').then(r => r.json());
  const box = $('#runs-list');
  box.innerHTML = '';
  if (!r.runs.length) {
    box.append(el('div', 'muted', '还没有运行记录'));
    return;
  }
  r.runs.forEach(run => {
    const it = el('button', 'run-item');
    it.append(el('div', 'rn', run.name));
    it.append(el('div', 'rm', `${run.mtime} · ${(run.size / 1024).toFixed(1)} KB`));
    it.onclick = async () => {
      document.querySelectorAll('.run-item').forEach(x => x.classList.remove('active'));
      it.classList.add('active');
      const d = await fetch('/api/run?name=' + encodeURIComponent(run.name))
        .then(r => r.json());
      $('#run-title').textContent = run.name;
      $('#run-content').textContent = d.content || d.error || '';
      $('#run-content').scrollTop = 0;
    };
    box.append(it);
  });
}

/* ══════════ 启动 ══════════ */
(async function init() {
  const saved = localStorage.getItem('mf_mode') || 'auto';
  setMode(saved);
  await loadPipeline();
  await loadConfig();
  connectSSE();
  pollStatus();
  setInterval(pollStatus, 5000);
  appendLog({ ts: '', level: 'ok', message: 'Music Flow 就绪。连接热点后点「同步」即可。' });
})();
