'use strict';
const $ = id => document.getElementById(id);
const form = $('settings-form');
// Remove the one-use code before navigation, requests or user interaction.
const connectCode = new URLSearchParams(location.hash.slice(1)).get('connect');
if (connectCode) history.replaceState(null, '', location.pathname + location.search);
let mode = 'server', localDirty = false;
function setMode(value) {
  mode = value === 'desktop' ? 'desktop' : 'server';
  const desktop = mode === 'desktop';
  document.body.dataset.mode = mode;
  $('mode-label').textContent = desktop ? '电脑按需导出' : '后台持续归档';
  $('tasks-label').textContent = desktop ? '导出记录' : '归档任务';
  $('run-label').textContent = desktop ? '开始导出' : '运行一次';
  $('stop').textContent = desktop ? '停止本次导出' : '停止并关闭自动归档';
  $('metric-success-label').textContent = desktop ? '最近成功导出' : '最近成功归档';
  $('stop-now').textContent = desktop ? '停止本次导出' : '停止并关闭自动归档';
  $('status-title').textContent = desktop ? '导出状态' : '归档状态';
  $('callback-label').textContent = desktop ? '这台电脑的局域网 IP' : 'NAS 回调 IP';
  $('login-title').textContent = desktop ? '把原片，留在电脑里。' : '归档状态，一目了然。';
  $('login-description').textContent = desktop ? '双击启动文件可自动连接；也可从启动窗口提示的文件中获取管理密钥。' : '连接管理服务，查看任务进度、设备配置与存储状态。';
  if (desktop) {
    pages.overview = ['导出录像', '准备好设备和保存位置，按需取回原始录像。', '电脑版'];
    pages.settings = ['设备与存储', '登录账号、选择保存位置，设置本轮导出范围。', '管理'];
    pages.tasks = ['导出记录', '看看哪些任务已经完成，哪些还需要处理。', '电脑版'];
  }
}
const pages = {
  overview: ['概览', '快速了解设备配置、归档进度与需要处理的问题。', '工作台'],
  tasks: ['归档任务', '每一轮归档，都有可以回看的结果。', '工作台'],
  settings: ['设备与存储', '管理连接、归档范围和运行方式。', '管理'],
  diagnostics: ['诊断', '查看本地准备状态，区分已知结果与待验证项。', '管理'],
  access: ['访问设置', '管理当前页面连接与外观偏好。', '管理']
};
const intervals = {'1h': '1 小时', '1d': '1 天', '2d': '2 天', '3d': '3 天'};
const states = {running: ['运行中', 'positive'], success: ['成功', 'positive'], failed: ['失败', 'negative'], stopped: ['已停止', ''], interrupted: ['结果未确认', 'warning'], paused: ['空间不足暂停', 'warning']};
let token = '', timer, busy = false, polling = false, currentPage = 'overview', lastData = null, dirty = false;
function theme(mode) {
  document.documentElement.dataset.theme = mode;
  $('theme-label').textContent = mode === 'dark' ? '深色模式' : '浅色模式';
  $('theme-toggle').setAttribute('aria-pressed', String(mode === 'dark'));
  $('theme-toggle').setAttribute('aria-label', mode === 'dark' ? '切换到浅色模式' : '切换到深色模式');
}
try { theme(localStorage.getItem('pawzz-theme') === 'dark' ? 'dark' : 'light'); } catch (_) { theme('light'); }
$('theme-toggle').addEventListener('click', () => {
  const mode = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  theme(mode); try { localStorage.setItem('pawzz-theme', mode); } catch (_) { /* Preference storage is optional. */ }
});
function message(text = '', error = false) {
  $('message').textContent = text; $('message').hidden = !text;
  $('message').classList.toggle('error', error);
}
function route(page, focus = false) {
  currentPage = pages[page] ? page : 'overview';
  document.querySelectorAll('[data-view]').forEach(view => { view.hidden = view.dataset.view !== currentPage; });
  document.querySelectorAll('.nav-item').forEach(button => {
    const selected = button.dataset.page === currentPage;
    button.classList.toggle('active', selected);
    if (selected) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  const [title, description, section] = pages[currentPage];
  $('page-title').textContent = title; $('page-description').textContent = description;
  $('page-eyebrow').textContent = section; $('breadcrumb').textContent = section + ' / ' + title;
  if (focus) $('page-title').focus({preventScroll: true});
}
document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => route(button.dataset.page, true)));
document.querySelector('.brand').addEventListener('click', event => { event.preventDefault(); if (token) route('overview', true); });
async function api(path, body) {
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), path.startsWith('account/') ? 30000 : 10000);
  try {
    const response = await fetch('/api/' + path, {signal: controller.signal,
      method: body === undefined ? 'GET' : 'POST',
      headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok) { const error = new Error(data.error || '请求失败'); error.status = response.status; throw error; }
    return data;
  } finally { clearTimeout(timeout); }
}
function element(tag, text, className = '') { const el = document.createElement(tag); el.textContent = text; if (className) el.className = className; return el; }
function badge(id, text, style = '') { $(id).textContent = text; $(id).className = 'badge ' + style; }
function stamp(value, compact = false) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  const date = new Date(value * 1000);
  return date.toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false, ...(compact ? {} : {year:'numeric'})});
}
function bytes(value) {
  if (typeof value !== 'number') return '—';
  const gb = value / (1024 ** 3); return gb >= 1024 ? (gb / 1024).toFixed(1) + ' TB' : gb.toFixed(1) + ' GB';
}
function duration(record) {
  if (!record.finished_at) return record.state === 'running' ? '正在执行' : '结束时间未确认';
  const seconds = Math.max(0, Math.round(record.finished_at - record.started_at));
  return seconds >= 60 ? Math.floor(seconds / 60) + ' 分 ' + seconds % 60 + ' 秒' : seconds + ' 秒';
}
function populateHistory(records, target, detailed) {
  const rows = records.map(record => {
    const row = document.createElement('tr');
    const time = element('td', stamp(record.started_at, !detailed));
    if (detailed) time.append(element('small', duration(record), 'table-note'));
    row.append(time);
    const scope = element('td', record.scope || '—');
    if (detailed) scope.append(element('small', record.trigger === 'retry' ? '失败重试 ' + record.retry_attempt + '/3' : record.trigger === 'automatic' ? '自动归档' : '手动执行', 'table-note'));
    row.append(scope, element('td', typeof record.new_files === 'number' ? String(record.new_files) : '—'));
    const cell = document.createElement('td'); const [label, style] = states[record.state] || ['未知', ''];
    cell.append(element('span', label, 'badge ' + style)); row.append(cell);
    if (detailed) { const result = element('td', record.result || '—');
      if (record.exit_code !== null && record.exit_code !== undefined) result.append(element('small', '退出码 ' + record.exit_code, 'table-note'));
      row.append(result);
    }
    return row;
  });
  $(target).replaceChildren(...rows);
}
function diagnostic(title, detail, status, style = '') {
  const item = document.createElement('li'); const text = element('div', title);
  text.append(element('small', detail)); item.append(text, element('span', status, 'badge ' + style)); return item;
}
function updateControls() {
  if (!lastData) return;
  const data = lastData;
  $('run').disabled = busy || dirty || localDirty || data.running || !data.configured || !data.session_ready || !data.settings.allow_maintenance || !data.history_valid || !data.storage.available || !data.storage.writable || data.storage.free < data.policy.min_free_bytes;
  $('run').title = dirty || localDirty ? '请先保存设置后再运行' : '';
  $('stop-now').hidden = !data.running; $('stop-now').disabled = busy;
  $('stop').disabled = busy || (!data.running && !data.settings.automatic);
  for (const control of form.elements) control.disabled = busy || data.running;
  $('refresh').disabled = busy;
  for (const id of ['session-file', 'local-output-input', 'save-output']) $(id).disabled = busy || data.running;
  const account = data.account;
  const blocked = busy || data.running || !account.login_allowed || data.session_ready || account.logout_pending;
  for (const control of $('account-form').elements) control.disabled = blocked;
  $('send-code').disabled = blocked || account.sms_wait > 0;
  $('send-code').textContent = account.sms_wait > 0 ? account.sms_wait + ' 秒后可重发' : '发送验证码';
  $('account-submit').disabled = blocked || !account.code_pending;
  $('account-logout').disabled = busy || account.logout_pending;
  $('session-file').disabled = blocked || account.saved;
  $('save-hint').textContent = dirty ? '有未保存的修改，保存后才会生效。' : data.running ? '任务运行中；停止或完成后可修改。' : '修改后保存，才会对任务生效。';
}
function paint(data, fields = false) {
  const wasRunning = lastData && lastData.running;
  const previousAccount = lastData && lastData.account;
  lastData = data; setMode(data.mode);
  const account = data.account;
  const accountLabels = {temporary: '临时登录', saved: '已加密保存', external: '外部会话文件'};
  const accountLabel = account.logout_pending ? '正在停止并退出' : accountLabels[account.source] || (account.saved ? '保存文件需处理' : '未登录');
  badge('account-badge', accountLabel, data.session_ready ? 'positive' : '');
  $('account-transport').hidden = account.login_allowed;
  $('account-form').hidden = data.session_ready || account.saved || account.logout_pending;
  $('account-signed-in').hidden = !data.session_ready && !account.saved && !account.logout_pending && !account.source;
  $('account-detail').textContent = account.logout_pending ? '正在等待任务停止，完成后清除进程中的登录凭据。' : account.source === 'saved' ? 'NAS 已加密保存 user_id 和 token，解密密钥位于独立目录；手机号和验证码未写入文件。文件与密钥不可一起分享。' : account.source === 'external' ? '正在使用你提供的明文会话文件。退出会停用该文件，但不会删除宿主机原件。' : account.source === 'temporary' ? '登录凭据仅在当前服务内存中使用，程序未将它们保存到文件。' : '保存的登录状态无法使用，请退出后重新登录。';
  if (previousAccount && previousAccount.code_pending && !account.code_pending) $('account-form').reset();
  if (wasRunning && !data.running) message(data.last_result + (previousAccount.source === 'temporary' && !account.source ? '。临时登录已清除，下次导出请重新登录。' : ''), data.last_exit !== 0);
  const settings = data.settings, history = data.history || [], latest = history[0], storage = data.storage;
  $('connection').textContent = '管理服务已连接'; $('service-dot').classList.add('online');
  const lastSuccess = history.find(row => row.state === 'success');
  $('metric-success').textContent = lastSuccess ? stamp(lastSuccess.finished_at, true) : '暂无记录';
  $('metric-success-detail').textContent = lastSuccess ? '最近 100 轮任务内' : '完成任务后显示';
  $('metric-count-label').textContent = data.running ? '本轮已校验新增' : '最近一轮新增';
  $('metric-count').textContent = latest && typeof latest.new_files === 'number' ? latest.new_files + ' 段' : '—';
  $('metric-count-detail').textContent = latest ? (states[latest.state] || ['未知'])[0] : '尚未运行';
  $('metric-space').textContent = bytes(storage.free);
  let title, label, style = '';
  if (data.running) { title = mode === 'desktop' ? '正在导出原始录像' : '正在归档原始录像'; label = '运行中'; style = 'positive'; }
  else if (data.next_kind === 'storage' || storage.available && storage.free < data.policy.min_free_bytes || latest && latest.state === 'paused') { title = '存储空间不足，归档已暂停'; label = '已暂停'; style = 'warning'; }
  else if (!data.configured || !data.session_ready || !settings.allow_maintenance) { title = '完成准备，即可开始导出'; label = '等待设置'; }
  else if (data.problem || latest && ['failed', 'interrupted'].includes(latest.state)) { title = '有一项结果需要关注'; label = '需要检查'; style = 'warning'; }
  else { title = settings.automatic ? '自动归档已就绪' : '已就绪，等待手动运行'; label = '已就绪'; style = 'positive'; }
  $('state').textContent = title; badge('state-badge', label, style);
  $('result').textContent = data.running ? '正在读取并校验原片，完成后会保存本轮结果。' : data.last_result;
  $('timing').textContent = data.last_finished ? '上轮结束于 ' + stamp(data.last_finished) : '尚无已完成的任务';
  $('run-hint').textContent = dirty ? '有未保存的设置，请先保存。' : !data.configured ? '先填写设备与回调地址。' : !data.session_ready ? '先在设备与存储页登录账号。' : !settings.allow_maintenance ? '运行前需明确允许维护读取。' : '只处理当前已保存的导出范围。';
  badge('automatic-badge', settings.automatic ? '已开启' : '未开启', settings.automatic ? 'positive' : '');
  $('schedule-interval').textContent = '每轮完成后 ' + intervals[settings.interval];
  $('schedule-next').textContent = data.running ? '本轮结束后计算' : data.next_run ? (data.next_kind === 'retry' ? '重试 ' + data.retry_count + '/3 · ' : data.next_kind === 'storage' ? '检查空间 · ' : '') + stamp(data.next_run, true) : '手动执行';
  $('schedule-scope').textContent = settings.source ? '指定原片' : settings.start_date ? '自 ' + settings.start_date : '全部日期';
  badge('device-check', data.configured ? '已配置' : '待设置', data.configured ? 'positive' : '');
  $('device-subtitle').textContent = settings.device_ip || '尚未填写设备地址';
  badge('session-check', data.session_ready ? '已准备' : '待登录', data.session_ready ? 'positive' : 'warning');
  $('session').textContent = data.session_ready ? accountLabel + '，实际有效性待导出验证' : '尚未准备可用登录状态';
  $('output').textContent = data.output;
  $('local-output-summary').textContent = data.output;
  $('local-scope').textContent = settings.source ? '指定原片' : settings.start_date ? '自 ' + settings.start_date : '全部日期';
  if (!localDirty) $('local-output-input').value = data.output;
  $('storage-state').textContent = !storage.available ? '目录不可访问' : storage.writable ? '目录可访问，写权限初检通过' : '目录可访问，写权限初检未通过';
  $('storage-capacity').textContent = bytes(storage.free) + ' / ' + bytes(storage.total);
  const issues = [];
  if (!data.configured) issues.push('设备连接尚未配置完整。');
  if (!data.session_ready) issues.push('尚未准备登录会话，暂不能执行导出。');
  if (data.configured && !settings.allow_maintenance) issues.push('维护读取许可尚未开启。');
  if (!storage.available || !storage.writable) issues.push('输出目录未就绪，请检查保存位置与权限。');
  if (storage.available && storage.free < data.policy.min_free_bytes) issues.push('可用空间不足 5 GB，暂停归档；不会自动删除录像。');
  if (data.problem) issues.push(data.problem);
  if (latest && ['failed', 'interrupted'].includes(latest.state)) issues.push(latest.result);
  $('attention-list').replaceChildren(...issues.map(text => element('li', text)));
  $('attention-panel').hidden = !issues.length;
  $('recent-empty').hidden = $('history-empty').hidden = history.length > 0;
  $('recent-table-wrap').hidden = $('history-table-wrap').hidden = !history.length;
  populateHistory(history.slice(0, 5), 'recent-rows', false); populateHistory(history, 'history-rows', true);
  $('diagnostic-list').replaceChildren(
    diagnostic('管理服务', '网页可以读取当前服务的状态', '已连接', 'positive'),
    diagnostic('设备配置', '仅验证字段格式，不代表设备在线', data.configured ? '已配置' : '待设置', data.configured ? 'positive' : 'warning'),
    diagnostic('账号登录状态', '仅显示是否已准备，不向页面返回凭据', data.session_ready ? '已准备' : '待登录', data.session_ready ? 'positive' : 'warning'),
    diagnostic('输出目录', '目录可访问性与写权限初检；未进行真实写入验收', storage.available && storage.writable ? '初检通过' : '需要检查', storage.available && storage.writable ? 'positive' : 'warning'),
    diagnostic('任务历史', data.history_valid ? '保存最近 100 轮记录' : '保留原文件，修复后重启服务', data.history_valid ? '可读取' : '读取失败', data.history_valid ? 'positive' : 'warning'),
    diagnostic('设备在线与回调', '尚未对真实设备执行连通性验证', '未检测')
  );
  $('access-address').textContent = location.origin;
  $('access-transport').textContent = location.protocol === 'https:' ? 'HTTPS 加密连接' : 'HTTP · 仅限可信网络';
  if (fields) {
    for (const [name, value] of Object.entries(settings)) {
      const field = form.elements.namedItem(name);
      if (field.type === 'checkbox') field.checked = value; else field.value = value;
    }
    dirty = false;
  }
  $('updated-at').textContent = '状态更新于 ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
  updateControls();
}
function disconnect() {
  token = ''; clearInterval(timer); lastData = null; dirty = false; localDirty = false;
  $('key').value = ''; $('account-form').reset(); $('local-output-form').reset(); $('session-file').value = ''; form.reset(); $('console').hidden = true; $('navigation').hidden = true; $('login').hidden = false;
  document.body.classList.add('signed-out'); $('connection').textContent = '未连接管理服务'; $('service-dot').classList.remove('online');
  $('breadcrumb').textContent = '本地管理入口'; $('updated-at').textContent = '数据仅在认证后加载';
}
function failure(error) {
  if (error.status === 401) disconnect();
  else if (!error.status) { $('connection').textContent = '状态更新失败'; $('service-dot').classList.remove('online'); }
  message(error.name === 'AbortError' ? '请求超时，请检查管理服务连接。' : error.message || '连接失败，请稍后重试。', true);
}
async function refresh(manual = false) {
  if (!token || polling || busy) return;
  polling = true; const session = token;
  try { const data = await api('status'); if (session === token && token) { paint(data); if (manual) message('本地状态已更新。'); } }
  catch (error) { if (session === token && token) failure(error); }
  finally { polling = false; }
}
function connected(data) {
  paint(data, true); $('key').value = ''; $('console').hidden = false; $('navigation').hidden = false; $('login').hidden = true;
  document.body.classList.remove('signed-out'); route(data.session_ready ? 'overview' : 'settings'); message(); clearInterval(timer);
  timer = setInterval(() => refresh(), 5000);
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); token = $('key').value.trim();
  const session = token;
  try { const data = await api('status'); if (session === token) connected(data); }
  catch (error) { disconnect(); failure(error); }
});
async function initialize() {
  try {
    const info = await api('mode'); setMode(info.mode);
    if (connectCode && mode === 'desktop') {
      const login = await api('connect', {code: connectCode}); token = login.token;
      connected(await api('status'));
    }
  } catch (error) { disconnect(); message('自动连接未完成，请使用管理密钥连接。', true); }
}
initialize();
$('local-output-input').addEventListener('input', () => { localDirty = true; updateControls(); });
async function localSetup(body, notice) {
  if (busy) return;
  busy = true; updateControls(); const session = token;
  try {
    const data = await api('local', body);
    if (session === token && token) {
      if ('output_dir' in body) localDirty = false;
      paint(data); message(notice);
    }
  } catch (error) { if (session === token && token) failure(error); }
  finally { busy = false; updateControls(); }
}
$('local-output-form').addEventListener('submit', event => {
  event.preventDefault(); localSetup({output_dir: $('local-output-input').value.trim()}, '保存位置已更新，已有录像保持原位。');
});
$('session-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  try {
    if (!file) return;
    if (file.size > 8192) throw new Error('会话文件过大，请选择只包含 user_id 和 token 的 JSON 文件。');
    if (lastData.session_ready) throw new Error('请先退出当前账号。');
    const session = JSON.parse(await file.text());
    await localSetup({session}, '会话已临时载入内存，原文件未修改；退出账号或关闭服务时清除临时凭据。');
  } catch (error) { message(error instanceof SyntaxError ? '这不是有效的 JSON 会话文件。' : error.message, true); }
  finally { event.target.value = ''; }
});
form.addEventListener('input', () => { dirty = true; updateControls(); });
form.addEventListener('change', () => { dirty = true; updateControls(); });
form.addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  const settings = {};
  for (const field of form.elements) if (field.name) settings[field.name] = field.type === 'checkbox' ? field.checked : field.type === 'number' ? Number(field.value) : field.value.trim();
  busy = true; updateControls(); const session = token;
  try { const data = await api('settings', settings); if (session === token && token) { paint(data, true); message('设置已保存。'); } }
  catch (error) { if (session === token && token) failure(error); }
  finally { busy = false; updateControls(); }
});
for (const [id, action] of [['run','run'], ['stop','stop'], ['stop-now','stop']]) $(id).addEventListener('click', async () => {
  if (busy) return; busy = true; updateControls(); const session = token;
  try { const data = await api(action, {}); if (session === token && token) {
    if (action === 'stop') form.elements.namedItem('automatic').checked = false;
    paint(data); message(action === 'run' ? '已开始本轮导出。' : mode === 'desktop' ? '已请求停止本次导出。' : '已请求停止，并关闭自动归档。');
  } } catch (error) { if (session === token && token) failure(error); }
  finally { busy = false; updateControls(); }
});
$('refresh').addEventListener('click', () => refresh(true));
$('logout').addEventListener('click', () => { disconnect(); message('已断开管理页面。任务继续按已保存的设置执行。'); });

async function accountAction(action, body) {
  if (busy) return;
  busy = true; updateControls(); const current = token;
  try {
    const data = await api('account/' + action, body);
    if (current === token && token) {
      paint(data);
      const notices = {send: '已请求发送短信。收到后填写验证码，不会自动重发。', login: '登录成功。确认设备设置与维护许可后，再开始导出。', logout: data.account.logout_pending ? '已请求停止任务，停止后清除登录凭据。' : '已退出账号。本程序保存的凭据已移除；外部原件与备份请自行管理。', cancel: '已取消本次验证码验证并清空输入。'};
      message(notices[action]);
      if (action !== 'send') $('account-form').reset();
      if (action === 'logout') form.elements.namedItem('automatic').checked = false;
    }
  } catch (error) { if (current === token && token) failure(error); }
  finally { body = null; busy = false; updateControls(); refresh(); }
}
$('send-code').addEventListener('click', () => {
  if (!$('account-phone').reportValidity()) return;
  accountAction('send', {phone: $('account-phone').value.trim()});
});
$('account-form').addEventListener('submit', event => {
  event.preventDefault();
  const body = {phone: $('account-phone').value.trim(), code: $('account-code').value, remember: mode === 'server' && $('remember-account').checked};
  $('account-code').value = '';
  accountAction('login', body);
});
$('account-cancel').addEventListener('click', () => { $('account-form').reset(); accountAction('cancel', {}); });
$('account-logout').addEventListener('click', () => accountAction('logout', {}));
window.addEventListener('pagehide', () => $('account-form').reset());
