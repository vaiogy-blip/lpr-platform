// PSC 雲平台前端：一頁式，網址 # 後面決定顯示哪一頁。所有從伺服器來的文字都經過 esc() 再放進畫面。
'use strict';
const $ = id => document.getElementById(id);
const main = () => $('main');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pad = n => String(n).padStart(2, '0');
const isoDate = d => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
let me = null, permDefs = [], siteKinds = {};
const can = p => me && me.perms.includes(p);

async function api(name, body) {
  let r;
  try {
    r = await fetch('/api/' + name, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  } catch (e) { return {ok: false, error: '連不到 PSC 平台，請檢查網路'}; }
  const d = await r.json().catch(() => ({ok: false, error: '平台回應看不懂'}));
  if (d.login) { showLogin(); }
  else if (d.must_change_pw) { location.hash = '#/account'; }
  return d;
}
function say(el, text, ok) { el.textContent = text; el.className = 'msg ' + (ok ? 'ok' : 'bad'); }
function busy(btn, on) { if (btn) btn.disabled = on; }

// ---------- 登入 ----------
function showLogin() {
  me = null; $('shell').classList.add('hidden'); $('login').classList.remove('hidden'); $('lu').focus();
}
$('lbtn').addEventListener('click', async () => {
  busy($('lbtn'), true);
  const d = await api('login', {username: $('lu').value.trim(), password: $('lp').value});
  busy($('lbtn'), false); $('lp').value = '';
  if (!d.ok) return say($('lmsg'), d.error);
  $('lmsg').textContent = ''; pickedCompany = null; start();
});
$('lp').addEventListener('keydown', e => { if (e.key === 'Enter') $('lbtn').click(); });
$('lu').addEventListener('keydown', e => { if (e.key === 'Enter') $('lp').focus(); });

async function start() {
  const d = await api('me');
  if (!d.ok) return;
  me = d.user; permDefs = d.perm_defs; siteKinds = d.site_kinds;
  $('login').classList.add('hidden'); $('shell').classList.remove('hidden');
  if (me.must_change_pw) location.hash = '#/account';
  route();
}

// ---------- 選單與路由 ----------
const PAGES = [
  ['dashboard', '總覽', () => can('site.view')],
  ['sites', '場站', () => can('site.view')],
  ['events', '進出紀錄', () => can('event.view')],
  ['companies', '客戶公司', () => can('company.manage')],
  ['users', '帳號', () => can('user.manage')],
  ['roles', '權限群組', () => can('user.manage')],
  ['audit', '操作紀錄', () => can('audit.view')],
  ['account', '我的帳號', () => true],
];
function renderNav(cur) {
  $('nav').innerHTML = '<div class="brand">PSC</div><div class="who">' + esc(me.display_name) + '・' + esc(me.company_name) + '<br>' + esc(me.role_name) + '</div>' +
    PAGES.filter(p => p[2]()).map(p => '<a href="#/' + p[0] + '" class="' + (cur === p[0] ? 'on' : '') + '">' + p[1] + '</a>').join('') +
    '<div class="spacer"></div><a id="logout">登出</a>';
  $('logout').addEventListener('click', async () => { await api('logout'); showLogin(); });
}
window.addEventListener('hashchange', () => me && route());
function route() {
  const [page, arg] = location.hash.replace(/^#\//, '').split('/');
  if (me.must_change_pw && page !== 'account') { location.hash = '#/account'; return; }
  const name = page === 'site' ? 'sites' : (PAGES.find(p => p[0] === page && p[2]()) ? page : 'dashboard');
  renderNav(name);
  main().innerHTML = '<p class="muted">讀取中…</p>';
  const views = {dashboard: viewDashboard, sites: viewSites, events: () => viewEvents(arg ? +arg : null), companies: viewCompanies, users: viewUsers, roles: viewRoles, audit: viewAudit, account: viewAccount};
  if (page === 'site' && arg) return viewSite(+arg);
  (views[name] || viewDashboard)();
}

// 主控可以切換要管哪一家公司
let pickedCompany = null;
function bindPicker(onChange) {
  if ($('cpick')) $('cpick').addEventListener('change', e => { pickedCompany = +e.target.value; onChange(); });
}
async function companyPicker() {
  if (!me.is_root || !can('company.manage')) return '';
  const d = await api('companies');
  if (!d.ok) return '';
  return '<div class="row"><label for="cpick">公司</label><select id="cpick" class="auto">' +
    d.companies.map(c => '<option value="' + c.id + '"' + (c.id === (pickedCompany || me.company_id) ? ' selected' : '') + '>' + esc(c.name) + '</option>').join('') + '</select></div>';
}
const cid = () => pickedCompany || me.company_id;

// ---------- 總覽 ----------
async function viewDashboard() {
  const d = await api('dashboard');
  if (!d.ok) return main().innerHTML = '<p class="bad">' + esc(d.error) + '</p>';
  const c = d.counts;
  const stat = (n, label, cls) => '<div class="stat"><b class="' + (cls || '') + '">' + n + '</b><span>' + label + '</span></div>';
  let html = '<h1>總覽</h1><p class="muted">' + esc(me.company_name) + (me.is_root ? '（主控，看得到全部公司）' : '') + '</p><div class="grid">' +
    (me.is_root ? stat(c.companies, '公司') : '') + stat(c.sites, '場站') +
    stat(c.gateways_online + ' / ' + c.gateways, '小主機線上', c.gateways_online < c.gateways ? 'bad' : 'ok') +
    stat(c.cameras, '攝影機') + stat(c.sites_offline, '場站有斷線', c.sites_offline ? 'bad' : '') +
    stat(c.sites_no_gateway, '場站還沒裝小主機', c.sites_no_gateway ? 'warn' : '') +
    (c.today_events === null ? '' : stat(c.today_events, '今日進出') + stat(c.today_abnormal, '今日非白名單', c.today_abnormal ? 'warn' : '')) +
    '</div><h2>各場站狀態</h2>';
  const groups = {};
  d.sites.forEach(s => (groups[s.company_name] = groups[s.company_name] || []).push(s));
  if (!d.sites.length) html += '<div class="card muted">還沒有場站。' + (can('site.manage') ? '到「場站」新增第一個。' : '') + '</div>';
  for (const [company, sites] of Object.entries(groups)) {
    html += '<div class="card company"><h3>' + esc(company) + '</h3>' + sites.map(siteLine).join('') + '</div>';
  }
  main().innerHTML = html;
}
function siteLine(s) {
  const on = s.gateways.filter(g => g.online).length;
  const state = !s.gateways.length ? '<span class="warn">還沒裝小主機</span>' :
    '<span class="' + (s.online ? 'ok' : 'bad') + '"><span class="dot ' + (s.online ? 'on' : 'off') + '"></span>' + (s.online ? '線上' : '斷線') + '</span>';
  const cams = s.gateways.reduce((n, g) => n + g.cameras.length, 0);
  const last = s.gateways.map(g => esc(g.name) + ' ' + esc(g.last_seen)).join('、');
  return '<div class="site"><div><a href="#/site/' + s.id + '"><b>' + esc(s.name) + '</b></a> <span class="tag">' + esc(siteKinds[s.kind] || s.kind) + '</span>' +
    '<div class="small muted">小主機 ' + on + '/' + s.gateways.length + ' 線上・攝影機 ' + cams + ' 支' + (last ? '・最後連線 ' + last : '') +
    (s.today ? '・<a href="#/events/' + s.id + '">今日進出 ' + s.today.n + ' 筆' + (s.today.abnormal ? '（非白名單 ' + s.today.abnormal + '）' : '') + '</a>' : '') + '</div></div>' +
    '<div>' + state + '</div></div>';
}

// ---------- 場站清單 ----------
async function viewSites() {
  const d = await api('sites');
  if (!d.ok) return main().innerHTML = '<p class="bad">' + esc(d.error) + '</p>';
  let html = '<h1>場站</h1><div class="card">' + (d.sites.map(siteLine).join('') || '<span class="muted">還沒有場站</span>') + '</div>';
  if (can('site.manage') && me.site_ids === null) {
    html += '<h2>新增場站</h2><div class="card"><div class="form">' +
      (d.companies.length > 1 ? '<div><label for="scomp">所屬公司</label><select id="scomp">' + d.companies.map(c => '<option value="' + c.id + '">' + esc(c.name) + '</option>').join('') + '</select></div>' : '') +
      '<div><label for="sname">場站名稱</label><input id="sname" placeholder="例如 OO社區"></div>' +
      '<div><label for="skind">類型</label><select id="skind">' + Object.entries(siteKinds).map(([k, v]) => '<option value="' + k + '">' + esc(v) + '</option>').join('') + '</select></div>' +
      '<div><label for="saddr">地址（可不填）</label><input id="saddr"></div><div><button id="sadd">新增</button></div></div><div class="msg" id="smsg"></div></div>';
  }
  main().innerHTML = html;
  if ($('sadd')) $('sadd').addEventListener('click', async () => {
    busy($('sadd'), true);
    const r = await api('site_save', {company_id: $('scomp') ? +$('scomp').value : null, name: $('sname').value, kind: $('skind').value, address: $('saddr').value});
    busy($('sadd'), false);
    if (!r.ok) return say($('smsg'), r.error);
    location.hash = '#/site/' + r.id;
  });
}

// ---------- 單一場站 ----------
let camTab = 'wl';
async function viewSite(id) {
  const d = await api('sites');
  if (!d.ok) return main().innerHTML = '<p class="bad">' + esc(d.error) + '</p>';
  const s = d.sites.find(x => x.id === id);
  if (!s) return main().innerHTML = '<p class="bad">找不到這個場站，或你沒有權限</p>';
  const cams = s.gateways.flatMap(g => g.cameras.map(c => ({gw: g, cam: c})));
  let html = '<p><a href="#/sites">← 場站</a></p><h1>' + esc(s.name) + '</h1><p class="muted">' + esc(s.company_name) + '・' + esc(siteKinds[s.kind] || s.kind) + (s.address ? '・' + esc(s.address) : '') + '</p>';
  html += '<h2>現場小主機</h2><div class="card"><div class="scroll"><table><thead><tr><th>名稱</th><th>狀態</th><th>版本</th><th>最後連線</th><th>攝影機</th>' + (can('site.manage') ? '<th></th>' : '') + '</tr></thead><tbody>' +
    (s.gateways.map(g => '<tr><td>' + esc(g.name) + '<div class="small muted">' + esc(g.id) + '</div></td><td><span class="dot ' + (g.online ? 'on' : 'off') + '"></span>' + (g.online ? '線上' : '離線') + '</td><td>' + esc(g.version || '-') +
      '</td><td>' + esc(g.last_seen) + '</td><td>' + (g.cameras.map(c => esc(c.name) + pushState(g, c)).join('<br>') || '<span class="muted">還沒回報</span>') + '</td>' +
      (can('site.manage') ? '<td><button class="ghost sm" data-reset="' + esc(g.id) + '">重發通行碼</button></td>' : '') + '</tr>').join('') ||
      '<tr><td colspan="6" class="muted">還沒有小主機</td></tr>') + '</tbody></table></div>';
  if (can('site.manage')) html += '<div class="row mt"><input id="gname" placeholder="新小主機名稱，例如 警衛室電腦" class="auto"><button id="gadd">新增小主機</button></div><div class="msg" id="gmsg"></div><div id="gcfg"></div>';
  html += '</div>';
  if (can('site.manage')) html += '<details class="card"><summary>修改場站資料</summary><div class="form mt">' +
    '<div><label for="ename">名稱</label><input id="ename" value="' + esc(s.name) + '"></div>' +
    '<div><label for="ekind">類型</label><select id="ekind">' + Object.entries(siteKinds).map(([k, v]) => '<option value="' + k + '"' + (k === s.kind ? ' selected' : '') + '>' + esc(v) + '</option>').join('') + '</select></div>' +
    '<div><label for="eaddr">地址</label><input id="eaddr" value="' + esc(s.address) + '"></div><div><button id="esave">儲存</button></div></div><div class="msg" id="emsg"></div></details>';
  if (can('event.view')) html += '<p><a href="#/events/' + s.id + '">→ 看這個場站的進出紀錄</a></p>';
  html += '<h2>攝影機</h2>';
  if (!cams.length) html += '<div class="card muted">小主機連上來並回報攝影機後，這裡就能管理。</div>';
  else html += '<div class="card"><div class="row"><label for="camsel">選擇攝影機</label><select id="camsel" class="auto">' +
    cams.map((c, i) => '<option value="' + i + '">' + esc(c.cam.name) + '（' + esc(c.gw.name) + (c.gw.online ? '' : '・離線') + '）</option>').join('') + '</select></div>' +
    '<div class="tabs" id="camtabs"></div><div id="campane"></div></div>';
  main().innerHTML = html;

  main().querySelectorAll('[data-reset]').forEach(b => b.addEventListener('click', async () => {
    if (!confirm('重發通行碼後，現場小主機要換上新的設定檔才能再連線。確定嗎？')) return;
    const r = await api('gateway_reset', {gateway_id: b.dataset.reset});
    if (!r.ok) return say($('gmsg'), r.error);
    showGatewayConfig(r.config, r.note);
  }));
  if ($('gadd')) $('gadd').addEventListener('click', async () => {
    busy($('gadd'), true);
    const r = await api('gateway_add', {site_id: s.id, name: $('gname').value});
    busy($('gadd'), false);
    if (!r.ok) return say($('gmsg'), r.error);
    $('gmsg').textContent = ''; showGatewayConfig(r.config, '');
  });
  if ($('esave')) $('esave').addEventListener('click', async () => {
    const r = await api('site_save', {id: s.id, name: $('ename').value, kind: $('ekind').value, address: $('eaddr').value});
    if (!r.ok) return say($('emsg'), r.error);
    viewSite(s.id);
  });
  if (cams.length) {
    const pick = () => renderCamera(cams[+$('camsel').value]);
    $('camsel').addEventListener('change', pick); pick();
  }
}
function pushState(g, c) {
  if (g.online && g.camera_push === null) return ' <span class="small muted">（小主機 0.4 版起才看得到推送狀態）</span>';
  if (!g.online || !(c.id in (g.camera_push || {}))) return ' <span class="small muted">（沒收到推送）</span>';
  return g.camera_push[c.id] ? ' <span class="small ok">● 推送中</span>' : ' <span class="small bad">● 推送中斷</span>';
}
function showGatewayConfig(cfg, note) {
  const text = JSON.stringify(cfg, null, 2);
  $('gcfg').innerHTML = '<div class="notice"><b>小主機設定檔（只顯示這一次）</b><p class="small">把下面內容存成 <code>gateway_config.json</code>，跟 現場小主機.exe 放同一個資料夾。' +
    '<b>cameras 裡的 IP 要改成現場攝影機的 IP</b>。裡面的 token 等於這台小主機的鑰匙，不要外流。' + (note ? '<br>' + esc(note) : '') + '</p>' +
    '<textarea rows="12" readonly>' + esc(text) + '</textarea><div class="row mt"><button id="cfgdl">下載設定檔</button><button class="ghost" id="cfgcp">複製</button></div></div>';
  $('cfgdl').addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], {type: 'application/json'}));
    a.download = 'gateway_config.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  $('cfgcp').addEventListener('click', () => navigator.clipboard.writeText(text).then(() => $('cfgcp').textContent = '已複製'));
}

// ---------- 攝影機分頁 ----------
const CAM_TABS = [['wl', '白名單', 'wl.view'], ['photo', '照片', 'photo.view'], ['param', '參數', 'param.view'], ['led', '字幕機', 'param.view'], ['maint', '維護', 'param.view']];
let cur = null;  // 目前選的 {gw, cam}
const cmd = (type, args) => api('cmd', {gateway: cur.gw.id, camera: cur.cam.id, type, args: args || {}});
function renderCamera(c) {
  cur = c;
  const tabs = CAM_TABS.filter(t => can(t[2]));
  if (!tabs.find(t => t[0] === camTab)) camTab = tabs.length ? tabs[0][0] : '';
  $('camtabs').innerHTML = tabs.map(t => '<a data-tab="' + t[0] + '" class="' + (t[0] === camTab ? 'on' : '') + '">' + t[1] + '</a>').join('');
  $('camtabs').querySelectorAll('a').forEach(a => a.addEventListener('click', () => { camTab = a.dataset.tab; renderCamera(cur); }));
  const pane = $('campane');
  if (!c.gw.online) { pane.innerHTML = '<p class="bad">這台攝影機的小主機目前離線，等它連上來再操作。</p>'; return; }
  ({wl: tabWhitelist, photo: tabPhoto, param: tabParam, led: tabLed, maint: tabMaint}[camTab] || (() => pane.innerHTML = '<p class="muted">你沒有可以使用的功能</p>'))(pane);
}

// 白名單
function tabWhitelist(pane) {
  const t = new Date(), ny = new Date(t); ny.setFullYear(ny.getFullYear() + 1);
  pane.innerHTML = (can('wl.edit') ? '<div class="form"><div><label for="wplate">車牌（不用加 -）</label><input id="wplate" placeholder="例如 ABC1234" autocomplete="off"></div>' +
    '<div><label for="wbegin">開始（日期時間）</label><input id="wbegin" type="datetime-local" step="1" value="' + isoDate(t) + 'T00:00:00"></div>' +
    '<div><label for="wend">結束（日期時間）</label><input id="wend" type="datetime-local" step="1" value="' + isoDate(ny) + 'T23:59:59"></div>' +
    '<div><button id="wadd">新增／更新</button></div></div>' +
    '<p class="small warn">⚠️ 白名單車會自動開閘。時間（時:分:秒）攝影機怎麼解讀還沒實機驗證，正式社區請先維持 00:00:00～23:59:59。</p>' : '') +
    '<div class="row mt"><button class="ghost sm" id="wexport">匯出 Excel（CSV）</button>' +
    (can('wl.edit') ? '<label class="ghost sm filebtn">批次匯入（CSV）<input type="file" id="wfile" accept=".csv,text/csv" class="hidden"></label>' +
      '<span class="small muted">格式：車牌,開始,結束（例 ABC1234,2026/11/03,2027/11/02；可帶時間）</span>' : '') + '</div><div id="wimport"></div>' +
    '<div class="msg" id="wmsg"></div><div class="scroll"><table><thead><tr><th>車牌</th><th>有效期間</th><th>狀態</th><th></th></tr></thead><tbody id="wlist"><tr><td colspan="4" class="muted">讀取中…</td></tr></tbody></table></div>';
  let lastItems = [];
  const load = async () => {
    const d = await cmd('wl_list');
    if (!$('wlist')) return;  // 等結果時已經切到別的分頁
    if (d.ok) lastItems = d.data.items;
    if (!d.ok) return $('wlist').innerHTML = '<tr><td colspan="4" class="bad">' + esc(d.error) + '</td></tr>';
    const items = d.data.items;
    $('wlist').innerHTML = items.length ? items.map(i => '<tr><td><b>' + esc(i.plate) + '</b></td><td>' + esc(i.begin) + ' ～ ' + esc(i.end) + '</td><td class="' +
      (i.state === '有效' ? 'ok' : i.state === '已過期' ? 'bad' : 'warn') + '">' + esc(i.state) + '</td><td>' +
      (can('wl.edit') ? '<button class="danger sm" data-plate="' + esc(i.plate) + '">刪除</button>' : '') + '</td></tr>').join('') : '<tr><td colspan="4" class="muted">白名單是空的</td></tr>';
  };
  $('wlist').addEventListener('click', async e => {
    const p = e.target.dataset.plate; if (!p) return;
    if (!confirm('確定要從攝影機刪除 ' + p + ' 嗎？刪除後這台車不會自動開閘。')) return;
    e.target.disabled = true;
    const d = await cmd('wl_delete', {plate: p});
    say($('wmsg'), d.ok ? '已刪除 ' + p : d.error, d.ok); load();
  });
  if ($('wadd')) {
    $('wadd').addEventListener('click', async () => {
      busy($('wadd'), true);
      const p = $('wplate').value;
      const d = await cmd('wl_add', {plate: p, begin: $('wbegin').value, end: $('wend').value});
      busy($('wadd'), false);
      if (d.ok) { say($('wmsg'), '已寫入攝影機：' + p.toUpperCase().replace(/[- ]/g, ''), true); $('wplate').value = ''; } else say($('wmsg'), d.error);
      load();
    });
    $('wplate').addEventListener('keydown', e => { if (e.key === 'Enter') $('wadd').click(); });
    $('wfile').addEventListener('change', e => { const f = e.target.files[0]; e.target.value = ''; if (f) importCsv(f, load); });
  }
  $('wexport').addEventListener('click', () => {
    if (!lastItems.length) return say($('wmsg'), '白名單是空的，或還沒讀完');
    const csv = '\ufeff車牌,開始,結束,狀態\r\n' + lastItems.map(i => [i.plate, i.begin, i.end, i.state].map(csvCell).join(',')).join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
    a.download = '白名單_' + cur.cam.name + '_' + isoDate(new Date()) + '.csv'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  load();
}

const csvCell = v => {
  let t = String(v);
  if (/^[=+\-@]/.test(t)) t = "'" + t;  // 防止 Excel 把內容當公式執行
  return /[",\r\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
};
// 「2026/11/03」「2026-11-03 08:00」「2026/11/03 08:00:00」都轉成平台要的 2026-11-03T08:00:00；只給日期就原樣（開始 00:00:00、結束 23:59:59）
function normTime(t) {
  const m = String(t || '').trim().match(/^(\d{4})[\/.-](\d{1,2})[\/.-](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$/);
  if (!m) return null;
  const d = m[1] + '-' + pad(+m[2]) + '-' + pad(+m[3]);
  return m[4] === undefined ? d : d + 'T' + pad(+m[4]) + ':' + m[5] + ':' + (m[6] || '00');
}
async function importCsv(file, reload) {
  const box = $('wimport');
  const text = await file.text();
  const rows = [], bad = [];
  text.replace(/^\ufeff/, '').split(/\r?\n/).forEach((line, i) => {
    const cols = line.split(',').map(c => c.replace(/^"|"$/g, '').trim());
    if (!cols[0]) return;
    const plate = cols[0].replace(/[- ]/g, '').toUpperCase();
    if (!/^[A-Z0-9]{2,10}$/.test(plate)) { if (i > 0) bad.push('第' + (i + 1) + '行 ' + cols[0]); return; }  // 第一行通常是標題
    const begin = normTime(cols[1]), end = normTime(cols[2]);
    if (!begin || !end) { bad.push('第' + (i + 1) + '行 ' + plate + '（日期看不懂）'); return; }
    rows.push({plate, begin, end});
  });
  if (!rows.length) { box.innerHTML = '<p class="bad">檔案裡找不到可以匯入的車牌。' + esc(bad.slice(0, 5).join('、')) + '</p>'; return; }
  box.innerHTML = '<div class="notice"><b>要匯入 ' + rows.length + ' 筆到「' + esc(cur.cam.name) + '」</b>' +
    (bad.length ? '<p class="small warn">有 ' + bad.length + ' 行看不懂，會跳過：' + esc(bad.slice(0, 10).join('、')) + (bad.length > 10 ? '…' : '') + '</p>' : '') +
    '<p class="small">已經在白名單裡的車牌會更新有效期間。⚠️ 匯入後這些車會自動開閘。</p>' +
    '<div class="row"><button id="wimpgo">確定匯入</button><button class="ghost" id="wimpno">取消</button></div><div class="msg" id="wimpmsg"></div></div>';
  $('wimpno').addEventListener('click', () => box.innerHTML = '');
  $('wimpgo').addEventListener('click', async () => {
    busy($('wimpgo'), true); $('wimpno').disabled = true;
    const fails = [];
    let done = 0;
    for (let i = 0; i < rows.length; i += 50) {  // 一次 50 筆，確保在 45 秒內做完
      if (!$('wimpmsg')) return;
      say($('wimpmsg'), '匯入中… ' + done + ' / ' + rows.length, true);
      const d = await cmd('wl_batch', {items: rows.slice(i, i + 50)});
      if (!d.ok) { fails.push('第 ' + (i + 1) + '～' + Math.min(i + 50, rows.length) + ' 筆：' + d.error); continue; }
      d.data.report.forEach(r => { if (r.ok) done++; else fails.push(r.plate + '：' + r.error); });
    }
    if (!$('wimport')) return;
    $('wimport').innerHTML = '<div class="notice"><b>匯入完成：成功 ' + done + ' 筆' + (fails.length ? '，失敗 ' + fails.length + ' 筆' : '') + '</b>' +
      (fails.length ? '<p class="small bad">' + esc(fails.slice(0, 20).join('；')) + '</p>' : '') + '</div>';
    reload();
  });
}

// 照片
function tabPhoto(pane) {
  const t = isoDate(new Date());
  pane.innerHTML = '<div class="form"><div><label for="pplate">車牌（可不填）</label><input id="pplate"></div>' +
    '<div><label for="pstart">從</label><input id="pstart" type="datetime-local" value="' + t + 'T00:00"></div>' +
    '<div><label for="pend">到</label><input id="pend" type="datetime-local" value="' + t + 'T23:59"></div><div><button id="pq">查詢</button></div></div>' +
    '<p class="small muted">照片是個資，查看會留紀錄。</p><div class="msg" id="pmsg"></div><div id="plist"></div><img id="pimg" class="photo-big hidden" alt="車牌照片">';
  $('pq').addEventListener('click', async () => {
    busy($('pq'), true); say($('pmsg'), '查詢中…', true); $('plist').innerHTML = ''; $('pimg').classList.add('hidden');
    const d = await cmd('photo_query', {plate: $('pplate').value.trim(), start: $('pstart').value, end: $('pend').value});
    if (!$('pq')) return;
    busy($('pq'), false);
    if (!d.ok) return say($('pmsg'), d.error);
    const n = d.data.days.reduce((s, x) => s + x.paths.length, 0);
    say($('pmsg'), '找到 ' + n + ' 張' + (n > 200 ? '（只列出最新 200 張）' : ''), true);
    const all = d.data.days.flatMap(x => x.paths).reverse().slice(0, 200);
    $('plist').innerHTML = '<div class="photos">' + all.map(p => {
      const m = p.match(/\/(\d{6})_\d+_full_([^/]*)\.jpg$/);
      const label = m ? m[1].replace(/(\d\d)(\d\d)(\d\d)/, '$1:$2:$3') + ' ' + m[2] : p.split('/').pop();
      return '<button class="ghost sm" data-path="' + esc(p) + '">' + esc(label) + '</button>';
    }).join('') + '</div>';
  });
  $('plist').addEventListener('click', async e => {
    const p = e.target.dataset.path; if (!p) return;
    e.target.disabled = true;
    const d = await cmd('photo_get', {path: p});
    if (!$('pimg')) return;
    e.target.disabled = false;
    if (!d.ok) return say($('pmsg'), d.error);
    $('pimg').src = 'data:image/jpeg;base64,' + d.data.jpeg_b64; $('pimg').classList.remove('hidden');
  });
}

// 參數
function tabParam(pane) {
  pane.innerHTML = '<p class="muted">讀取攝影機參數中…</p>';
  const render = d => {
    const edit = can('param.edit');
    const editable = g => edit && (g.key !== 'black_whitelist' || can('gate.rule'));  // 開閘規則只有主控能改
    pane.innerHTML = '<div class="msg" id="amsg"></div>' + d.groups.map(g => '<div class="card" data-group="' + esc(g.key) + '"><h3>' + esc(g.title) + '</h3>' +
      (g.warn ? '<p class="small warn">⚠️ ' + esc(g.warn) + '</p>' : '') + '<div class="param">' + g.fields.map(f => {
        const id = 'f_' + g.key + '_' + f.name, dis = editable(g) && !f.readonly ? '' : ' disabled';
        let input;
        if (f.type === 'select') input = '<select id="' + esc(id) + '" data-name="' + esc(f.name) + '" data-type="int"' + dis + '>' +
          (f.options.some(o => o[0] === f.value) ? '' : '<option value="">（目前值 ' + esc(f.value) + '）</option>') +
          f.options.map(o => '<option value="' + esc(o[0]) + '"' + (o[0] === f.value ? ' selected' : '') + '>' + esc(o[1]) + '</option>').join('') + '</select>';
        else if (f.type === 'int') input = '<input id="' + esc(id) + '" type="number" min="' + esc(f.min) + '" max="' + esc(f.max) + '" value="' + esc(f.value) + '" data-name="' + esc(f.name) + '" data-type="int"' + dis + '>';
        else input = '<input id="' + esc(id) + '" value="' + esc(f.value) + '" data-name="' + esc(f.name) + '" data-type="text"' + dis + '>';
        return '<div><label for="' + esc(id) + '">' + esc(f.label) + (f.type === 'int' ? '（' + esc(f.min) + '～' + esc(f.max) + '）' : '') + (f.readonly ? '（只能在現場改）' : '') + '</label>' + input + '</div>';
      }).join('') + '</div>' + (editable(g) ? '<button class="sm" data-save="' + esc(g.key) + '">儲存「' + esc(g.title) + '」</button>' :
        (edit ? '<p class="small muted">開閘規則只有主控公司能改。</p>' : '')) + '</div>').join('') +
      '<div class="card"><h3>攝影機資訊（只能看）</h3>' + d.info.map(i => '<div class="small"><span class="muted">' + esc(i.label) + '：</span>' + esc(i.value) + '</div>').join('') + '</div>' +
      (edit ? '' : '<p class="small muted">你只有「看參數」權限，不能修改。</p>');
    const original = {};
    d.groups.forEach(g => g.fields.forEach(f => original[g.key + '.' + f.name] = f.value));
    pane.querySelectorAll('[data-save]').forEach(b => b.addEventListener('click', async () => {
      const key = b.dataset.save, values = {};
      pane.querySelectorAll('[data-group="' + key + '"] [data-name]').forEach(el => {
        if (el.value === '' || el.disabled) return;
        const v = el.dataset.type === 'int' ? Number(el.value) : el.value;
        if (v !== original[key + '.' + el.dataset.name]) values[el.dataset.name] = v;
      });
      if (!Object.keys(values).length) return say($('amsg'), '沒有改任何欄位', true);
      if (key === 'black_whitelist' && !confirm('開閘規則會影響閘門開不開，確定要改嗎？')) return;
      busy(b, true);
      const r = await cmd('param_set', {key, values});
      busy(b, false);
      if (!r.ok) return say($('amsg'), r.error);
      render(r.data);
      say($('amsg'), '已存到攝影機：' + (r.data.changes.join('；') || '沒有變更'), true);
    }));
  };
  cmd('param_get').then(d => d.ok ? render(d.data) : pane.innerHTML = '<p class="bad">' + esc(d.error) + '</p>');
}

// 字幕機
const COLORS = ['紅', '綠', '黃'], MODES = {0: '離線時才顯示', 1: '一律顯示', 2: '不使用'};
const IDLE = [['time', '時間'], ['date', '日期'], ['idle', '自訂文字']], BUSY = [['plate', '車牌'], ['type', '車輛類型'], ['days', '月租剩餘天數'], ['busy', '自訂文字']];
function tabLed(pane) {
  pane.innerHTML = '<p class="muted">讀取字幕機設定中…</p>';
  const dis = can('led.edit') ? '' : ' disabled';
  const colorSel = (v, cls, i) => '<select class="' + cls + '" data-i="' + i + '"' + dis + '>' + COLORS.map((c, k) => '<option value="' + k + '"' + (k === v ? ' selected' : '') + '>' + c + '</option>').join('') + '</select>';
  const rows = (led, kind, flags) => led.lines.map((l, i) => '<div class="ledline"><span class="ln small muted">第' + (i + 1) + '行</span><div class="chk">' +
    flags.map(([f, n]) => '<label><input type="checkbox" class="' + kind + '" data-i="' + i + '" data-f="' + f + '"' + (l[kind][f] ? ' checked' : '') + dis + '>' + n + '</label>').join('') +
    '</div><input class="' + kind + '_text" data-i="' + i + '" value="' + esc(l[kind + '_text']) + '" placeholder="自訂文字"' + dis + '>' + colorSel(l[kind + '_color'], kind + '_color', i) + '</div>').join('');
  const render = led => {
    pane.innerHTML = '<p class="small warn">⚠️ 現場字幕機目前還不會跟著攝影機動（硬體／協定還在查），這裡改的是攝影機裡的設定。</p>' +
      '<div class="opt"><label>顯示方式 <select id="ledmode"' + dis + '>' + Object.entries(MODES).map(([k, n]) => '<option value="' + k + '"' + (+k === led.mode ? ' selected' : '') + '>' + n + '</option>').join('') + '</select></label>' +
      '<span class="small muted">協定 ' + esc(led.protocol) + '</span></div><h3>沒有車的時候</h3>' + rows(led, 'idle', IDLE) + '<h3>車子來的時候</h3>' + rows(led, 'busy', BUSY) +
      '<h3>語音</h3><div class="opt"><label><input type="checkbox" id="vplate"' + (led.voice_plate ? ' checked' : '') + dis + '>播報車牌</label>' +
      '<label><input type="checkbox" id="vwel"' + (led.voice_welcome_on ? ' checked' : '') + dis + '>歡迎語</label><select id="vweltext" class="auto"' + dis + '>' +
      [...new Set([led.voice_welcome, '歡迎光臨', '歡迎回家'])].map(w => '<option' + (w === led.voice_welcome ? ' selected' : '') + '>' + esc(w) + '</option>').join('') + '</select></div>' +
      (can('led.edit') ? '<div class="row mt"><button id="ledsave">儲存到攝影機</button></div>' : '<p class="small muted">你沒有「改字幕機」權限，只能看。</p>') + '<div class="msg" id="ledmsg"></div>';
    if (!$('ledsave')) return;
    $('ledsave').addEventListener('click', async () => {
      const v = JSON.parse(JSON.stringify(led)), q = s => pane.querySelectorAll(s);
      v.mode = +$('ledmode').value; v.voice_plate = $('vplate').checked; v.voice_welcome_on = $('vwel').checked; v.voice_welcome = $('vweltext').value;
      for (const k of ['idle', 'busy']) {
        q('input.' + k).forEach(e => v.lines[e.dataset.i][k][e.dataset.f] = e.checked);
        q('.' + k + '_text').forEach(e => v.lines[e.dataset.i][k + '_text'] = e.value);
        q('.' + k + '_color').forEach(e => v.lines[e.dataset.i][k + '_color'] = +e.value);
      }
      busy($('ledsave'), true);
      const d = await cmd('led_set', {led: v});
      if (!d.ok) { busy($('ledsave'), false); return say($('ledmsg'), d.error); }
      render(d.data.led); say($('ledmsg'), '已存到攝影機', true);
    });
  };
  cmd('led_get').then(d => d.ok ? render(d.data.led) : pane.innerHTML = '<p class="bad">' + esc(d.error) + '</p>');
}

// 維護
function tabMaint(pane) {
  pane.innerHTML = '<div id="rinfo" class="muted">讀取重開機排程中…</div>' +
    (can('camera.reboot') ? '<div class="row mt"><button class="danger" id="reboot">攝影機重開機</button><span class="small muted">重開約 20 秒，這段時間認不到車牌</span></div>' : '') + '<div class="msg" id="rmsg"></div>';
  cmd('restart_info').then(d => {
    if (!$('rinfo')) return;
    if (!d.ok) return $('rinfo').innerHTML = '<span class="bad">' + esc(d.error) + '</span>';
    const r = d.data.restart;
    $('rinfo').textContent = r.auto ? '自動重開機：每 ' + r.every_days + ' 天 ' + r.time : '自動重開機：沒有開';
  });
  if ($('reboot')) $('reboot').addEventListener('click', async () => {
    if (!confirm('確定要重開「' + cur.cam.name + '」嗎？重開期間約 20 秒認不到車牌。')) return;
    busy($('reboot'), true);
    const d = await cmd('reboot');
    say($('rmsg'), d.ok ? '已送出重開機，約 20 秒後恢復' : d.error, d.ok);
    setTimeout(() => $('reboot') && busy($('reboot'), false), 60000);
  });
}

// ---------- 進出紀錄 ----------
async function viewEvents(siteId) {
  const sd = await api('sites');
  if (!sd.ok) return main().innerHTML = '<p class="bad">' + esc(sd.error) + '</p>';
  main().innerHTML = '<h1>進出紀錄</h1><div class="card"><div class="form">' +
    '<div><label for="esite">場站</label><select id="esite"><option value="">全部場站</option>' +
    sd.sites.map(x => '<option value="' + x.id + '"' + (x.id === siteId ? ' selected' : '') + '>' + esc(x.name) + (me.is_root ? '（' + esc(x.company_name) + '）' : '') + '</option>').join('') + '</select></div>' +
    '<div><label for="edate">日期</label><input id="edate" type="date" value="' + isoDate(new Date()) + '"></div>' +
    '<div><label for="eplate">車牌（可只打一部分）</label><input id="eplate" autocomplete="off"></div>' +
    '<div class="radio"><label><input type="checkbox" id="eabn">只看非白名單</label></div><div><button id="eq">查詢</button></div></div>' +
    '<p class="small muted">白名單車不存照片；非白名單車才存照片。查詢與看照片都會留紀錄。</p></div>' +
    '<div class="msg" id="emsg"></div><div class="card scroll"><table><thead><tr><th>時間</th><th>場站／攝影機</th><th>車牌</th><th>進出</th><th>白名單</th><th></th></tr></thead><tbody id="elist"></tbody></table></div>' +
    '<img id="eimg" class="photo-big hidden" alt="進出照片">';
  const load = async () => {
    busy($('eq'), true);
    const d = await api('events', {site_id: $('esite').value ? +$('esite').value : null, date: $('edate').value, plate: $('eplate').value.trim(), abnormal_only: $('eabn').checked});
    if (!$('elist')) return;
    busy($('eq'), false);
    if (!d.ok) return say($('emsg'), d.error);
    say($('emsg'), d.date + ' 共 ' + d.rows.length + ' 筆' + (d.rows.length >= 500 ? '（只列最新 500 筆，請縮小範圍）' : ''), true);
    $('elist').innerHTML = d.rows.map(r => '<tr><td>' + esc(r.ts.slice(11)) + '</td><td>' + esc(r.site_name) + '<div class="small muted">' + esc(r.camera_name) + '</div></td><td><b>' + esc(r.plate) + '</b></td><td>' +
      (r.direction === 'out' ? '出' : '進') + '</td><td class="' + (r.is_whitelist ? 'ok' : 'warn') + '">' + (r.is_whitelist ? '是' : '否') + '</td><td>' +
      (r.has_photo && can('photo.view') ? '<button class="ghost sm" data-eid="' + r.id + '">照片</button>' : '') + '</td></tr>').join('') || '<tr><td colspan="6" class="muted">這一天沒有紀錄</td></tr>';
  };
  $('eq').addEventListener('click', load);
  $('elist').addEventListener('click', async e => {
    const id = e.target.dataset.eid; if (!id) return;
    e.target.disabled = true;
    const d = await api('event_photo', {id: +id});
    if (!$('eimg')) return;
    e.target.disabled = false;
    if (!d.ok) return say($('emsg'), d.error);
    $('eimg').src = 'data:image/jpeg;base64,' + d.jpeg_b64; $('eimg').classList.remove('hidden'); $('eimg').scrollIntoView({behavior: 'smooth'});
  });
  load();
}

// ---------- 客戶公司 ----------
async function viewCompanies() {
  const d = await api('companies');
  if (!d.ok) return main().innerHTML = '<p class="bad">' + esc(d.error) + '</p>';
  main().innerHTML = '<h1>客戶公司</h1><div class="card scroll"><table><thead><tr><th>公司</th><th>場站</th><th>帳號</th><th>狀態</th><th></th></tr></thead><tbody>' +
    d.companies.map(c => '<tr><td>' + esc(c.name) + (c.is_root ? ' <span class="tag">主控</span>' : '') + '<div class="small muted">建立 ' + esc(c.created) + '</div></td><td>' + c.sites + '</td><td>' + c.users + '</td><td class="' + (c.active ? 'ok' : 'bad') + '">' + (c.active ? '使用中' : '已停用') + '</td><td>' +
      (c.is_root ? '' : '<button class="ghost sm" data-cid="' + c.id + '" data-active="' + (c.active ? 0 : 1) + '">' + (c.active ? '停用' : '啟用') + '</button>') + '</td></tr>').join('') +
    '</tbody></table></div><h2>新增客戶公司</h2><div class="card"><p class="small muted">會同時開好這家公司的第一個管理員帳號，並產生臨時密碼（第一次登入要改）。</p><div class="form">' +
    '<div><label for="cname">公司名稱</label><input id="cname"></div><div><label for="cuser">管理員帳號（英數）</label><input id="cuser" autocomplete="off"></div>' +
    '<div><label for="cdisp">管理員名稱</label><input id="cdisp" placeholder="例如 王經理"></div><div><button id="cadd">新增</button></div></div><div class="msg" id="cmsg"></div><div id="cres"></div></div>';
  main().querySelectorAll('[data-cid]').forEach(b => b.addEventListener('click', async () => {
    const on = b.dataset.active === '1';
    if (!on && !confirm('停用後，這家公司的所有帳號都不能登入、小主機也會被拒絕連線。確定嗎？')) return;
    const r = await api('company_active', {id: +b.dataset.cid, active: on});
    if (!r.ok) return alert(r.error);
    viewCompanies();
  }));
  $('cadd').addEventListener('click', async () => {
    busy($('cadd'), true);
    const r = await api('company_add', {name: $('cname').value, admin_username: $('cuser').value, admin_name: $('cdisp').value});
    busy($('cadd'), false);
    if (!r.ok) return say($('cmsg'), r.error);
    $('cmsg').textContent = '';
    $('cres').innerHTML = tempPasswordBox(r.username, r.temp_password);
    $('cname').value = $('cuser').value = $('cdisp').value = '';
    setTimeout(viewCompaniesTable, 0);
  });
}
async function viewCompaniesTable() {
  const keep = $('cres') ? $('cres').innerHTML : '';
  await viewCompanies();
  if (keep) $('cres').innerHTML = keep;
}
function tempPasswordBox(username, pw) {
  return '<div class="notice"><b>帳號已建立（臨時密碼只顯示這一次）</b><div class="mt">帳號：<span class="secret">' + esc(username) + '</span></div>' +
    '<div class="mt">臨時密碼：<span class="secret">' + esc(pw) + '</span></div><p class="small">請用安全的方式交給對方，對方第一次登入時會被要求改密碼。</p></div>';
}

// ---------- 帳號 ----------
async function viewUsers() {
  const picker = await companyPicker();
  const d = await api('users', {company_id: cid()});
  if (!d.ok) return main().innerHTML = '<p class="bad">' + esc(d.error) + '</p>';
  const siteName = id => (d.sites.find(s => s.id === id) || {}).name || '#' + id;
  main().innerHTML = '<h1>帳號</h1>' + picker + '<div class="card scroll"><table><thead><tr><th>帳號</th><th>名稱</th><th>權限群組</th><th>可管場站</th><th>狀態</th><th></th></tr></thead><tbody>' +
    d.users.map(u => '<tr><td>' + esc(u.username) + '</td><td>' + esc(u.display_name) + '</td><td>' + esc(u.role_name) + '</td><td>' + (u.site_ids ? u.site_ids.map(i => esc(siteName(i))).join('、') : '全部') +
      '</td><td class="' + (u.active ? 'ok' : 'bad') + '">' + (u.active ? '啟用' : '停用') + (u.must_change_pw ? '<div class="small warn">還沒改臨時密碼</div>' : '') + '</td><td class="row">' +
      '<button class="ghost sm" data-edit="' + u.id + '">修改</button>' + (u.id === me.id ? '' : '<button class="ghost sm" data-pw="' + u.id + '">重設密碼</button>') + '</td></tr>').join('') +
    '</tbody></table></div><div class="msg" id="umsg"></div><div id="ures"></div><h2 id="uformtitle">新增帳號</h2><div class="card" id="uform"></div>';
  bindPicker(viewUsers);
  const form = u => {
    const sel = u && u.site_ids;
    $('uformtitle').textContent = u ? '修改帳號：' + u.username : '新增帳號';
    $('uform').innerHTML = '<div class="form">' + (u ? '' : '<div><label for="fuser">帳號（英數，3～32 字）</label><input id="fuser" autocomplete="off"></div>') +
      '<div><label for="fdisp">名稱</label><input id="fdisp" value="' + esc(u ? u.display_name : '') + '"></div>' +
      '<div><label for="frole">權限群組</label><select id="frole">' + d.roles.map(r => '<option value="' + r.id + '"' + (u && u.role_id === r.id ? ' selected' : '') + '>' + esc(r.name) + '</option>').join('') + '</select></div>' +
      '<div><label for="fact">狀態</label><select id="fact"><option value="1">啟用</option><option value="0"' + (u && !u.active ? ' selected' : '') + '>停用</option></select></div></div>' +
      '<h3>可以管理的場站</h3><div class="radio"><label><input type="radio" name="fscope" value="all"' + (sel ? '' : ' checked') + '>全部場站（含之後新增的）</label>' +
      '<label><input type="radio" name="fscope" value="some"' + (sel ? ' checked' : '') + '>只能管指定的場站</label></div>' +
      '<div class="perms mt" id="fsites">' + (d.sites.map(s => '<label><input type="checkbox" value="' + s.id + '"' + (sel && sel.includes(s.id) ? ' checked' : '') + '>' + esc(s.name) + '</label>').join('') || '<span class="muted small">這家公司還沒有場站</span>') + '</div>' +
      '<div class="row mt"><button id="fsave">' + (u ? '儲存' : '新增帳號') + '</button>' + (u ? '<button class="ghost" id="fcancel">取消</button>' : '') + '</div><div class="msg" id="fmsg"></div>';
    if ($('fcancel')) $('fcancel').addEventListener('click', () => form(null));
    $('fsave').addEventListener('click', async () => {
      const some = document.querySelector('input[name=fscope]:checked').value === 'some';
      const body = {company_id: cid(), display_name: $('fdisp').value, role_id: +$('frole').value, active: $('fact').value === '1',
        site_ids: some ? [...$('fsites').querySelectorAll('input:checked')].map(e => +e.value) : null};
      if (u) body.id = u.id; else body.username = $('fuser').value.trim();
      busy($('fsave'), true);
      const r = await api('user_save', body);
      busy($('fsave'), false);
      if (!r.ok) return say($('fmsg'), r.error);
      await viewUsers();
      if (r.temp_password) $('ures').innerHTML = tempPasswordBox(r.username, r.temp_password);
      else say($('umsg'), '已儲存', true);
    });
  };
  form(null);
  main().querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => { form(d.users.find(u => u.id === +b.dataset.edit)); $('uformtitle').scrollIntoView({behavior: 'smooth'}); }));
  main().querySelectorAll('[data-pw]').forEach(b => b.addEventListener('click', async () => {
    const u = d.users.find(x => x.id === +b.dataset.pw);
    if (!confirm('重設 ' + u.username + ' 的密碼？對方目前的登入會被登出。')) return;
    const r = await api('user_reset_password', {company_id: cid(), id: u.id});
    if (!r.ok) return say($('umsg'), r.error);
    $('ures').innerHTML = tempPasswordBox(r.username, r.temp_password);
  }));
}

// ---------- 權限群組 ----------
async function viewRoles() {
  const picker = await companyPicker();
  const d = await api('roles', {company_id: cid()});
  if (!d.ok) return main().innerHTML = '<p class="bad">' + esc(d.error) + '</p>';
  const defs = permDefs.filter(p => d.company.is_root || p.key !== 'company.manage');
  const label = k => (permDefs.find(p => p.key === k) || {label: k}).label;
  main().innerHTML = '<h1>權限群組</h1>' + picker + '<p class="muted small">先在這裡定義群組能做哪些事，再到「帳號」把人放進群組。你只能給別人你自己有的權限。</p>' +
    '<div class="card scroll"><table><thead><tr><th>群組</th><th>權限</th><th>帳號數</th><th></th></tr></thead><tbody>' +
    d.roles.map(r => '<tr><td>' + esc(r.name) + (r.is_system ? ' <span class="tag">內建</span>' : '') + '</td><td>' + r.perms.map(p => '<span class="tag">' + esc(label(p)) + '</span>').join('') + '</td><td>' + r.users + '</td><td class="row">' +
      (r.is_system ? '<span class="small muted">不能修改</span>' : '<button class="ghost sm" data-redit="' + r.id + '">修改</button><button class="danger sm" data-rdel="' + r.id + '">刪除</button>') + '</td></tr>').join('') +
    '</tbody></table></div><div class="msg" id="rmsg"></div><h2 id="rformtitle">新增權限群組</h2><div class="card" id="rform"></div>';
  bindPicker(viewRoles);
  const form = r => {
    $('rformtitle').textContent = r ? '修改權限群組：' + r.name : '新增權限群組';
    $('rform').innerHTML = '<div class="form"><div><label for="rname">群組名稱</label><input id="rname" value="' + esc(r ? r.name : '') + '" placeholder="例如 社區警衛"></div></div><h3>可以做的事</h3><div class="perms">' +
      defs.map(p => '<label><input type="checkbox" value="' + esc(p.key) + '"' + ((r ? r.perms.includes(p.key) : p.key === 'site.view') ? ' checked' : '') + (can(p.key) ? '' : ' disabled') + (p.key === 'site.view' ? ' disabled checked' : '') +
        '><span>' + esc(p.label) + '<br><span class="small muted">' + esc(p.desc) + (can(p.key) ? '' : '（你沒有這個權限，不能給）') + '</span></span></label>').join('') + '</div>' +
      '<div class="row mt"><button id="rsave">' + (r ? '儲存' : '新增群組') + '</button>' + (r ? '<button class="ghost" id="rcancel">取消</button>' : '') + '</div><div class="msg" id="rfmsg"></div>';
    if ($('rcancel')) $('rcancel').addEventListener('click', () => form(null));
    $('rsave').addEventListener('click', async () => {
      const perms = [...$('rform').querySelectorAll('.perms input:checked')].map(e => e.value);
      const body = {company_id: cid(), name: $('rname').value, perms};
      if (r) body.id = r.id;
      const res = await api('role_save', body);
      if (!res.ok) return say($('rfmsg'), res.error);
      await viewRoles(); say($('rmsg'), '已儲存', true);
    });
  };
  form(null);
  main().querySelectorAll('[data-redit]').forEach(b => b.addEventListener('click', () => { form(d.roles.find(r => r.id === +b.dataset.redit)); $('rformtitle').scrollIntoView({behavior: 'smooth'}); }));
  main().querySelectorAll('[data-rdel]').forEach(b => b.addEventListener('click', async () => {
    if (!confirm('確定刪除這個權限群組？')) return;
    const r = await api('role_delete', {company_id: cid(), id: +b.dataset.rdel});
    if (!r.ok) return say($('rmsg'), r.error);
    viewRoles();
  }));
}

// ---------- 操作紀錄 ----------
async function viewAudit() {
  const d = await api('audit', {limit: 300});
  if (!d.ok) return main().innerHTML = '<p class="bad">' + esc(d.error) + '</p>';
  main().innerHTML = '<h1>操作紀錄</h1><p class="muted small">最新 300 筆' + (me.is_root ? '（主控看得到全部公司）' : '') + '</p><div class="card scroll"><table><thead><tr><th>時間</th>' + (me.is_root ? '<th>公司</th>' : '') +
    '<th>帳號</th><th>動作</th><th>對象</th><th>內容</th><th>結果</th></tr></thead><tbody>' +
    (d.rows.map(r => '<tr><td class="small">' + esc(r.ts) + '</td>' + (me.is_root ? '<td class="small">' + esc(r.company_name || '-') + '</td>' : '') + '<td>' + esc(r.username) + '<div class="small muted">' + esc(r.ip) + '</div></td><td>' + esc(r.action) +
      '</td><td class="small">' + esc(r.target) + '</td><td class="small">' + esc(r.detail) + '</td><td class="small ' + (r.result === '成功' ? 'ok' : 'bad') + '">' + esc(r.result) + '</td></tr>').join('') ||
      '<tr><td colspan="7" class="muted">還沒有紀錄</td></tr>') + '</tbody></table></div>';
}

// ---------- 我的帳號 ----------
function viewAccount() {
  main().innerHTML = '<h1>我的帳號</h1>' + (me.must_change_pw ? '<div class="notice warnbox">你正在用臨時密碼，請先設定自己的密碼才能使用其他功能。</div>' : '') +
    '<div class="card"><div>帳號：' + esc(me.username) + '</div><div>名稱：' + esc(me.display_name) + '</div><div>公司：' + esc(me.company_name) + '</div><div>權限群組：' + esc(me.role_name) + '</div></div>' +
    '<h2>修改密碼</h2><div class="card"><div class="form"><div><label for="pold">目前的密碼</label><input id="pold" type="password" autocomplete="current-password"></div>' +
    '<div><label for="pnew">新密碼（至少 10 個字）</label><input id="pnew" type="password" autocomplete="new-password"></div>' +
    '<div><label for="pnew2">再輸入一次新密碼</label><input id="pnew2" type="password" autocomplete="new-password"></div><div><button id="psave">修改密碼</button></div></div><div class="msg" id="pmsg"></div></div>';
  $('psave').addEventListener('click', async () => {
    if ($('pnew').value !== $('pnew2').value) return say($('pmsg'), '兩次輸入的新密碼不一樣');
    busy($('psave'), true);
    const r = await api('change_password', {old: $('pold').value, new: $('pnew').value});
    busy($('psave'), false);
    $('pold').value = $('pnew').value = $('pnew2').value = '';
    if (!r.ok) return say($('pmsg'), r.error);
    say($('pmsg'), '密碼已修改', true);
    if (me.must_change_pw) { me.must_change_pw = false; setTimeout(() => { location.hash = '#/dashboard'; }, 800); }
  });
}

start();
