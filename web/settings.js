'use strict';
const $ = id => document.getElementById(id);
const form = $('device-form');
const archiveForm = $('archive-form');
const settingForms = {device:form, archive:archiveForm};
const groupFields = {device:['device_ip','device_mac','callback_ip','callback_port','serial','allow_maintenance'], archive:['start_date','source','interval','repair_video']};
const dirtyGroups = new Set();
let activeSettingsSection = 'account';
// Remove the one-use code before navigation, requests or user interaction.
const connectCode = new URLSearchParams(location.hash.slice(1)).get('connect');
if (connectCode) history.replaceState(null, '', location.pathname + location.search);
let mode = 'server', localDirty = false;
function setMode(value) {
  const previousMode = mode;
  mode = value === 'desktop' ? 'desktop' : 'server';
  const desktop = mode === 'desktop';
  document.body.dataset.mode = mode;
  $('mode-label').textContent = desktop ? '电脑按需导出' : '后台持续归档';
  $('tasks-label').textContent = desktop ? '导出记录' : '归档任务';
  $('run-label').textContent = desktop ? '开始导出' : '只运行一次';
  $('run').classList.toggle('secondary', !desktop);
  $('run').setAttribute('role', desktop ? 'button' : 'menuitem');
  $('run-options-menu').setAttribute('role', desktop ? 'group' : 'menu');
  if (desktop) $('run-options-menu').hidden = false;
  else if (previousMode === 'desktop') setRunMenu(false);
  $('stop').textContent = desktop ? '停止本次导出' : '停止当前任务';
  $('metric-success-label').textContent = desktop ? '最近成功导出' : '最近成功归档';
  $('stop-now').textContent = desktop ? '停止本次导出' : '停止当前任务';
  $('status-title').textContent = desktop ? '导出状态' : '归档状态';
  $('callback-label').textContent = desktop ? '这台电脑的局域网 IP' : 'NAS 回调 IP';
  $('callback-help').textContent = desktop ? '保存录像的这台电脑的局域网 IP，不填网页的 127.0.0.1。' : 'NAS 储存的 IP，即保存录像的 NAS 局域网地址，不填 DDNSTO 域名。';
  $('setup-guide').querySelector('p').textContent = desktop ? '登录账号 → 配置设备与保存位置 → 手动开始导出' : '登录账号 → 配置设备与存储 → 保存归档设置 → 开启自动归档';
  $('login-title').textContent = desktop ? '把原片，留在电脑里。' : '归档状态，一目了然。';
  $('login-description').textContent = desktop ? '双击启动文件可自动连接；也可从启动窗口提示的文件中获取管理密码。' : '连接管理服务，查看任务进度、设备配置与存储状态。';
  if (desktop) {
    pages.overview = ['导出录像', '准备好设备和保存位置，按需取回原始录像。', '电脑版'];
    pages.settings = ['设置', '账号、设备与存储、归档设置分别保存。', '管理'];
    pages.tasks = ['导出记录', '看看哪些任务已经完成，哪些还需要处理。', '电脑版'];
  }
}
const pages = {
  overview: ['概览', '快速了解设备配置、归档进度与需要处理的问题。', '工作台'],
  tasks: ['归档任务', '每一轮归档，都有可以回看的结果。', '工作台'],
  settings: ['设置', '账号、设备与存储、归档设置分别保存。', '管理'],
  diagnostics: ['诊断', '查看本地准备状态，区分已知结果与待验证项。', '管理'],
  access: ['访问设置', '管理当前页面连接与外观偏好。', '管理']
};
const intervals = {'1h': '1 小时', '3h': '3 小时', '6h': '6 小时', '12h': '12 小时', '1d': '1 天', '2d': '2 天', '3d': '3 天'};
const states = {running: ['运行中', 'positive'], success: ['成功', 'positive'], failed: ['失败', 'negative'], stopped: ['已停止', ''], interrupted: ['结果未确认', 'warning'], paused: ['空间不足暂停', 'warning']};
let token = '', timer, busy = false, polling = false, currentPage = 'overview', lastData = null, dirty = false;
let logsInitialized = false, logsBusy = false, logRequest = 0;
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
  setRunMenu(false);
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
  if (currentPage === 'diagnostics' && token && !logsInitialized) initializeLogs();
}
document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => { route(button.dataset.page, true); if (button.dataset.settingsSection) switchSettings(button.dataset.settingsSection); }));
document.querySelector('.brand').addEventListener('click', event => { event.preventDefault(); if (token) route('overview', true); });
async function api(path, body) {
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), path.startsWith('account/') ? 30000 : 10000);
  try {
    const response = await fetch('/api/' + path, {signal: controller.signal,
      method: body === undefined ? 'GET' : 'POST',
      headers: {'Authorization': 'Bearer64 ' + btoa(String.fromCharCode(...new TextEncoder().encode(token))), 'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok) { const error = new Error(data.error || '请求失败'); error.status = response.status; error.field = data.field; throw error; }
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
      const logButton = element('button', '查看日志', 'text-button task-log');
      logButton.addEventListener('click', async () => {
        await initializeLogs();
        if (!token) return;
        route('diagnostics', true);
        $('log-date').value = new Date(((record.finished_at || record.started_at) + 8 * 3600) * 1000).toISOString().slice(0, 10);
        $('log-task').value = record.id;
        loadLogs();
      });
      result.append(logButton);
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
function switchSettings(section, focus = false) {
  if (!['account','device','archive','notifications'].includes(section)) return;
  activeSettingsSection = section;
  if (section === 'notifications') loadNotificationPolicy();
  document.querySelectorAll('[data-settings-panel]').forEach(panel => { panel.hidden = panel.dataset.settingsPanel !== section; });
  document.querySelectorAll('[role=tab][data-settings-section]').forEach(button => {
    const selected = button.dataset.settingsSection === section;
    button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1;
    if (selected && focus) button.focus({preventScroll:true});
  });
}
document.querySelectorAll('[role=tab][data-settings-section]').forEach(button => {
  button.addEventListener('click', () => switchSettings(button.dataset.settingsSection));
  button.addEventListener('keydown', event => {
    const sections = ['account','device','archive','notifications']; let index = sections.indexOf(activeSettingsSection);
    if (event.key === 'ArrowRight') index = (index + 1) % sections.length;
    else if (event.key === 'ArrowLeft') index = (index - 1 + sections.length) % sections.length;
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = sections.length - 1;
    else return;
    event.preventDefault(); switchSettings(sections[index], true);
  });
});
function dateVisibility() {
  const selected = archiveForm.elements.namedItem('scope_mode').value === 'date';
  $('start-date-field').hidden = !selected;
  const date = archiveForm.elements.namedItem('start_date');
  date.required = selected;
  if (!selected) { date.value = ''; date.setCustomValidity(''); }
}
function fillGroup(group, settings, collapse = false) {
  for (const name of groupFields[group]) {
    const field = settingForms[group].elements.namedItem(name);
    if (field.type === 'checkbox') field.checked = settings[name]; else field.value = settings[name];
    field.setCustomValidity(''); field.removeAttribute('aria-invalid');
    if (name === 'repair_video') field.setAttribute('aria-describedby', 'ffmpeg-status'); else field.removeAttribute('aria-describedby');
  }
  settingForms[group].querySelectorAll('.field-error').forEach(error => error.remove());
  if (group === 'archive') {
    archiveForm.elements.namedItem('scope_mode').value = settings.start_date ? 'date' : 'all';
    dateVisibility(); if (settings.source) $('source-details').open = true;
  } else if (collapse) $('nas-connection').open = !settings.callback_ip;
}
function showSettingError(error, group) {
  const field = settingForms[group].elements.namedItem(error.field);
  if (!field) { failure(error); return; }
  switchSettings(group); field.closest('details')?.setAttribute('open', '');
  let details = field.parentElement;
  while (details && details !== settingForms[group]) { if (details.tagName === 'DETAILS') details.open = true; details = details.parentElement; }
  field.setCustomValidity(error.message); field.setAttribute('aria-invalid','true');
  let note = document.getElementById('error-' + field.name);
  if (!note) { note = element('small', '', 'field-error'); note.id = 'error-' + field.name; field.parentElement.append(note); }
  note.textContent = error.message; field.setAttribute('aria-describedby',note.id); field.reportValidity();
}
function updateControls() {
  dirty = dirtyGroups.size > 0;
  form.elements.namedItem('device_ip').required = !form.elements.namedItem('device_mac').value.trim();
  if (!lastData) return;
  const data = lastData;
  const repairBlocked = data.settings.repair_video && !data.ffmpeg.available;
  const reason = dirty || localDirty ? '有未保存的修改，请先保存对应分组。' : !data.session_ready ? '请先登录趴趴爪账号。' : !data.configured ? '请先完成设备连接设置。' : !data.settings.allow_maintenance ? '请先保存维护读取许可。' : !data.history_valid ? '任务历史无法读取，请查看诊断。' : repairBlocked ? '无损修复所需的 FFmpeg 不可用，请检查或关闭修复后保存。' : !data.storage.available || !data.storage.writable ? '请检查保存位置和写入权限。' : data.storage.free < data.policy.min_free_bytes ? '保存位置需保留至少 5 GB 可用空间。' : '';
  $('run').disabled = busy || data.running || Boolean(reason);
  $('run').title = reason || (data.running ? '正在执行一轮任务' : '按已保存设置运行一次');
  $('run-once-hint').textContent = data.running ? '当前任务结束后才能再运行一轮。' : reason || '按已保存设置执行一轮，不改变自动计划。';
  $('run-options-toggle').disabled = busy;
  $('run-readiness').hidden = !reason; $('run-blocker').textContent = reason;
  $('prepare-settings').dataset.page = !data.history_valid ? 'diagnostics' : 'settings';
  $('prepare-settings').dataset.settingsSection = dirtyGroups.has('device') || localDirty ? 'device' : dirtyGroups.has('archive') ? 'archive' : !data.session_ready ? 'account' : repairBlocked ? 'archive' : 'device';
  $('stop-now').hidden = !data.running; $('stop-now').disabled = busy;
  $('stop').disabled = busy || !data.running;
  for (const [group, settingForm] of Object.entries(settingForms)) {
    for (const control of settingForm.elements) control.disabled = busy;
    const changed = dirtyGroups.has(group);
    $('save-' + group).disabled = busy || data.running || !changed;
    $('save-' + group).title = data.running ? '本轮结束后可保存草稿' : changed ? '' : '没有未保存的修改';
    $(group + '-feedback').textContent = data.running ? (changed ? '草稿未生效；当前任务使用原设置，结束后可保存。' : '当前任务使用已保存设置；可先编辑下一轮草稿。') : changed ? '有未保存的修改，仅保存本分组后生效。' : '已保存的设置供之后的任务使用，保存不会启动任务。';
  }
  const repair = archiveForm.elements.namedItem('repair_video');
  repair.disabled = busy || (!data.ffmpeg.available && !repair.checked);
  $('ffmpeg-status').textContent = data.ffmpeg.message;
  $('refresh').disabled = busy;
  $('check-storage').disabled = busy || data.running;
  for (const control of $('password-form').elements) control.disabled = busy || !data.account.login_allowed;
  $('account-save').disabled = busy || data.running || !data.account.login_allowed;
  for (const id of ['session-file', 'save-output']) $(id).disabled = busy || data.running;
  $('local-output-input').disabled = busy;
  const account = data.account;
  const blocked = busy || data.running || !account.login_allowed || data.session_ready || account.logout_pending;
  for (const control of $('account-form').elements) control.disabled = blocked;
  $('send-code').disabled = blocked || account.sms_wait > 0;
  $('send-code').textContent = account.sms_wait > 0 ? account.sms_wait + ' 秒后可重发' : '发送验证码';
  $('account-submit').disabled = blocked || !account.code_pending;
  $('account-submit').textContent = mode === 'server' && $('remember-account').checked ? '登录并加密保存' : '临时登录';
  $('account-logout').disabled = busy || account.logout_pending;
  $('session-file').disabled = blocked || account.saved;
  $('automation-toggle').disabled = busy || (!data.settings.automatic && Boolean(reason));
  $('run-actions').dataset.automatic = String(data.settings.automatic);
  $('automation-toggle').setAttribute('aria-pressed', String(data.settings.automatic));
  $('automation-toggle').textContent = data.settings.automatic ? '自动归档运行中' : '开启自动归档';
  $('automation-toggle').title = data.settings.automatic ? '自动归档已开启，点击暂停；当前任务继续执行' : reason || (data.running ? '本轮结束后按已保存间隔继续' : '开启后立即执行一轮，之后按间隔自动运行');
  $('automation-hint').textContent = data.settings.automatic ? '暂停后不再启动新任务；当前任务继续执行。停止本轮请使用“停止当前任务”。' : data.running ? '开启后不打断本轮，完成后按已保存间隔继续。' : '开启后立即运行一轮。请先手动验收，再开启自动计划。';
  if (!data.settings.automatic && reason) $('automation-hint').textContent = reason;
}
function paint(data, fields = false) {
  const wasRunning = lastData && lastData.running;
  const previousAccount = lastData && lastData.account;
  lastData = data; setMode(data.mode);
  const account = data.account;
  const accountLabels = {temporary: '临时登录', saved: '已加密保存', external: '外部会话文件'};
  const accountLabel = account.save_problem ? '临时登录，保存失败' : account.logout_pending ? '正在停止并退出' : accountLabels[account.source] || (account.saved ? '保存文件需处理' : '未登录');
  badge('account-badge', accountLabel, data.session_ready ? 'positive' : '');
  $('account-transport').hidden = account.login_allowed;
  $('account-form').hidden = data.session_ready || account.saved || account.logout_pending;
  $('account-signed-in').hidden = !data.session_ready && !account.saved && !account.logout_pending && !account.source;
  $('account-save').hidden = mode !== 'server' || account.source !== 'temporary' || !data.session_ready;
  $('default-password-note').hidden = !data.default_password;
  $('account-detail').textContent = account.logout_pending ? '正在等待任务停止，完成后清除进程中的登录凭据。' : account.source === 'saved' ? '已在 NAS 加密保存登录状态，可在服务重启后恢复；厂商会话过期时仍需重新登录。' : account.source === 'external' ? '正在使用你提供的明文会话文件。退出会停用该文件，但不会删除宿主机原件。' : account.source === 'temporary' ? '登录凭据仅在当前服务内存中使用，程序未将它们保存到文件，重启后失效。' : '保存的登录状态无法使用，请退出后重新登录。';
  if (account.save_problem) $('account-detail').textContent += ' 加密保存失败：' + account.save_problem + '。修复权限后可点击加密保存，无需再次短信登录。';
  if (previousAccount && previousAccount.code_pending && !account.code_pending) $('account-form').reset();
  if (wasRunning && !data.running) message(data.last_result + (previousAccount.source === 'temporary' && !account.source ? '。临时登录已清除，下次导出请重新登录。' : ''), ![0,130,-15].includes(data.last_exit));
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
  else { title = settings.automatic ? '自动归档已就绪' : mode === 'desktop' ? '已就绪，等待手动运行' : '已就绪，可以开启自动归档'; label = '已就绪'; style = 'positive'; }
  $('state').textContent = title; badge('state-badge', label, style);
  $('result').textContent = data.running ? '正在读取并校验原片，完成后会保存本轮结果。' : data.last_result;
  $('timing').textContent = data.last_finished ? '上轮结束于 ' + stamp(data.last_finished) : '尚无已完成的任务';
  $('run-hint').textContent = dirty ? '有未保存的设置，请先保存。' : !data.configured ? '先填写设备 IP 或 MAC，以及回调地址。' : !data.session_ready ? '先在设置的账号分组登录。' : !settings.allow_maintenance ? '运行前需明确允许维护读取。' : '只处理当前已保存的导出范围。';
  badge('automatic-badge', settings.automatic ? '已开启' : '未开启', settings.automatic ? 'positive' : '');
  $('schedule-interval').textContent = '每轮完成后 ' + intervals[settings.interval];
  $('schedule-next').textContent = !settings.automatic ? (data.running ? '本轮结束后不再运行' : '未开启自动计划') : data.running ? '本轮结束后计算' : data.next_run ? (data.next_kind === 'retry' ? '重试 ' + data.retry_count + '/3 · ' : data.next_kind === 'storage' ? '检查空间 · ' : '') + stamp(data.next_run, true) : '手动执行';
  $('schedule-scope').textContent = settings.source ? '指定原片' : settings.start_date ? '自 ' + settings.start_date : '全部日期';
  badge('device-check', data.configured ? '已配置' : '待设置', data.configured ? 'positive' : '');
  $('device-subtitle').textContent = settings.device_ip ? settings.device_ip + (settings.device_mac ? ' · MAC 兜底' : '') : settings.device_mac ? '按 MAC 查找 · ' + settings.device_mac : '尚未填写设备地址';
  badge('device-saved-badge',data.configured ? '已配置' : '待完成',data.configured ? 'positive' : '');
  $('nas-summary').textContent = settings.callback_ip ? '保存端连接 · ' + settings.callback_ip + ' · 点击修改' : '保存端连接 · 待配置';
  badge('plan-state',settings.automatic ? '已开启' : '未开启',settings.automatic ? 'positive' : '');
  $('automation-detail').textContent = settings.automatic ? '自动计划已开启，每轮结束后等待 ' + intervals[settings.interval] + '。' : '自动计划未开启，仍可随时手动运行。';
  $('setup-guide').hidden = history.some(record => record.state === 'success');
  badge('session-check', data.session_ready ? '已准备' : '待登录', data.session_ready ? 'positive' : 'warning');
  $('session').textContent = data.session_ready ? accountLabel + '，实际有效性待导出验证' : '尚未准备可用登录状态';
  $('output').textContent = data.output;
  $('local-output-summary').textContent = data.output;
  $('local-scope').textContent = settings.source ? '指定原片' : settings.start_date ? '自 ' + settings.start_date : '全部日期';
  if (!localDirty) $('local-output-input').value = data.output;
  $('storage-state').textContent = storage.message;
  $('write-check-result').textContent = storage.write_check ? storage.write_check.message : '检查会创建、读取并删除一个临时文件，不修改已有录像；任务运行期间不可执行。';
  $('storage-capacity').textContent = bytes(storage.free) + ' / ' + bytes(storage.total);
  const issues = [];
  if (!data.configured) issues.push('设备连接尚未配置完整。');
  if (!data.session_ready) issues.push('尚未准备登录会话，暂不能执行导出。');
  if (data.configured && !settings.allow_maintenance) issues.push('维护读取许可尚未开启。');
  if (!storage.available || !storage.writable) issues.push(storage.message);
  if (storage.available && storage.free < data.policy.min_free_bytes) issues.push('可用空间不足 5 GB，暂停归档；不会自动删除录像。');
  if (data.problem) issues.push(data.problem);
  if (account.save_problem) issues.push('账号已临时登录，但未加密保存：' + account.save_problem);
  if (data.diagnostics_problem) issues.push(data.diagnostics_problem);
  if (data.default_password) issues.push('当前管理密码为 admin，请在设置中修改。');
  if (latest && ['failed', 'interrupted'].includes(latest.state)) issues.push(latest.result);
  $('attention-list').replaceChildren(...issues.map(text => element('li', text)));
  $('attention-panel').hidden = !issues.length;
  $('recent-empty').hidden = $('history-empty').hidden = history.length > 0;
  $('recent-table-wrap').hidden = $('history-table-wrap').hidden = !history.length;
  populateHistory(history.slice(0, 5), 'recent-rows', false); populateHistory(history, 'history-rows', true);
  $('diagnostic-list').replaceChildren(
    diagnostic('管理服务', '网页可以读取当前服务的状态', '已连接', 'positive'),
    diagnostic('无损修复工具', data.ffmpeg.message, data.ffmpeg.available ? '可用' : '不可用', data.ffmpeg.available ? 'positive' : 'warning'),
    diagnostic('设备配置', '仅验证字段格式，不代表设备在线', data.configured ? '已配置' : '待设置', data.configured ? 'positive' : 'warning'),
    diagnostic('账号登录状态', '仅显示是否已准备，不向页面返回凭据', data.session_ready ? '已准备' : '待登录', data.session_ready ? 'positive' : 'warning'),
    diagnostic('输出目录', storage.message + (storage.write_check ? '；' + storage.write_check.message : '；可在设置中检查实际写入权限'), storage.write_check ? (storage.write_check.passed ? '写入检查通过' : '写入检查失败') : storage.available && storage.writable ? '初检通过' : '需要检查', storage.available && storage.writable && (!storage.write_check || storage.write_check.passed) ? 'positive' : 'warning'),
    diagnostic('运行身份', 'UID ' + data.permissions.uid + ' / GID ' + data.permissions.gid, data.permissions.uid === 0 ? 'root：请配置专用账号' : '非 root', data.permissions.uid === 0 ? 'warning' : 'positive'),
    ...data.permissions.directories.map(check => diagnostic(check.name, check.message + (check.owner === null ? '' : '；所有者 UID ' + check.owner), check.state === 'ready' ? '初检通过' : '需要检查', check.state === 'ready' ? 'positive' : 'warning')),
    diagnostic('任务历史', data.history_valid ? '保存最近 100 轮记录' : '保留原文件，修复后重启服务', data.history_valid ? '可读取' : '读取失败', data.history_valid ? 'positive' : 'warning'),
    diagnostic('详细日志', data.diagnostics_problem || '按天记录，最近 14 天；点击查看才加载详细内容', data.diagnostics_problem ? '保存失败' : '已启用', data.diagnostics_problem ? 'warning' : 'positive'),
    diagnostic('设备在线与回调', '尚未对真实设备执行连通性验证', '未检测')
  );
  $('access-address').textContent = location.origin;
  $('access-transport').textContent = location.protocol === 'https:' ? 'HTTPS 加密连接' : 'HTTP · 仅限可信网络';
  if (fields) dirtyGroups.clear();
  for (const group of Object.keys(settingForms)) if (fields || !dirtyGroups.has(group)) fillGroup(group,settings,fields);
  $('updated-at').textContent = '状态更新于 ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
  updateControls();
}
function disconnect() {
  logsInitialized = false; logRequest++; $('log-output').textContent = '尚未加载日志。'; dirtyGroups.clear(); archiveForm.reset();
  setRunMenu(false);
  token = ''; clearInterval(timer); lastData = null; dirty = false; localDirty = false;
  $('key').value = ''; $('password-form').reset(); $('account-form').reset(); $('local-output-form').reset(); $('session-file').value = ''; form.reset(); $('console').hidden = true; $('navigation').hidden = true; $('login').hidden = false;
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
  document.body.classList.remove('signed-out'); switchSettings(data.session_ready ? (data.configured ? 'archive' : 'device') : 'account'); route(data.session_ready ? 'overview' : 'settings'); message(); clearInterval(timer);
  timer = setInterval(() => refresh(), 5000);
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); token = $('key').value;
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
  } catch (error) { disconnect(); message('自动连接未完成，请使用管理密码连接。', true); }
}
initialize();
$('local-output-input').addEventListener('input', () => { localDirty = true; message(); updateControls(); });
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
for (const [group, settingForm] of Object.entries(settingForms)) {
  const edited = event => {
    const field = event.target;
    if (field.setCustomValidity) { field.setCustomValidity(''); field.removeAttribute('aria-invalid'); field.removeAttribute('aria-describedby'); document.getElementById('error-' + field.name)?.remove(); }
    dirtyGroups.add(group); message(); if (group === 'archive') dateVisibility(); updateControls();
  };
  settingForm.addEventListener('input', edited); settingForm.addEventListener('change',edited);
  settingForm.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || lastData.running || !dirtyGroups.has(group)) return;
    for (const field of settingForm.elements) if (field.validity && !field.validity.valid) { let parent = field.parentElement; while (parent && parent !== settingForm) { if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement; } }
    if (!settingForm.reportValidity()) return;
    const settings = {};
    for (const name of groupFields[group]) { const field = settingForm.elements.namedItem(name); settings[name] = field.type === 'checkbox' ? field.checked : field.type === 'number' ? Number(field.value) : field.value.trim(); }
    if (group === 'archive' && archiveForm.elements.namedItem('scope_mode').value === 'all') settings.start_date = '';
    busy = true; updateControls(); const session = token;
    try { const data = await api('settings/' + group, settings); if (session === token && token) { dirtyGroups.delete(group); fillGroup(group,data.settings,true); paint(data); message((group === 'device' ? '设备' : '归档') + '设置已保存，未启动任务。'); } }
    catch (error) { if (session === token && token) { if (error.field) { busy = false; updateControls(); showSettingError(error,group); } else failure(error); } }
    finally { busy = false; updateControls(); }
  });
}
function setRunMenu(open, focus = false) {
  if (mode === 'desktop') return;
  $('run-options-menu').hidden = !open;
  $('run-options-toggle').setAttribute('aria-expanded', String(open));
  if (open && focus) ($('run').disabled ? $('run-options-menu') : $('run')).focus({preventScroll:true});
}
$('run-options-toggle').addEventListener('click', () => setRunMenu($('run-options-menu').hidden, true));
$('run-options-toggle').addEventListener('keydown', event => {
  if (['ArrowDown','ArrowUp'].includes(event.key)) { event.preventDefault(); setRunMenu(true,true); }
  if (event.key === 'Escape') setRunMenu(false);
});
$('run-options-menu').addEventListener('keydown', event => {
  if (event.key === 'Escape') { event.preventDefault(); setRunMenu(false); $('run-options-toggle').focus({preventScroll:true}); }
  if (event.key === 'Tab') setRunMenu(false);
});
document.addEventListener('click', event => { if (!$('run-actions').contains(event.target)) setRunMenu(false); });
$('remember-account').addEventListener('change', updateControls);
$('automation-toggle').addEventListener('click', async () => {
  if (busy || !lastData) return;
  setRunMenu(false);
  const enabled = !lastData.settings.automatic; busy = true; updateControls(); const session = token;
  try { const data = await api('automation',{enabled}); if (session === token && token) { paint(data); message(enabled ? (data.running ? '自动归档已开启，本轮结束后按间隔继续。' : '自动归档已开启，即将执行第一轮。') : '自动归档已暂停，当前任务继续执行。'); } }
  catch (error) { if (session === token && token) failure(error); }
  finally { busy = false; updateControls(); }
});
for (const [id, action] of [['run','run'], ['stop','stop'], ['stop-now','stop']]) $(id).addEventListener('click', async () => {
  if (busy || (action === 'run' && $('run').disabled)) return;
  setRunMenu(false); busy = true; updateControls(); const session = token;
  try { const data = await api(action, {}); if (session === token && token) {
    paint(data); message(action === 'run' ? '已开始本轮导出。' : mode === 'desktop' ? '已请求停止本次导出。' : '已请求停止当前任务，自动计划保持原状态。');
  } } catch (error) { if (session === token && token) failure(error); }
  finally { busy = false; updateControls(); }
});
$('refresh').addEventListener('click', () => refresh(true));
$('logout').addEventListener('click', () => { disconnect(); message('已断开管理页面。任务继续按已保存的设置执行。'); });

const logStages = {task:'任务', session:'读取登录状态', storage:'存储检查', mac_discovery:'MAC 地址查找', mqtt:'MQTT 连接', catalog:'目录回传', transfer:'原片传输', checksum:'原片校验', repair:'无损修复', cleanup:'关闭维护通道'};
const logActions = {start:'开始', success:'完成', failed:'失败', retry:'准备重试', waiting:'等待设备回传', command_sent:'命令已发送至 MQTT', callback_received:'收到回传', callback_rejected:'拒绝回传', subscribed:'订阅成功', disconnected:'连接断开', stopped:'已停止', paused:'空间不足暂停', interrupted:'服务中断，结果未确认'};
async function initializeLogs() {
  const current = token;
  try {
    const info = await api('logs');
    if (!token || current !== token) return;
    if (!$('log-date').value) $('log-date').value = info.dates[0] || info.today;
    const selected = $('log-task').value;
    const all = element('option', '当天全部任务'); all.value = '';
    $('log-task').replaceChildren(all, ...(lastData?.history || []).map(record => {
      const option = element('option', stamp(record.started_at) + ' · ' + (states[record.state]?.[0] || record.state) + ' · ' + record.id);
      option.value = record.id; return option;
    }));
    $('log-task').value = selected;
    logsInitialized = true;
  } catch (error) { if (token && current === token) failure(error); }
}
async function loadLogs(download = false) {
  if (!token || logsBusy) return;
  const current = token, request = ++logRequest;
  const day = $('log-date').value;
  const query = new URLSearchParams({date:day, task:$('log-task').value});
  logsBusy = true; $('load-logs').disabled = $('download-logs').disabled = true;
  $('log-date').disabled = $('log-task').disabled = true;
  try {
    if (download) {
      query.set('download', '1');
      const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
      let response;
      try { response = await fetch('/api/logs?' + query, {signal:controller.signal, headers:{'Authorization':'Bearer64 ' + btoa(String.fromCharCode(...new TextEncoder().encode(token)))}});
        if (!response.ok) { const error = new Error((await response.json()).error || '日志下载失败'); error.status = response.status; throw error; }
        const blob = await response.blob();
        if (current !== token || request !== logRequest) return;
        const url = URL.createObjectURL(blob), link = element('a', '');
        link.href = url; link.download = 'pawzz-diagnostics-' + day + '.jsonl'; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } finally { clearTimeout(timeout); }
    } else {
      const data = await api('logs?' + query);
      if (current !== token || request !== logRequest) return;
      $('log-output').textContent = data.entries.slice().reverse().map(row => {
        const details = Object.entries(row).filter(([key]) => !['time','task_id','stage','action'].includes(key)).map(([key,value]) => key + '=' + value).join(' ');
        const reason = row.reason === 'source_mismatch' ? '来源 IP 与预期不符' : row.reason === 'route_mismatch' ? '回传地址校验失败' : '';
        const time = new Date(row.time * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false});
        return time + ' [' + row.task_id + '] ' + (logStages[row.stage] || row.stage) + ' · ' + (logActions[row.action] || row.action) + (reason ? ' · ' + reason : '') + (details ? ' · ' + details : '');
      }).join('\n') || '所选日期和任务没有详细日志；旧任务不会补生成记录，过期或超出容量的记录会被清理。';
      $('log-note').textContent = data.truncated ? '匹配 ' + data.count + ' 条，当前仅显示最近 200 条（新记录在前）。下载可获取所选范围内全部保留记录。' : '显示 ' + data.entries.length + ' 条（新记录在前）。日期按北京时间分日；跨午夜任务请切换日期查看。';
    }
  } catch (error) { if (current === token && token) failure(error); }
  finally { logsBusy = false; $('load-logs').disabled = $('download-logs').disabled = false; $('log-date').disabled = $('log-task').disabled = false; }
}
$('load-logs').addEventListener('click', () => loadLogs());
$('download-logs').addEventListener('click', () => loadLogs(true));

async function accountAction(action, body) {
  if (busy) return;
  busy = true; updateControls(); const current = token;
  try {
    const data = await api('account/' + action, body);
    if (current === token && token) {
      paint(data);
      const notices = {send: '已请求发送短信。收到后填写验证码，不会自动重发。', login: data.account.save_problem ? '登录成功，但加密保存失败。当前为临时登录，重启后失效；修复权限后可直接再次保存。' : data.account.source === 'saved' ? '登录成功，已加密保存。确认设备设置与维护许可后，再开始导出。' : '临时登录成功，重启后失效。确认设备设置与维护许可后，再开始导出。', save: data.account.save_problem ? '仍未加密保存：' + data.account.save_problem : '当前登录已加密保存，无需重新登录。', logout: data.account.logout_pending ? '已请求停止任务，停止后清除登录凭据。' : '已退出账号。本程序保存的凭据已移除；外部原件与备份请自行管理。', cancel: '已取消本次验证码验证并清空输入。'};
      message(notices[action], Boolean(data.account.save_problem));
      if (action !== 'send') $('account-form').reset();
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

$('account-save').addEventListener('click', () => accountAction('save', {}));
$('check-storage').addEventListener('click', async () => {
  if (busy) return;
  busy = true; updateControls(); const current = token;
  try {
    const data = await api('storage/check', {});
    if (current === token && token) { paint(data); message(data.storage.write_check.message, !data.storage.write_check.passed); }
  } catch (error) { if (current === token && token) failure(error); }
  finally { busy = false; updateControls(); }
});
$('password-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  const password = $('new-password').value;
  const body = {current: $('current-password').value, password, confirm: $('confirm-password').value};
  if (password !== body.confirm) { message('两次输入的新密码不一致。', true); return; }
  busy = true; updateControls(); const current = token;
  try {
    const data = await api('password', body);
    if (current === token && token) {
      token = password; $('password-form').reset(); paint(data);
      message('管理密码已修改并立即生效，其他页面需要用新密码重新连接。');
    }
  } catch (error) { if (current === token && token) failure(error); }
  finally { busy = false; updateControls(); }
});
window.addEventListener('pagehide', () => $('password-form').reset());

async function loadNotificationPolicy() {
  try {
    const result = await api('notification-settings');
    const form = $('notification-policy-form');
    if (form.dataset.dirty !== 'true') {
    form.elements.failure_threshold.value = result.policy.failure_threshold;
    form.elements.reminder_minutes.value = result.policy.reminder_minutes;
    form.elements.recovery.checked = result.policy.recovery;
    form.elements.immediate_codes.value = result.policy.immediate_codes.join(',');
    }
    $('notification-connection').textContent = result.configured ? '通知适配器已启用' : '尚未接入通知中心';
    const relay=result.relay;
    const errors={authentication_failed:'项目凭据无效',connection_refused:'无法连接通知中心',dns_failed:'地址无法解析',timeout:'上报超时',rate_limited:'上报限流',server_error:'通知中心内部错误',tls_failed:'证书或 TLS 连接错误'};
    const errorName=code=>errors[code]||code||'无';
    $('notification-relay-status').textContent=relay ? `通知上报：待传 ${relay.pending} 条；最近成功 ${relay.last_success ? new Date(relay.last_success*1000).toLocaleString() : '尚无'}；当前错误 ${errorName(relay.last_error)}；最近故障 ${errorName(relay.last_failure)}；故障排障编号 ${relay.last_failure_request_id||'无'}。` : '尚无上报状态；不会在这里显示项目凭据。';
    $('notification-policy-message').textContent = result.problem || '策略由发送端保存，推送渠道在通知中心按项目选择。';
  } catch (error) { $('notification-policy-message').textContent = error.message; }
}
$('notification-policy-form').addEventListener('input', () => { $('notification-policy-form').dataset.dirty = 'true'; });
$('notification-policy-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('button'); button.disabled = true;
  try {
    await api('notification-settings', {failure_threshold:Number(form.elements.failure_threshold.value), reminder_minutes:Number(form.elements.reminder_minutes.value), recovery:form.elements.recovery.checked, immediate_codes:form.elements.immediate_codes.value.split(',').map(x => x.trim()).filter(Boolean)});
    form.dataset.dirty = 'false'; await loadNotificationPolicy(); $('notification-policy-message').textContent = '发送端通知策略已保存；下载重试设置保持原样。';
  } catch (error) { $('notification-policy-message').textContent = error.message; } finally { button.disabled = false; }
});

$('refresh-notification-status').addEventListener('click',loadNotificationPolicy);
