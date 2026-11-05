const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const res = await fetch(path, { credentials: "same-origin", headers, ...opts });
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) {
    const detail = data && data.detail ? String(data.detail) : `状态码 ${res.status}`;
    toast(`请求失败：${detail}`);
    throw new Error(detail);
  }
  return data;
}

function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._t);
  t._t = setTimeout(() => (t.hidden = true), ms);
}

function badge(status) {
  const map = { success: "成功", partial: "部分成功", error: "失败", running: "运行中" };
  return `<span class="badge ${status}">${map[status] || status}</span>`;
}

// 时间展示：后端返回的已是东八区时间（如 2026-08-19T07:59:00），
// 这里只做字符串美化（T -> 空格、去毫秒），不做时区换算，避免重复加偏移。
function fmtTime(s) {
  if (!s) return "";
  return String(s).replace("T", " ").slice(0, 19);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 头像：有 URL 显示图片（加载失败自动回落到首字母），无 URL 直接显示首字母
function avatarHtml(url, name) {
  // 按 Unicode 码点截取首字符，避免 emoji/特殊符号被切出乱码
  const letter = escapeHtml(Array.from(String(name || "?"))[0] ?? "?").toUpperCase();
  const fb = `<span class="avatar-fallback">${letter}</span>`;
  if (!url) return `<span class="avatar-wrap">${fb}</span>`;
  return `<span class="avatar-wrap">${fb}<img class="avatar" src="${url}" alt="" referrerpolicy="no-referrer" onerror="this.remove()"></span>`;
}

async function refresh() {
  const st = await api("/api/accounts/status");
  if (!st) return;
  renderAccount("china", st.china);
  renderAccount("intl", st.intl);
  const settings = await api("/api/settings");
  if (settings) renderSettings(settings);
  const sync = await api("/api/sync/status");
  if (sync) renderSync(sync);
  const log = await api("/api/sync/log?limit=20");
  if (log) renderLogList($("sync-log"), log, syncSummary);
  const xm = await api("/api/xiaomi/status");
  if (xm) renderXiaomi(xm);
  const wset = await api("/api/weight/settings");
  if (wset) {
    $("weight-enabled").checked = !!wset.weight_enabled;
    fillSelect($("weight-window"), wset.window_options, wset.weight_sync_window);
  }
  const wsync = await api("/api/weight/sync/status");
  if (wsync) renderWeightSync(wsync);
  const wlog = await api("/api/weight/sync/log?limit=20");
  if (wlog) renderLogList($("weight-log"), wlog, weightSummary);
  const hset = await api("/api/health/settings");
  if (hset) {
    $("health-enabled").checked = !!hset.health_enabled;
    fillSelect($("health-window"), hset.window_options, hset.health_sync_window);
  }
  const hsync = await api("/api/health/sync/status");
  if (hsync) renderHealthSync(hsync);
  const hlog = await api("/api/health/sync/log?limit=20");
  if (hlog) renderLogList($("health-log"), hlog, healthSummary);
  const igp = await api("/api/igpsport/status");
  if (igp) renderIgpsport(igp);
}

// 登录卡片状态渲染（佳明两区 / 小米 / 迹驰 共用）
function renderLoginCard({ statusEl, formEl, logoutEl, mfaEl, loggedIn, name, avatarUrl }) {
  if (loggedIn) {
    statusEl.className = "status ok";
    statusEl.innerHTML = `${avatarHtml(avatarUrl, name)}已登录：${escapeHtml(name)}`;
    formEl.hidden = true;
    logoutEl.hidden = false;
  } else {
    statusEl.className = "status no";
    statusEl.textContent = "未登录";
    formEl.hidden = false;
    logoutEl.hidden = true;
  }
  if (mfaEl) mfaEl.hidden = true;
}

function renderAccount(region, info) {
  renderLoginCard({
    statusEl: $(`${region}-status`),
    formEl: $(`${region}-form`),
    logoutEl: $(`${region}-logout-btn`),
    mfaEl: $(`${region}-mfa`),
    loggedIn: info.logged_in,
    name: info.username || info.email || "",
    avatarUrl: info.avatar_url,
  });
}

const WINDOW_LABELS = { "1day": "最近一天", "1week": "最近一周", "1month": "最近一个月", "3months": "最近三个月" };

// 下拉框：首次填充选项，之后仅更新选中值
function fillSelect(sel, options, current) {
  if (!sel.options.length) {
    (options || []).forEach((k) => {
      const o = document.createElement("option");
      o.value = k;
      o.textContent = WINDOW_LABELS[k] || k;
      sel.appendChild(o);
    });
  }
  if (current) sel.value = current;
}

function renderSettings(s) {
  const sel = $("freq");
  if (!sel.options.length) {
    s.options.forEach((m) => {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = m + " 分钟";
      sel.appendChild(o);
    });
  }
  sel.value = String(s.frequency_minutes);
  fillSelect($("window"), s.window_options, s.sync_window || "3months");
  $("enabled").checked = s.enabled;
}

// 活动同步摘要：读取 N 条 ｜ 国际区 历史 H 成功 S 失败 F ｜ 迹驰 已传 N 历史 H 成功 S 失败 F
function syncSummary(r) {
  const read = r.read_count != null ? r.read_count : (r.fetched_count || 0);
  const parts = [`读取 ${read} 条`];
  parts.push(`国际区 历史 ${r.intl_history_count || 0} 成功 ${r.intl_synced || 0} 失败 ${r.intl_failed || 0}`);
  const igp = [`历史 ${r.igp_history_count || 0}`, `成功 ${r.igp_synced || 0}`, `失败 ${r.igp_failed || 0}`];
  if (r.igp_done) igp.unshift(`已传 ${r.igp_done}`);
  parts.push(`迹驰 ${igp.join(" ")}`);
  return parts.join(" ｜ ");
}

function renderSync(s) {
  const el = $("sync-status");
  let txt = `启用：${s.enabled ? "是" : "否"} ｜ 频率：${s.frequency_minutes} 分钟`;
  if (s.last_sync) {
    const ls = s.last_sync;
    txt += `<br>上次：${fmtTime(ls.finished_at || ls.started_at)} ${badge(ls.status)}`;
    txt += `<br>${syncSummary(ls)}`;
    if (ls.error) txt += `<br><span class="err">原因：${ls.error}</span>`;
  }
  if (s.next_run_at) txt += `<br>下次：约 ${fmtTime(s.next_run_at)}`;
  el.innerHTML = txt;
}

// 日志列表：三类同步共用，summaryFn 生成每行摘要
function renderLogList(el, rows, summaryFn) {
  el.innerHTML = "";
  if (!rows.length) { el.innerHTML = '<li class="muted">暂无记录</li>'; return; }
  rows.forEach((r) => {
    const li = document.createElement("li");
    let detail = `${badge(r.status)} ${summaryFn(r)}`;
    if (r.error) detail += `<br><span class="err">${r.error}</span>`;
    li.innerHTML = `<span>${fmtTime(r.finished_at || r.started_at)}</span><span>${detail}</span>`;
    el.appendChild(li);
  });
}

function showMfa(region, token) {
  $(`${region}-form`).hidden = true;
  $(`${region}-mfa`).hidden = false;
  $(`${region}-mfa`).dataset.token = token;
}

// ---- 小米体脂秤 ----
function renderXiaomi(info) {
  renderLoginCard({
    statusEl: $("xiaomi-status"),
    formEl: $("xiaomi-form"),
    logoutEl: $("xiaomi-logout-btn"),
    loggedIn: info.logged_in,
    name: info.user_id || "",
  });
}

// ---- iGPSPORT 迹驰（同步结果并入活动同步日志，此处仅显示登录状态） ----
function renderIgpsport(info) {
  renderLoginCard({
    statusEl: $("igpsport-status"),
    formEl: $("igpsport-form"),
    logoutEl: $("igpsport-logout-btn"),
    loggedIn: info.logged_in,
    name: info.username || "",
    avatarUrl: info.avatar_url,
  });
  if (info.igp_diag) {
    const el = $("igpsport-status");
    el.innerHTML += `<br><span class="muted">头像诊断：${escapeHtml(info.igp_diag)}</span>`;
  }
}

// ---- 体重同步（固定双区、固定每天 00:00，无需配置目标/频率） ----
// 汇总统计：读取 N 条（无效 M）｜ 国际区 历史 H 成功 X 失败 Y ｜ 中国区 历史 H 成功 A 失败 B
function weightSummary(r) {
  const parts = [];
  const f = r.fetch_failed || 0;
  const h = r.history_count || 0; // 历史（之前已同步过、本次跳过），双区相同
  parts.push(`读取 ${r.fetched_count || 0} 条${f ? `（无效 ${f}）` : ""}`);
  parts.push(`国际区 历史 ${h} 成功 ${r.intl_synced || 0} 失败 ${r.intl_failed || 0}`);
  parts.push(`中国区 历史 ${h} 成功 ${r.china_synced || 0} 失败 ${r.china_failed || 0}`);
  return parts.join(" ｜ ");
}

function renderWeightSync(s) {
  const el = $("weight-sync-status");
  let txt = `上传目标：国际区 + 中国区 ｜ 自动同步：${s.enabled ? "已启用（每天 00:00）" : "已停用"}`;
  if (s.next_run_at) txt += ` ｜ 下次：${fmtTime(s.next_run_at)}`;
  if (s.last_sync) {
    const ls = s.last_sync;
    txt += `<br>上次：${fmtTime(ls.finished_at || ls.started_at)} ${badge(ls.status)}`;
    txt += `<br>${weightSummary(ls)}`;
    if (ls.error) txt += `<br><span class="err">原因：${ls.error}</span>`;
  }
  el.innerHTML = txt;
}

// ---- 模块⑤ 双区全量数据同步（固定每天 03:00，活动 + 健康 互相补全） ----
// 摘要：健康/活动 各自的全量、双向补全与失败数
function healthSummary(r) {
  // 兼容升级前的单向日志（只有 fetched/synced/upload_failed）
  const legacy = !r.read_count && !r.china_synced && !r.intl_synced
    && !r.china_failed && !r.intl_failed && !r.history_count && !r.act_count;
  if (legacy) {
    return `下载 ${r.fetched_count || 0} 个 ｜ 上传成功 ${r.synced_count || 0} 个 ｜ 失败 ${r.upload_failed || 0} 个`;
  }
  const parts = [];
  parts.push(`健康 全量 ${r.read_count || 0} 个 ｜ 补中国区 ${r.china_synced || 0} 失败 ${r.china_failed || 0} ｜ 补国际区 ${r.intl_synced || 0} 失败 ${r.intl_failed || 0}`);
  if (r.act_count || r.act_china_synced || r.act_intl_synced || r.act_failed) {
    parts.push(`活动 全量 ${r.act_count || 0} 条 ｜ 补中国区 ${r.act_china_synced || 0} ｜ 补国际区 ${r.act_intl_synced || 0} ｜ 失败 ${r.act_failed || 0}`);
  }
  parts.push(`已完整 健康 ${r.history_count || 0} / 活动 ${r.act_both || 0}`);
  if (r.intl_history_count) parts.push(`已传过跳过 ${r.intl_history_count}`);
  return parts.join(" ｜ ");
}

function renderHealthSync(s) {
  const el = $("health-sync-status");
  let txt = `同步方式：中国区 ⇄ 国际区 全量数据（活动 + 健康）对比、互相补全 ｜ 自动同步：${s.enabled ? "已启用（每天 03:00）" : "已停用"}`;
  if (s.next_run_at) txt += ` ｜ 下次：${fmtTime(s.next_run_at)}`;
  if (s.last_sync) {
    const ls = s.last_sync;
    txt += `<br>上次：${fmtTime(ls.finished_at || ls.started_at)} ${badge(ls.status)}`;
    txt += `<br>${healthSummary(ls)}`;
    if (ls.error) txt += `<br><span class="err">原因：${ls.error}</span>`;
  }
  el.innerHTML = txt;
}

// ---- 事件绑定 ----
$("china-login-btn").onclick = async () => {
  const btn = $("china-login-btn");
  btn.disabled = true;
  try {
    const r = await api("/api/accounts/china/login", { method: "POST", body: JSON.stringify({ email: $("china-email").value, password: $("china-pass").value }) });
    if (r && r.error) { toast(r.error); return; }
    if (r && r.mfa_required) showMfa("china", r.mfa_token);
    else refresh();
  } catch (e) {}
  finally { btn.disabled = false; }
};
$("china-mfa-btn").onclick = async () => {
  const token = $("china-mfa").dataset.token;
  try {
    const r = await api("/api/accounts/china/mfa", { method: "POST", body: JSON.stringify({ mfa_token: token, code: $("china-code").value }) });
    if (r && r.ok === false) { toast(r.error || "验证失败，请重试"); return; }
    refresh();
  } catch (e) {}
};
$("china-logout-btn").onclick = async () => {
  await api("/api/accounts/logout", { method: "POST", body: JSON.stringify({ region: "china" }) });
  refresh();
};

$("intl-login-btn").onclick = async () => {
  const btn = $("intl-login-btn");
  btn.disabled = true;
  try {
    const r = await api("/api/accounts/intl/login", { method: "POST", body: JSON.stringify({ email: $("intl-email").value, password: $("intl-pass").value }) });
    if (r && r.error) { toast(r.error); return; }
    if (r && r.mfa_required) showMfa("intl", r.mfa_token);
    else refresh();
  } catch (e) {}
  finally { btn.disabled = false; }
};
$("intl-mfa-btn").onclick = async () => {
  const token = $("intl-mfa").dataset.token;
  try {
    const r = await api("/api/accounts/intl/mfa", { method: "POST", body: JSON.stringify({ mfa_token: token, code: $("intl-code").value }) });
    if (r && r.ok === false) { toast(r.error || "验证失败，请重试"); return; }
    refresh();
  } catch (e) {}
};
$("intl-logout-btn").onclick = async () => {
  await api("/api/accounts/logout", { method: "POST", body: JSON.stringify({ region: "intl" }) });
  refresh();
};

$("settings-save").onclick = async () => {
  const msg = $("settings-msg");
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ frequency_minutes: Number($("freq").value), enabled: $("enabled").checked, sync_window: $("window").value }) });
    msg.className = "msg"; msg.textContent = "已保存";
    refresh();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
};

// 手动触发同步后轮询：直到本次 finished_at 变化或超时 60 秒，结果用 toast 反馈
async function waitSyncDone({ statusPath, before, summaryFn, label }) {
  let waited = 0;
  while (waited < 60) {
    await new Promise((r) => setTimeout(r, 1500));
    waited += 1.5;
    const st = await api(statusPath);
    const fin = st?.last_sync?.finished_at;
    if (fin && fin !== before) {
      const s = st.last_sync;
      const sum = summaryFn(s).replace(/ ｜ /g, "，");
      if (s.status === "success") toast(`${label}完成：${sum}`);
      else if (s.status === "partial") toast(`${label}部分完成：${sum}${s.error ? "；" + s.error : ""}`);
      else toast(`${label}失败：${s.error || "未知错误"}`);
      refresh();
      return;
    }
  }
  toast(`${label}等待超时，请稍后查看同步状态`);
  refresh();
}

// 手动触发同步按钮（活动/体重/健康 共用）
function bindSyncNow(btnId, nowPath, statusPath, label, summaryFn) {
  $(btnId).onclick = async () => {
    const before = (await api(statusPath))?.last_sync?.finished_at || null;
    await api(nowPath, { method: "POST" });
    toast(`${label}中…`);
    waitSyncDone({ statusPath, before, summaryFn, label });
  };
}

// 清空日志按钮（三类共用）
function bindLogClear(btnId, path, msg, logEl, summaryFn) {
  $(btnId).onclick = async () => {
    try {
      await api(path, { method: "POST" });
      toast(msg);
      renderLogList(logEl, [], summaryFn);
    } catch (e) {}
  };
}

bindSyncNow("sync-now", "/api/sync/now", "/api/sync/status", "同步", syncSummary);
bindLogClear("sync-log-clear", "/api/sync/log", "活动同步日志已清空", $("sync-log"), syncSummary);

// ---- 小米体脂秤（账号密码登录） ----
$("xiaomi-logout-btn").onclick = async () => {
  await api("/api/xiaomi/logout", { method: "POST" });
  toast("已退出该账户");
  refresh();
};

// 账号密码登录状态机（图形验证码 / 短信邮箱 2FA）
let xiaomiLoginState = { loginId: null, step: null };

function handleXiaomiLoginStep(r) {
  if (!r) return;
  if (r.step === "success") {
    toast("小米账户登录成功");
    refresh();
    return;
  }
  xiaomiLoginState.loginId = r.login_id;
  xiaomiLoginState.step = r.step;
  $("xiaomi-form").hidden = true;
  $("xiaomi-acct-step").hidden = false;
  const img = $("xiaomi-captcha-img");
  if (r.step === "captcha") {
    img.hidden = false;
    img.src = "data:image/png;base64," + (r.captcha_img || "");
    $("xiaomi-step-hint").textContent = "请输入图片中的验证码";
  } else {
    img.hidden = true;
    $("xiaomi-step-hint").textContent = `验证码已发送至 ${r.target || "绑定手机/邮箱"}，请输入收到的验证码`;
  }
  $("xiaomi-step-code").value = "";
  $("xiaomi-step-code").focus();
}

function resetXiaomiAcctForm() {
  xiaomiLoginState = { loginId: null, step: null };
  $("xiaomi-form").hidden = false;
  $("xiaomi-acct-step").hidden = true;
  $("xiaomi-step-code").value = "";
}

$("xiaomi-acct-login-btn").onclick = async () => {
  const btn = $("xiaomi-acct-login-btn");
  const account = $("xiaomi-account").value.trim();
  const password = $("xiaomi-password").value;
  if (!account || !password) { toast("请输入小米账号和密码"); return; }
  btn.disabled = true;
  try {
    const r = await api("/api/xiaomi/login/start", {
      method: "POST",
      body: JSON.stringify({ account, password }),
    });
    handleXiaomiLoginStep(r);
  } catch (e) { resetXiaomiAcctForm(); }
  finally { btn.disabled = false; }
};

$("xiaomi-step-btn").onclick = async () => {
  const btn = $("xiaomi-step-btn");
  const code = $("xiaomi-step-code").value.trim();
  if (!code) { toast("请输入验证码"); return; }
  if (!xiaomiLoginState.loginId) { toast("登录会话已失效，请重新开始"); resetXiaomiAcctForm(); return; }
  btn.disabled = true;
  try {
    const path = xiaomiLoginState.step === "captcha"
      ? "/api/xiaomi/login/captcha"
      : "/api/xiaomi/login/verify";
    const r = await api(path, {
      method: "POST",
      body: JSON.stringify({ login_id: xiaomiLoginState.loginId, code }),
    });
    handleXiaomiLoginStep(r);
  } catch (e) {
    toast("验证码提交失败，请检查后重试");
    resetXiaomiAcctForm();
  }
  finally { btn.disabled = false; }
};

// ---- 体重/健康同步开关与周期下拉（共用保存逻辑） ----
function bindSettingsToggle({ enabledId, windowId, path, field, onEnabled, onDisabled, onWindow }) {
  const save = async (msg) => {
    try {
      await api(path, {
        method: "POST",
        body: JSON.stringify({ [field + "_enabled"]: $(enabledId).checked, [field + "_sync_window"]: $(windowId).value }),
      });
      toast(msg);
      refresh();
    } catch (e) {
      // 保存失败会回填数据库原值（看起来像"选完又跳回去"），
      // 这里提示保留久一点，便于看清真实原因
      toast("保存失败，已恢复为原设置：" + e.message, 6000);
      refresh();
    }
  };
  $(enabledId).onchange = () => save($(enabledId).checked ? onEnabled : onDisabled);
  $(windowId).onchange = () => save(onWindow);
}

// ---- 体重同步开关与触发 ----
bindSettingsToggle({
  enabledId: "weight-enabled",
  windowId: "weight-window",
  path: "/api/weight/settings",
  field: "weight",
  onEnabled: "体重自动同步已开启（每天 00:00）",
  onDisabled: "体重自动同步已关闭",
  onWindow: "体重同步周期已保存",
});
bindSyncNow("weight-sync-now", "/api/weight/sync/now", "/api/weight/sync/status", "体重同步", weightSummary);
bindLogClear("weight-log-clear", "/api/weight/sync/log", "体重同步日志已清空", $("weight-log"), weightSummary);

// ---- 健康数据同步开关与触发 ----
bindSettingsToggle({
  enabledId: "health-enabled",
  windowId: "health-window",
  path: "/api/health/settings",
  field: "health",
  onEnabled: "健康数据自动同步已开启（每天 03:00）",
  onDisabled: "健康数据自动同步已关闭",
  onWindow: "健康数据同步周期已保存",
});
bindSyncNow("health-sync-now", "/api/health/sync/now", "/api/health/sync/status", "健康数据同步", healthSummary);
bindLogClear("health-log-clear", "/api/health/sync/log", "健康数据同步日志已清空", $("health-log"), healthSummary);

// ---- iGPSPORT 迹驰（登录 / 退出 / 开关） ----
$("igpsport-login-btn").onclick = async () => {
  const btn = $("igpsport-login-btn");
  const username = $("igpsport-username").value.trim();
  const password = $("igpsport-password").value;
  if (!username || !password) { toast("请输入 iGPSPORT 账号和密码"); return; }
  btn.disabled = true;
  try {
    const r = await api("/api/igpsport/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    if (r && r.ok === false) { toast(r.error || "登录失败"); return; }
    toast("iGPSPORT 登录成功");
    refresh();
  } catch (e) {}
  finally { btn.disabled = false; }
};

$("igpsport-logout-btn").onclick = async () => {
  await api("/api/igpsport/logout", { method: "POST" });
  toast("已退出 iGPSPORT 账户");
  refresh();
};

// 免登录：直接进入面板并加载数据
refresh();
