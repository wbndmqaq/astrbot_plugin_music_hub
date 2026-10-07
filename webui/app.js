/* Music Hub 管理面板（原生 JS 单页应用，无构建依赖） */
(function () {
  "use strict";

  var $ = function (sel) { return document.querySelector(sel); };
  // 必须支持第二个 root 参数：焦点陷阱要只在弹窗内取可聚焦元素
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }
  function fmtCount(n) {
    n = num(n);
    if (n >= 1e8) return (n / 1e8).toFixed(1) + " 亿";
    if (n >= 1e4) return (n / 1e4).toFixed(1) + " 万";
    return String(n);
  }
  function fmtTime(ts) {
    if (!ts) return "--";
    var d = new Date(ts * 1000);
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2) + ":" + ("0" + d.getSeconds()).slice(-2);
  }
  function fmtDateTime(ts) {
    if (!ts) return "--";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2) + " " + fmtTime(ts);
  }
  // 一次性快照的问题：用户在系统里改「减少动态效果」后本页面不再降级。
  // 改为实时值 + change 监听。
  var reduceQuery = window.matchMedia ? matchMedia("(prefers-reduced-motion: reduce)") : null;
  function motionReduced() { return !!(reduceQuery && reduceQuery.matches); }
  if (reduceQuery && reduceQuery.addEventListener) {
    reduceQuery.addEventListener("change", function () {
      document.documentElement.classList.toggle("reduced-motion", motionReduced());
    });
  }
  var ACT_NAMES = { search: "搜索", play: "点歌", url: "取流", lyric: "歌词", detail: "详情", comment: "评论", mv: "MV", rank: "排行榜", playlist: "歌单", explore: "浏览", login: "登录", resolve: "解析", other: "其他" };
  var SRC_NAMES = { ncm: "网易云", kg: "酷狗", qq: "QQ音乐" };

  /* ─────────── Toast ─────────── */
  function srcTagHtml(source, cls) {
    return '<span class="' + cls + " " + esc(source) + '">' +
      '<img src="/webui/logos/' + esc(source) + '.png" alt="" />' +
      esc(SRC_NAMES[source] || source) + "</span>";
  }

  var TOAST_MAX = 4;
  function toast(msg, cls) {
    var box = $("#toastBox");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "toast-msg toast-" + (cls || "ok");
    el.textContent = msg;
    // 容器已声明 role=status aria-live=polite；逐条再设 role 会形成嵌套
    // live region，同一条消息被读屏播报两次
    box.appendChild(el);
    // 「检测连通性」一次发 3 条，不封顶会把屏幕塞满
    while (box.childElementCount > TOAST_MAX) box.removeChild(box.firstElementChild);
    el._t1 = setTimeout(function () { el.classList.add("toast-hide"); }, 2600);
    el._t2 = setTimeout(function () { el.remove(); }, 3000);
  }
  function clearToasts() {
    var box = $("#toastBox");
    if (!box) return;
    $$(".toast-msg", box).forEach(function (el) {
      clearTimeout(el._t1);
      clearTimeout(el._t2);
      el.remove();
    });
  }

  /* ─────────── API ─────────── */
  var authGen = 0;
  var DEFAULT_TIMEOUT = 15000;
  function withTimeout(opt, ms) {
    // 裸 fetch 没有超时：上游挂住时按钮永远停在 pending
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, ms || DEFAULT_TIMEOUT);
    var outer = opt.signal;
    if (outer) {
      if (outer.aborted) ctrl.abort();
      else outer.addEventListener("abort", function () { ctrl.abort(); });
    }
    opt.signal = ctrl.signal;
    return { opt: opt, done: function () { clearTimeout(timer); } };
  }
  async function api(url, opt, timeout) {
    opt = opt || {};
    var gen = authGen;
    var t = withTimeout(opt, timeout);
    var r;
    try {
      r = await fetch(url, t.opt);
    } catch (e) {
      if (e && e.name === "AbortError") {
        var err = new Error("请求超时或已取消");
        err.aborted = true;
        throw err;
      }
      throw e;
    } finally {
      t.done();
    }
    if (!r.ok) {
      var b = {};
      try { b = await r.json(); } catch (e) {}
      if (r.status === 401 && gen === authGen) enterLoggedOut();
      var err2 = new Error(b.error || ("HTTP " + r.status));
      err2.status = r.status;
      throw err2;
    }
    var ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
  }
  function jget(u, opt, timeout) { return api(u, opt, timeout); }
  async function jpost(u, body) {
    return api(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }

  /* ─────────── 主题 ─────────── */
  function applyTheme(mode) {
    var dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    var d = document.documentElement;
    d.dataset.themeMode = mode;
    d.dataset.theme = mode === "auto" ? (dark ? "dark" : "light") : mode;
    try { localStorage.setItem("mh-theme", mode); } catch (e) {}
  }
  function cycleTheme() {
    var cur = document.documentElement.dataset.themeMode || "auto";
    var next = cur === "auto" ? "dark" : cur === "dark" ? "light" : "auto";
    applyTheme(next);
    toast("主题：" + (next === "auto" ? "跟随系统" : next === "dark" ? "夜间紫" : "樱花粉"), "ok");
  }
  if (window.matchMedia) {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (document.documentElement.dataset.themeMode === "auto") applyTheme("auto");
    });
  }

  /* ─────────── 花瓣 ─────────── */
  function makePetals() {
    if (motionReduced()) return;
    var box = $("#petalLayer");
    if (!box || box.childElementCount) return;
    for (var i = 0; i < 14; i++) {
      var p = document.createElement("div");
      p.className = "petal " + (i % 3 === 1 ? "p2" : i % 3 === 2 ? "p3" : "");
      var size = (Math.random() * 8 + 8).toFixed(0);
      p.style.cssText = "left:" + (Math.random() * 100).toFixed(1) + "%;width:" + size + "px;height:" +
        (size * 0.72).toFixed(0) + "px;--dur:" + (9 + Math.random() * 8).toFixed(1) +
        "s;--delay:" + (-Math.random() * 14).toFixed(1) + "s";
      box.appendChild(p);
    }
  }

  /* ─────────── 滚动进度 ───────────
     节点与 scrollHeight 都在 rAF 外取好：每帧 querySelector +
     读 scrollHeight 会强制整页布局重算。 */
  var progressEl = null;
  var docScrollH = 0;
  function measureScroll() {
    progressEl = $("#scrollProgress");
    docScrollH = document.documentElement.scrollHeight;
  }
  var ticking = false;
  var _resizeTimer = null;
  window.addEventListener("scroll", function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      if (!progressEl) measureScroll();
      var h = docScrollH - window.innerHeight;
      var p = h > 0 ? window.scrollY / h : 0;
      if (progressEl) progressEl.style.transform = "scaleX(" + p.toFixed(4) + ")";
      ticking = false;
    });
  }, { passive: true });
  window.addEventListener("resize", function () {
    clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(measureScroll, 150);
  });

  /* ─────────── countUp ─────────── */
  function countUp(el, target) {
    target = num(target);
    if (motionReduced()) { el.textContent = fmtCount(target); return; }
    var start = null, dur = 900;
    function step(ts) {
      if (!start) start = ts;
      var p = Math.min(1, (ts - start) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = fmtCount(Math.round(target * eased));
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  /* ─────────── 导航 ─────────── */
  var ICONS = {
    overview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="3.5" y="3.5" width="7.5" height="7.5" rx="2.5"/><rect x="13" y="3.5" width="7.5" height="7.5" rx="2.5"/><rect x="3.5" y="13" width="7.5" height="7.5" rx="2.5"/><rect x="13" y="13" width="7.5" height="7.5" rx="2.5"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l5 5"/></svg>',
    accounts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20c1.2-3.6 4-5.4 7.5-5.4S18.3 16.4 19.5 20"/></svg>',
    stats: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M21 20H3.5"/></svg>',
    acl: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3l7.5 3v5.4c0 4.6-3 8-7.5 9.6-4.5-1.6-7.5-5-7.5-9.6V6z"/><path d="M9 12l2.2 2.2L15.4 10"/></svg>',
    config: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.8 1.8M16.7 16.7l1.8 1.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8"/></svg>',
    resolve: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>',
    sessions: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="4" y="10.5" width="16" height="10" rx="3"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/></svg>',
    logs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 5.5h16M4 10h11M4 14.5h13M4 19h7"/></svg>',
    /* 统计卡图标：与总览的 <img> logo 统一走 SVG，不再混用文本字符
       （"♪/Σ/网/狗/Q" 属把字符当图标，且与总览的图形体系冲突） */
    music: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="7" cy="17.5" r="2.6"/><circle cx="18" cy="15.5" r="2.6"/><path d="M9.6 17.5V7l10.9-2.4v10.9"/></svg>',
    sigma: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M17 5.5H7l5 6.5-5 6.5h10"/></svg>',
    trend: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 17.5l5-5.5 3.5 3L20 7.5"/><path d="M15.5 7.5H20V12"/></svg>',
    ncm: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17A8.5 8.5 0 0 0 12 3.5z" fill="currentColor" stroke="none"/></svg>',
    kg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 14c3-6 13-6 16 0"/><circle cx="12" cy="15" r="2.4"/><path d="M6 8.5c1.6 1.2 2.4 1.2 4 0M14 8.5c1.6 1.2 2.4 1.2 4 0"/></svg>',
    qq: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 11.5c0-4 3.6-7 8-7s8 3 8 7c0 4-3.6 7.5-8 7.5-1 0-2-.2-2.8-.5L5 20l1-3.2A7 7 0 0 1 4 11.5z"/></svg>'
  };
  var NAV_ITEMS = [
    { id: "overview", name: "总览" },
    { id: "search", name: "多音源搜索" },
    { id: "accounts", name: "账号扫码" },
    { id: "stats", name: "调用统计" },
    { id: "acl", name: "黑白名单" },
    { id: "config", name: "插件配置" },
    { id: "resolve", name: "链接解析" },
    { id: "sessions", name: "我的会话" },
    { id: "logs", name: "运行日志" }
  ];
  // 底栏只放 5 项：9×56px=504px 在 375px 屏放不下，停靠点会挤成两行
  var BOTTOM_IDS = ["overview", "search", "accounts", "stats"];
  var curPanel = "overview";
  var moreOpen = false;

  function navBtn(item, cls, iconFirst) {
    var b = document.createElement("button");
    b.className = cls + (item.id === curPanel ? " on" : "");
    b.dataset.panel = item.id;
    if (item.id === curPanel) b.setAttribute("aria-current", "page");
    b.innerHTML = iconFirst
      ? ICONS[item.id] + "<span>" + esc(item.name) + "</span>"
      : '<span class="bubble">' + ICONS[item.id] + '</span><span>' + esc(item.name) + "</span>";
    b.addEventListener("click", function () {
      closeMore();
      switchPanel(item.id);
    });
    return b;
  }
  function renderNav() {
    var side = $("#sideList"), bottom = $("#bottomNav");
    side.innerHTML = "";
    bottom.innerHTML = "";
    NAV_ITEMS.forEach(function (item) { side.appendChild(navBtn(item, "side-item", false)); });
    BOTTOM_IDS.forEach(function (id) {
      var it = NAV_ITEMS.filter(function (n) { return n.id === id; })[0];
      if (it) bottom.appendChild(navBtn(it, "bn-item", true));
    });
    var more = document.createElement("button");
    more.className = "bn-item" + (moreOpen ? " on" : "");
    more.dataset.act = "more-toggle";
    more.setAttribute("aria-expanded", moreOpen ? "true" : "false");
    more.setAttribute("aria-controls", "moreSheet");
    more.innerHTML = '<span class="bn-more" aria-hidden="true">⋯</span><span>更多</span>';
    bottom.appendChild(more);
    renderMore();
  }
  function renderMore() {
    var sheet = $("#moreSheet");
    if (!sheet) return;
    sheet.innerHTML = "";
    NAV_ITEMS.forEach(function (item) {
      if (BOTTOM_IDS.indexOf(item.id) >= 0) return;
      sheet.appendChild(navBtn(item, "side-item", false));
    });
    var foot = document.createElement("div");
    foot.className = "more-foot";
    // 主题切换与退出登录在手机上必须可达（不能藏在会被 display:none 的侧栏里）
    foot.innerHTML =
      '<button class="btn btn-ghost btn-sm" data-act="theme">切换主题</button>' +
      '<button class="btn btn-ghost btn-sm" data-act="logout">退出登录</button>';
    sheet.appendChild(foot);
  }
  function openMore() {
    moreOpen = true;
    var sheet = $("#moreSheet");
    if (!sheet) return;
    sheet.hidden = false;
    renderNav();
  }
  function closeMore() {
    if (!moreOpen) return;
    moreOpen = false;
    var sheet = $("#moreSheet");
    if (sheet) sheet.hidden = true;
    renderNav();
  }

  function switchPanel(id) {
    curPanel = id;
    closeMore();
    if (id !== "logs") stopLogsPoll();
    // 离开搜索面板即停止试听：否则音频在后台继续播，且按钮已随 DOM 销毁，
    // _previewBtn 指向游离节点，再点其它试听时状态判定彻底错乱
    if (id !== "search") { stopPreview(); abortPreview(); }
    $$(".panel").forEach(function (p) { p.classList.toggle("on", p.id === "panel-" + id); });
    $$(".side-item,.bn-item").forEach(function (b) {
      var on = b.dataset.panel === id;
      b.classList.toggle("on", on);
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    window.scrollTo({ top: 0, behavior: motionReduced() ? "auto" : "smooth" });
    var loaders = { overview: loadOverview, search: loadSearchExtras, accounts: loadAccounts, stats: loadStats, acl: loadAcl, config: loadConfig, resolve: null, sessions: loadSessions, logs: loadLogsPanel };
    var fn = loaders[id];
    if (typeof fn === "function") {
      Promise.resolve(fn()).catch(function (e) {
        if (e && e.aborted) return;
        toast(e.message, "error");
      });
    }
    // 文档高度只在 boot/resize 测过：面板渲染后 scrollHeight 可能翻倍，
    // 旧值会让进度条提前走满。下一帧重测（复用 measureScroll，避免每帧重查节点）
    requestAnimationFrame(measureScroll);
  }
  function loadSearchExtras() {
    focusSearch();
    loadScopes();
  }
  var _focusTimer = null;
  function focusSearch() {
    // 200ms 延迟 + 触发时校验 curPanel：面板已切走时绝不抢焦点。
    // clearTimeout 只防快速连续触发导致定时器堆叠，无需在 switchPanel 里取消。
    clearTimeout(_focusTimer);
    _focusTimer = setTimeout(function () {
      _focusTimer = null;
      var el = $("#searchInput");
      if (el && window.innerWidth > 768 && curPanel === "search") el.focus();
    }, 200);
  }
  function httpsUrl(url) {
    // scheme 白名单：http 强制升级 https；协议相对（//host/...）跟随页面协议；
    // 其余 scheme（javascript:/data:，上游被劫持才可能出现）一律丢弃。
    // CSP 也会拦，但不应该依赖兜底。
    var s = String(url || "").trim();
    if (/^http:\/\//i.test(s)) return "https://" + s.slice(7);
    if (/^https:\/\//i.test(s) || s.slice(0, 2) === "//") return s;
    return "";
  }

  /* ─────────── 登录 ─────────── */
  var loginShown = false;
  // 登出与 401 必须走同一条路径：停音频、停二维码/日志轮询、关弹窗一次做完
  function enterLoggedOut() {
    stopPreview();
    abortPreview();
    closeQR();
    stopLogsPoll();
    clearInterval(overviewTimer);
    showLogin();
  }
  function showLogin() {
    if (loginShown) return;
    loginShown = true;
    $("#loginMask").hidden = false;
    clearToasts();
    setTimeout(function () { $("#loginPwd").focus(); }, 150);
  }
  function hideLogin() {
    loginShown = false;
    $("#loginMask").hidden = true;
    $("#loginErr").hidden = true;
    $("#loginPwd").value = "";
  }
  async function doLogin() {
    var btn = $("#loginBtn");
    var pwd = $("#loginPwd").value;
    if (!pwd) { shakeLogin("请输入密码"); return; }
    btn.disabled = true;
    btn.textContent = "验证中…";
    try {
      await jpost("/api/auth/login", { password: pwd });
      authGen++;
      hideLogin();
      boot();
    } catch (e) {
      shakeLogin(e.message || "密码错误");
    } finally {
      btn.disabled = false;
      btn.textContent = "进入面板";
    }
  }
  function shakeLogin(msg) {
    var box = $(".login-box");
    var err = $("#loginErr");
    err.textContent = msg;
    err.hidden = false;
    box.classList.remove("shake");
    void box.offsetWidth;
    box.classList.add("shake");
  }
  $("#loginBtn").addEventListener("click", doLogin);
  $("#loginPwd").addEventListener("keydown", function (e) { if (e.key === "Enter") doLogin(); });

  /* ─────────── 加载态工具 ───────────
     账号/统计/黑白名单/配置/会话这几个面板首屏要等 1~9 次串行网络往返
     （如账号页最坏 9 次），空白期与「确实没数据」无法区分，必须有骨架屏。
     骨架屏与错误态用同一容器互斥：写骨架屏后若请求抛错，必须 renderError
     覆盖掉，否则 shimmer 永久驻留（看起来像一直在加载）。 */
  function skeletonRows(n, h) {
    var out = "";
    for (var i = 0; i < (n || 3); i++) {
      out += '<div class="skeleton" aria-hidden="true" style="height:' + (h || 76) + 'px"></div>';
    }
    return out;
  }
  function busy(sel, on) {
    var el = $(sel);
    if (el) el.setAttribute("aria-busy", on ? "true" : "false");
  }
  function withSkeleton(sel, html) {
    var el = $(sel);
    if (!el) return;
    el.innerHTML = html || '<div class="skeleton" aria-hidden="true" style="height:120px"></div>';
    // 骨架屏不是「已渲染」：必须清掉标记，否则重试成功后 loadXxx 会
    // 误判成原地更新而把骨架屏永久留下
    el.removeAttribute("data-rendered");
    busy(sel, true);
  }
  function renderError(sel, msg) {
    var el = $(sel);
    if (!el) return;
    busy(sel, false);
    el.removeAttribute("data-rendered");
    el.innerHTML = '<div class="empty-state">⚠ ' + esc(msg) +
      '<br><button class="btn btn-ghost btn-sm" style="margin-top:10px" data-act="panel-retry">重试</button></div>';
  }
  function setLoaded(sel) { busy(sel, false); }
  // 统计卡 markup 两处共用（总览 / 调用统计）：icon 优先用 c.src 的平台 logo，
  // 否则取 ICONS[c.ico]；总览的静默轮询不走这里（走 patchNumbers 原地更新）
  function statCardHtml(cards) {
    return cards.map(function (c) {
      return '<div class="stat-card' + (c.tone ? " tone-" + c.tone : "") + '">' +
        '<div class="icon">' +
        (c.src ? '<img src="/webui/logos/' + esc(c.src) + '.png" alt="" />' : ICONS[c.ico]) + "</div>" +
        '<div class="label">' + esc(c.label) + "</div>" +
        '<div class="num" data-n="' + c.num + '">0</div>' +
        '<div class="foot">' + esc(c.foot) + "</div></div>";
    }).join("");
  }

  /* ─────────── 总览 ─────────── */
  var _ovSeq = 0;
  var _ovInited = false;
  async function loadOverview(silent) {
    // 静默轮询不清骨架屏：面板已有内容，重建会把入场动画和数字滚动重播一遍
    if (!silent) {
      withSkeleton("#statGrid", skeletonRows(4, 96));
      withSkeleton("#svcGrid", skeletonRows(3, 84));
    }
    var seq = ++_ovSeq;
    var d;
    try {
      d = await jget("/api/overview");
    } catch (e) {
      if (seq !== _ovSeq || (e && e.aborted)) return;
      // 错误必须写进容器（骨架屏与错误态互斥），否则骨架屏会一直闪
      if (!silent) { renderError("#statGrid", e.message); renderError("#svcGrid", e.message); }
      return;
    }
    if (seq !== _ovSeq) return;
    if (!d) { if (!silent) { renderError("#statGrid", "总览数据为空"); renderError("#svcGrid", "总览数据为空"); } return; }
    setLoaded("#statGrid");
    setLoaded("#svcGrid");
    $("#chipVersion").innerHTML = '<span class="dot"></span>v' + esc(d.version);
    var master = $("#chipMaster");
    // 加载完成前用中性灰点，绿色只表示「运行中」
    master.className = "chip " + (d.enable ? "ok" : "warn");
    master.innerHTML = '<span class="dot"></span>' + (d.enable ? "运行中" : "已关闭");
    var aclChip = $("#chipAcl");
    var aclMode = d.acl && d.acl.mode;
    aclChip.className = "chip" + (aclMode === "off" ? "" : " warn");
    aclChip.innerHTML = '<span class="dot"></span>ACL ' + (aclMode === "off" ? "关闭" : aclMode === "blacklist" ? "黑名单" : "白名单");

    var first = !_ovInited;
    _ovInited = true;
    var cards = [
      { label: "今日调用", num: num(d.todayTotal), foot: "全部音源合计", ico: "music" },
      { label: "网易云 · 今日", num: num(d.today && d.today.ncm), foot: "累计 " + fmtCount(d.total && d.total.ncm), tone: "ncm", src: "ncm" },
      { label: "酷狗 · 今日", num: num(d.today && d.today.kg), foot: "累计 " + fmtCount(d.total && d.total.kg), tone: "kg", src: "kg" },
      { label: "QQ · 今日", num: num(d.today && d.today.qq), foot: "累计 " + fmtCount(d.total && d.total.qq), tone: "qq", src: "qq" }
    ];
    var grid = $("#statGrid");
    // 只有结构变化才重建 innerHTML。10 秒轮询整块替换会：
    // ① 重播 .stat-card 的 cardPop + nth-child 60~240ms 延迟（读数字时每 10 秒弹跳一次）
    // ② 清掉用户的文本选区
    // 轮询走 patchNumbers() 逐个 textContent，不动 DOM 结构。
    // 判据用显式标记而非 childElementCount：骨架屏 skeletonRows(4,96) 恰好也是 4，
    // 两者撞车会让重试后的骨架屏永久驻留（patchNumbers 找不到 .num 就什么都不做）。
    if (grid.getAttribute("data-rendered") !== "1" || !grid.querySelector(".stat-card")) {
      grid.innerHTML = statCardHtml(cards);
      grid.classList.remove("no-anim");
      grid.setAttribute("data-rendered", "1");
      $$(".num", grid).forEach(function (el) { countUp(el, num(el.dataset.n)); });
    } else {
      grid.classList.add("no-anim");
      patchNumbers(grid, cards);
    }

    renderSvc(d.sources || []);
    renderFeed(d.recent || [], first);
    $("#recentHint").textContent = (d.recent || []).length ? "最新 " + d.recent.length + " 条" : "";
  }
  function patchNumbers(root, cards) {
    var nums = $$(".stat-card .num", root);
    var foots = $$(".stat-card .foot", root);
    cards.forEach(function (c, i) {
      var el = nums[i];
      if (!el) return;
      var next = fmtCount(c.num);
      if (el.textContent !== next) el.textContent = next;
      el.dataset.n = c.num;
      if (foots[i] && c.foot != null) foots[i].textContent = c.foot;
    });
  }

  function renderSvc(sources) {
    var grid = $("#svcGrid");
    if (!sources.length) {
      grid.classList.remove("no-anim");
      grid.removeAttribute("data-rendered");
      grid.innerHTML = '<div class="empty-state">没有可用的音源服务</div>';
      return;
    }
    // 与 statGrid 同理：音源状态每 10 秒轮询一次，不能整块重建。
    // 判据用显式标记而不是 childElementCount —— 后者与骨架屏的行数
    // （skeletonRows(3,84) 恰好也是 3）会撞车，导致骨架屏永久驻留。
    if (grid.getAttribute("data-rendered") !== "1" || grid.childElementCount !== sources.length) {
      grid.classList.remove("no-anim");
      grid.innerHTML = sources.map(function (s) {
        var ava = '<img class="svc-logo" src="/webui/logos/' + esc(s.source) + '.png" alt="" />';
        return '<div class="svc" data-src="' + esc(s.source) + '">' +
          '<div class="svc-head">' + ava +
          '<span class="svc-name">' + esc(s.name) + '</span><span class="svc-tag"></span></div>' +
          '<div class="svc-meta"></div></div>';
      }).join("");
      grid.setAttribute("data-rendered", "1");
    } else {
      grid.classList.add("no-anim");
    }
    // 按 data-src 拼 querySelector 会对引号/反斜杠敏感，改索引对位：
    // 上面的重建分支保证 .svc 数量与 sources 一一对应
    var cards = $$(".svc", grid);
    sources.forEach(function (s, i) {
      var card = cards[i];
      if (!card) return;
      var tag = !s.enabled ? "未配置" : s.logged ? "已登录" : s.source === "qq" ? "匿名可用" : "匿名";
      var tagEl = card.querySelector(".svc-tag");
      tagEl.className = "svc-tag " + (!s.enabled ? "bad" : s.logged ? "ok" : "wait");
      tagEl.textContent = tag;
      card.querySelector(".svc-meta").innerHTML =
        "API：" + esc(s.apiBase) + "<br>音质：" + esc(s.quality);
    });
  }

  function renderFeed(recent, animate) {
    var feed = $("#recentFeed");
    if (!recent.length) {
      feed.classList.remove("no-anim");
      feed.innerHTML = '<div class="empty-state">暂无调用记录，去群里点首歌试试</div>';
      return;
    }
    feed.classList.toggle("no-anim", animate === false);
    feed.innerHTML = recent.map(function (r, i) {
      // 静默更新不写 animation-delay：入场动画只应在首载播一次，
      // 否则 10 秒轮询会让整列记录反复滑入
      var delay = animate === false ? "" : ' style="animation-delay:' + Math.min(i * 40, 200) + 'ms"';
      // 成功/失败不靠边框颜色区分（对白底约 1.4:1），加 ✓/✕ 图标并给读屏标签
      return '<div class="feed-item' + (r.ok ? "" : " err") + '"' + delay + ">" +
        '<span class="fi-ic ' + (r.ok ? "ok" : "bad") + '" aria-hidden="true">' + (r.ok ? "✓" : "✕") + "</span>" +
        '<span class="sr-only">' + (r.ok ? "成功：" : "失败：") + "</span>" +
        '<span class="fi-time">' + fmtTime(r.ts) + "</span>" +
        srcTagHtml(r.source, "fi-src") +
        '<span class="fi-txt">' + esc(ACT_NAMES[r.action] || r.action) + (r.detail ? " · " + esc(r.detail) : "") + "</span></div>";
    }).join("");
  }

  /* ─────────── 多音源搜索 ─────────── */
  var _searchSeq = 0;
  var searchGroups = [];
  var searchItems = [];
  var searchFilter = "all";
  var searchType = "song";
  // 非 song 类型的中文名：空结果文案与结果徽标共用一份
  var TYPED_NAMES = { song: "歌曲", playlist: "歌单", album: "专辑", artist: "歌手" };
  var _audio = null;
  var _previewBtn = null;
  var _previewSeq = 0;
  var _searchCtrl = null;
  var _previewCtrl = null;
  var _debounceTimer = null;

  function stopPreview() {
    _previewSeq++; // 作废在途请求，防止迟到响应复活旧试听
    if (_audio) {
      _audio.pause();
      // 只 pause 不清 src：连接与缓冲不释放，音频仍在后台占带宽
      _audio.removeAttribute("src");
      _audio.load();
      _audio = null;
    }
    if (_previewBtn) { _previewBtn.textContent = "▶ 试听"; _previewBtn.classList.remove("playing"); _previewBtn = null; }
  }
  function abortPreview() {
    if (_previewCtrl) { _previewCtrl.abort(); _previewCtrl = null; }
  }
  async function playPreview(source, sid, btn) {
    if (_previewBtn === btn) { stopPreview(); abortPreview(); return; }
    stopPreview();
    abortPreview();
    var seq = _previewSeq;
    var ctrl = new AbortController();
    _previewCtrl = ctrl;
    btn.disabled = true; btn.textContent = "加载中…";
    try {
      var r = await jget("/api/preview?source=" + encodeURIComponent(source) + "&sid=" + encodeURIComponent(sid),
        { signal: ctrl.signal });
      if (seq !== _previewSeq) return; // 等待期间用户又点了别的试听
      if (!r.url) throw new Error(r.error || "未取到试听链接");
      _audio = new Audio(r.url);
      _previewBtn = btn;
      var a = _audio;
      // autoplay 被拦截时必须告知：否则按钮显示「⏸ 播放中」但用户听不到声
      a.play().catch(function () {
        if (a !== _audio) return;
        stopPreview();
        toast("浏览器拦截了自动播放，请再点一次试听", "warn");
      });
      btn.textContent = "⏸ 播放中" + (r.label ? " · " + r.label : "");
      btn.classList.add("playing");
      a.onended = stopPreview;
      a.onerror = function () {
        if (a !== _audio) return;
        stopPreview();
        toast("音频加载失败，可能是直链已过期或需要登录", "error");
      };
    } catch (e) {
      if (seq === _previewSeq && !(e && e.aborted)) toast(e.message, "error");
    } finally {
      btn.disabled = false;
      if (_previewBtn !== btn) btn.textContent = "▶ 试听";
    }
  }
  async function deliverPlay(source, sid, btn) {
    var umo = $("#remoteScope").value;
    if (!umo) { toast("先选择要投递的会话（群/好友）", "warn"); return; }
    btn.disabled = true;
    try {
      var r = await jpost("/api/remote/play", { umo: umo, source: source, sid: sid });
      toast("已投递「" + (r.name || "") + "」到所选会话", "ok");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      btn.disabled = false;
    }
  }
  var _scopeSeq = 0;
  async function loadScopes() {
    var sel = $("#remoteScope");
    if (!sel) return;
    var seq = ++_scopeSeq;
    try {
      var d = await jget("/api/remote/scopes");
      if (seq !== _scopeSeq) return;  // 快速切面板时丢弃过期响应
      var cur = sel.value;
      sel.innerHTML = (d.scopes || []).map(function (s) {
        return '<option value="' + esc(s.umo) + '">' + esc(s.scope) + "</option>";
      }).join("") || '<option value="">暂无会话（群里先发一次指令）</option>';
      $("#remoteBar").hidden = false;
      if (cur) sel.value = cur;
    } catch (e) {
      if (seq !== _scopeSeq || (e && e.aborted)) return;
      // 失败也要展开投递条：保留手动刷新入口，且不能静默
      $("#remoteBar").hidden = false;
      toast(e.message, "error");
    }
  }
  async function doSearch() {
    clearTimeout(_debounceTimer);
    _debounceTimer = null;
    var keyword = $("#searchInput").value.trim();
    if (!keyword) { toast("先输入关键词", "warn"); return; }
    var seq = ++_searchSeq;
    var box = $("#searchResults");
    stopPreview();
    abortPreview();
    // /api/search 是三平台并行扇出：重入前必须 abort 上一次，
    // 否则旧关键词的响应会覆盖新结果并浪费三份配额
    if (_searchCtrl) _searchCtrl.abort();
    var ctrl = new AbortController();
    _searchCtrl = ctrl;
    box.innerHTML = '<div class="skeleton" aria-hidden="true" style="height:76px"></div><div class="skeleton" aria-hidden="true" style="height:76px"></div><div class="skeleton" aria-hidden="true" style="height:76px"></div>';
    var d;
    try {
      // 搜索比普通请求慢（三平台并行 + 去重），单独给 25s
      d = await jget("/api/search?keyword=" + encodeURIComponent(keyword) + "&type=" + encodeURIComponent(searchType),
        { signal: ctrl.signal }, 25000);
    } catch (e) {
      if (seq === _searchSeq && !(e && e.aborted)) box.innerHTML = '<div class="empty-state">⚠ ' + esc(e.message) + "</div>";
      return;
    }
    if (seq !== _searchSeq) return;
    searchGroups = d.groups || [];
    searchItems = d.items || [];
    updateFilterBar();
    renderSearchResults(true);
    var empty = searchType === "song" ? !searchGroups.length : !searchItems.length;
    if (empty) box.innerHTML = '<div class="empty-state">没有搜到「' + esc(keyword) + '」相关的' + esc(TYPED_NAMES[searchType] || "内容") + "</div>";
    else toast("找到 " + (searchType === "song" ? searchGroups.length : searchItems.length) + " 条结果", "ok");
  }
  // 筛选条显隐只认一处判据：歌单 tab 切回单曲且输入为空时，
  // 不能露出一个点了必然空结果的筛选条。
  function updateFilterBar() {
    var show = searchType === "song" && searchGroups.length > 0;
    $("#srcFilter").hidden = !show;
    if (!show && searchFilter !== "all") {
      searchFilter = "all";
      $$(".sfilter").forEach(function (x) {
        var on = x.dataset.f === "all";
        x.classList.toggle("on", on);
        x.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }
  }
  function renderSearchResults(animate) {
    var box = $("#searchResults");
    // 筛选/翻页是原地换内容，不该重播整列的滑入动画（.no-anim 见 style.css）
    box.classList.toggle("no-anim", animate === false);
    if (searchType !== "song") { renderTypedResults(box, animate); return; }
    var list = searchGroups.filter(function (g) {
      if (searchFilter === "all") return true;
      if (searchFilter === "multi") return (g.versions || []).length > 1;
      return (g.versions || []).some(function (v) { return v.source === searchFilter; });
    });
    if (!list.length) {
      box.innerHTML = '<div class="empty-state">该筛选条件下没有结果</div>';
      return;
    }
    box.innerHTML = list.map(function (g, i) {
      var vers = (g.versions || []).map(function (v) {
        // 类名不带 v- 前缀：CSS 定义的是 .vchip.ncm
        return '<span class="vchip ' + esc(v.source) + '" title="' + esc(v.sourceName) + (v.pay ? " · VIP" : "") + '">' +
          '<img src="/webui/logos/' + esc(v.source) + '.png" alt="' + esc(v.sourceName) + '" />' +
          (v.pay ? "<small>VIP</small>" : "") + "</span>";
      }).join("");
      var primary = (g.versions || [])[0] || {};
      var canPlay = primary.sid && !primary.pay;
      return '<div class="result-row"' + (animate === false ? "" : ' style="animation-delay:' + Math.min(i * 45, 240) + 'ms"') + ">" +
        // httpsUrl 可能拒绝非 http(s) 地址返回空串：外层三元必须判净化结果，
        // 否则输出 src=""（部分浏览器会当当前页 URL 发请求）
        ((g.cover && httpsUrl(g.cover))
          ? '<img class="result-cover" src="' + esc(httpsUrl(g.cover)) + '" referrerpolicy="no-referrer" alt="" />'
          : '<div class="result-cover-ph" aria-hidden="true">♪</div>') +
        '<div class="result-main"><div class="result-name">' + esc(g.name) + "</div>" +
        '<div class="result-sub">' + esc(g.artist) + (g.album ? " · " + esc(g.album) : "") + "</div></div>" +
        '<div class="result-vers">' + vers + "</div>" +
        '<div class="row-actions">' +
        (canPlay ? '<button class="btn btn-ghost btn-sm" data-act="preview" data-source="' + esc(primary.source) + '" data-sid="' + esc(primary.sid) + '">▶ 试听</button>' : "") +
        '<button class="btn btn-blue btn-sm" data-act="deliver" data-source="' + esc(primary.source) + '" data-sid="' + esc(primary.sid) + '">投递</button>' +
        "</div>" +
        '<div class="result-dur">' + esc(g.duration || "") + "</div></div>";
    }).join("");
    bindCoverFallback(box);
  }
  function renderTypedResults(box, animate) {
    // 歌单/专辑/歌手不渲染投递按钮：/api/search 非 song 分支只回 id 无 sid，
    // 且把整个歌单当音频投递本就无意义，这里只展示来源与类型
    box.innerHTML = searchItems.map(function (it, i) {
      return '<div class="result-row"' + (animate === false ? "" : ' style="animation-delay:' + Math.min(i * 45, 240) + 'ms"') + ">" +
        ((it.cover && httpsUrl(it.cover))
          ? '<img class="result-cover" src="' + esc(httpsUrl(it.cover)) + '" referrerpolicy="no-referrer" alt="" />'
          : '<div class="result-cover-ph" aria-hidden="true">♪</div>') +
        '<div class="result-main"><div class="result-name">' + esc(it.name) + "</div>" +
        '<div class="result-sub">' + esc(it.sub || "") + (it.count ? " · " + esc(it.count) + " 首" : "") + "</div></div>" +
        '<div class="result-vers"><span class="src-dot ' + esc(it.source) + '">' + esc(it.sourceName || it.source) + "</span></div>" +
        '<div class="result-dur"><span class="badge">' + esc(TYPED_NAMES[searchType] || searchType) + "</span></div></div>";
    }).join("") || '<div class="empty-state">没有搜到相关结果</div>';
    bindCoverFallback(box);
  }
  function bindCoverFallback(box) {
    $$("img.result-cover", box).forEach(function (img) {
      img.addEventListener("error", function () {
        var ph = document.createElement("div");
        ph.className = "result-cover-ph";
        ph.setAttribute("aria-hidden", "true");
        ph.textContent = "♪";
        img.replaceWith(ph);
      });
    });
  }
  $("#searchTabs").addEventListener("click", function (e) {
    var b = e.target.closest(".stab");
    if (!b) return;
    searchType = b.dataset.t || "song";
    $$("#searchTabs .stab").forEach(function (x) {
      var on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-pressed", on ? "true" : "false");  // 与视觉选中态保持一致
    });
    updateFilterBar();
    if ($("#searchInput").value.trim()) doSearch();
  });
  $("#srcFilter").addEventListener("click", function (e) {
    var b = e.target.closest(".sfilter");
    if (!b) return;
    searchFilter = b.dataset.f;
    $$(".sfilter").forEach(function (x) {
      var on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-pressed", on ? "true" : "false");
    });
    renderSearchResults(false);
  });
  $("#searchInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); doSearch(); }
  });
  // 输入即搜：250ms debounce，避免每敲一个字都扇出三平台
  $("#searchInput").addEventListener("input", function () {
    clearTimeout(_debounceTimer);
    var v = this.value.trim();
    if (!v) { updateFilterBar(); return; }
    _debounceTimer = setTimeout(function () { doSearch(); }, 250);
  });

  /* ─────────── 账号扫码 ─────────── */
  var _acctSeq = 0;
  async function loadAccounts() {
    var seq = ++_acctSeq;
    withSkeleton("#acctGrid", skeletonRows(3, 110));
    var d;
    try {
      d = await jget("/api/accounts");
    } catch (e) {
      if (seq !== _acctSeq || (e && e.aborted)) return;
      renderError("#acctGrid", e.message);
      return;
    }
    if (seq !== _acctSeq) return;
    if (!d) { renderError("#acctGrid", "账号数据为空"); return; }
    setLoaded("#acctGrid");
    var colors = { ncm: "var(--ncm)", kg: "var(--kg)", qq: "var(--qq)" };
    var inks = { ncm: "var(--ncm-ink)", kg: "var(--kg-ink)", qq: "var(--qq-ink)" };
    $("#acctGrid").innerHTML = (d.accounts || []).map(function (a, i) {
      var ok = a.loggedIn;
      // httpsUrl 拒绝非 http(s) 头像时返回空串：判净化结果而非原字段，
      // 落回平台 logo 占位而不是 src=""
      var ava = ok && a.avatar && httpsUrl(a.avatar)
        ? '<img src="' + esc(httpsUrl(a.avatar)) + '" alt="" referrerpolicy="no-referrer" />'
        : '<img class="acct-src-logo" src="/webui/logos/' + esc(a.source) + '.png" alt="" />';
      return '<div class="acct" style="--sc:' + (colors[a.source] || "#888") + ';--sci:' + (inks[a.source] || "var(--sub)") + ';animation-delay:' + i * 70 + 'ms">' +
        '<div class="acct-head"><div class="acct-ava' + (ok ? " ok" : "") + '">' + ava + "</div>" +
        '<div><div class="acct-name">' + esc(a.name) + "</div>" +
        '<div class="acct-sub">' + (ok ? esc(a.nickname || a.uid || "已登录") : "未登录") + "</div></div>" +
        '<span class="acct-badge ' + (ok ? "ok" : "off") + '">' + (ok ? "已登录" : "未登录") + "</span></div>" +
        '<div class="acct-meta"><span>音质 <b>' + esc(a.quality) + "</b></span>" +
        (a.vip ? "<span>" + esc(a.vip) + "</span>" : "") +
        (a.enabled ? "" : '<span class="warn-inline">API 未配置</span>') + "</div>" +
        '<div class="acct-actions">' +
        '<button class="btn btn-gold btn-sm" data-act="qr" data-src="' + esc(a.source) + '"' + (a.enabled ? "" : " disabled") + ">扫码登录</button>" +
        '<button class="btn btn-ghost btn-sm" data-act="acct-refresh" data-src="' + esc(a.source) + '"' + (a.source === "qq" && ok ? "" : " disabled") + ">刷新凭证</button>" +
        '<button class="btn btn-ghost btn-sm" data-act="acct-logout" data-src="' + esc(a.source) + '"' + (ok ? "" : " disabled") + ">退出登录</button>" +
        "</div></div>";
    }).join("");
  }

  /* 扫码弹窗 */
  var qrTicket = null, qrTimer = null, qrSource = null;
  var _qrPollBusy = false;
  // 连续失败计数：扫码窗口几分钟，一次网络抖动（15s 超时）不该把轮询杀掉。
  // ≤3 次继续轮，>3 才转终态；任意一轮成功即清零（openQR 开新会话也清零）
  var qrFailCount = 0;
  var _qrPrevFocus = null;
  // openQR 的 await 期间用户可能已按 Esc 关窗；返回后必须核对 token
  var _qrOpenToken = 0;
  var FOCUSABLE = 'button:not([disabled]),[href],input,select,textarea,[tabindex]:not([tabindex="-1"])';
  async function openQR(source) {
    qrSource = source;
    qrFailCount = 0;
    var token = ++_qrOpenToken;
    _qrPrevFocus = document.activeElement;   // 关闭后归还焦点，保持键盘动线
    $("#qrMask").hidden = false;
    var dlg = $("#qrDialog");
    if (dlg) dlg.focus();
    $("#qrTitle").textContent = SRC_NAMES[source] + " · 扫码登录";
    // src="" 在部分浏览器会当作当前页面 URL 发起请求
    $("#qrImg").removeAttribute("src");
    $("#qrImgWrap").classList.remove("expired");
    setQRState("正在获取二维码…", "");
    try {
      var r = await jpost("/api/qr/start", { source: source, type: "qq" });
      // 弹窗已关 / 已被另一次 openQR 取代：这次的 ticket 直接取消，
      // 否则会成为服务端孤儿会话，且下面的轮询还会一直打 /api/qr/status
      if (token !== _qrOpenToken || $("#qrMask").hidden) {
        if (r && r.ticket) jpost("/api/qr/cancel", { ticket: r.ticket }).catch(function () {});
        return;
      }
      qrTicket = r.ticket;
      // r.qr 是后端自产的 data:image/png;base64（core/login.py 组包），不是外链，
      // 过不了 httpsUrl 白名单（data: 被刻意丢弃）；这里钉死 data URI 形态——
      // 形态不对（被劫持/改字段才可能出现）就当没有图，落回 qrUrl 或错误提示
      var qrData = typeof r.qr === "string" && r.qr.indexOf("data:image/png;base64,") === 0 ? r.qr : "";
      if (qrData) { $("#qrImg").src = qrData; setQRState("请使用手机 App 扫码", ""); }
      else if (r.qrUrl) {
        // 纯文本 URL 不可点
        setQRStateHTML('<a href="' + esc(httpsUrl(r.qrUrl)) + '" target="_blank" rel="noopener">点此打开二维码页面</a>（或复制链接到浏览器）');
      }
      else { setQRState("未获取到二维码，请刷新重试", "bad"); return; }
      pollQR();
    } catch (e) {
      if (token !== _qrOpenToken) return;
      setQRState("创建失败：" + e.message, "bad");
    }
  }
  // 递归 setTimeout 而非 setInterval（同 pollLogsOnce 的模式）：interval 会与
  // 上一次请求重叠，没有 in-flight 守卫时同一张票可能被并发查询两次
  function pollQR() { scheduleQRPoll(_qrOpenToken); }
  function scheduleQRPoll(token) {
    clearTimeout(qrTimer);
    qrTimer = setTimeout(function () { pollQROnce(token); }, 1500);
  }
  async function pollQROnce(token) {
    qrTimer = null;
    if (_qrPollBusy || token !== _qrOpenToken || !qrTicket || $("#qrMask").hidden) return;
    _qrPollBusy = true;
    try {
      var s = await jget("/api/qr/status?ticket=" + encodeURIComponent(qrTicket));
      if (token !== _qrOpenToken) return;  // 等待期间弹窗已关 / 已重新打开
      qrFailCount = 0;   // 本轮查询成功：连续失败计数清零
      if (s.state === "wait") { setQRState("等待扫码…", ""); }
      else if (s.state === "scanned") { setQRState("已扫码，请在手机上确认", ""); }
      else if (s.state === "done") {
        setQRState("登录成功 " + (s.nickname || ""), "ok");
        toast(SRC_NAMES[qrSource] + " 登录成功", "ok");
        setTimeout(closeQR, 900);
        loadAccounts();
        return;
      } else if (s.state === "timeout") {
        $("#qrImgWrap").classList.add("expired");
        setQRState("二维码已过期，请刷新", "bad");
        return;
      } else if (s.state === "refuse" || s.state === "cancel") {
        setQRState("已取消：" + (s.msg || ""), "bad");
        return;
      }
    } catch (e) {
      // 与 pollLogsOnce 的「早退也重排」纪律对齐：一次网络抖动不该停摆整个扫码
      // 窗口。连续失败 ≤3 次继续轮（文案用中性色，不吓退正在扫码的人），超过才
      // 转终态 bad；弹窗已关则直接丢弃
      qrFailCount++;
      if (token !== _qrOpenToken || qrFailCount > 3) {
        if (token === _qrOpenToken) setQRState("查询失败：" + e.message, "bad");
        return;
      }
      setQRState("查询失败，重试中…", "");
    } finally {
      _qrPollBusy = false;
    }
    scheduleQRPoll(token);  // wait / scanned / 瞬时失败继续轮，终态不再排下一轮
  }
  function setQRState(text, cls) {
    var el = $("#qrState");
    el.textContent = text;
    el.className = "qr-state " + (cls || "");
  }
  function setQRStateHTML(html) {
    var el = $("#qrState");
    el.innerHTML = html;
    el.className = "qr-state";
  }
  function closeQR() {
    _qrOpenToken++;   // 作废在途的 openQR，其返回后不会再赋值/轮询
    clearTimeout(qrTimer);
    qrTimer = null;
    // 无条件发 cancel（后端有 ticket 校验，多发无害）：openQR 请求飞行期间关窗时
    // qrTicket 还是 null，漏发会让服务端会话成孤儿。
    jpost("/api/qr/cancel", { ticket: qrTicket }).catch(function () {});
    qrTicket = null;
    $("#qrMask").hidden = true;
    if (_qrPrevFocus && _qrPrevFocus.focus) _qrPrevFocus.focus();  // 焦点归位
    _qrPrevFocus = null;
  }

  /* ─────────── 统计 ─────────── */
  var _stSeq = 0;
  async function loadStats() {
    var seq = ++_stSeq;
    withSkeleton("#statGrid2", skeletonRows(4, 96));
    withSkeleton("#actionBars", skeletonRows(4, 40));
    chartSkeleton('<div class="skeleton" aria-hidden="true" style="height:200px"></div>');
    withSkeleton("#errList", skeletonRows(3, 40));
    var d;
    try {
      d = await jget("/api/stats?days=14");
    } catch (e) {
      if (seq !== _stSeq || (e && e.aborted)) return;
      renderError("#statGrid2", e.message);
      renderError("#actionBars", e.message);
      renderError("#errList", e.message);
      chartError(e.message);
      return;
    }
    if (seq !== _stSeq) return;
    if (!d) { renderError("#statGrid2", "统计数据为空"); renderError("#actionBars", "统计数据为空"); renderError("#errList", "统计数据为空"); chartError("统计数据为空"); return; }
    setLoaded("#statGrid2");
    setLoaded("#actionBars");
    setLoaded("#errList");
    var cards = [
      { label: "今日调用", num: num(d.todayTotal), foot: "全部音源", ico: "music" },
      { label: "累计调用", num: num(d.totalAll), foot: "历史总计", ico: "sigma" },
      { label: "网易云累计", num: num(d.total && d.total.ncm), tone: "ncm", foot: "今日 " + num(d.today && d.today.ncm), ico: "ncm" },
      { label: "酷狗累计", num: num(d.total && d.total.kg), tone: "kg", foot: "今日 " + num(d.today && d.today.kg), ico: "kg" },
      { label: "QQ 累计", num: num(d.total && d.total.qq), tone: "qq", foot: "今日 " + num(d.today && d.today.qq), ico: "qq" }
    ];
    var grid = $("#statGrid2");
    grid.innerHTML = statCardHtml(cards);
    $$(".num", grid).forEach(function (el) { countUp(el, num(el.dataset.n)); });

    drawTrend(d.days || []);
    drawActions(d.byActionToday || {});
    renderRecentTable(d.recent || []);
    renderErrors(d.topErrors || []);
    loadHistory().catch(function () {});
  }

  function renderErrors(top) {
    var box = $("#errList");
    if (!top.length) {
      box.innerHTML = '<div class="empty-state">最近没有失败记录</div>';
      return;
    }
    var max = Math.max.apply(null, top.map(function (t) { return num(t.count); })) || 1;
    box.innerHTML = top.map(function (t, i) {
      return '<div class="err-row" style="animation-delay:' + Math.min(i * 40, 200) + 'ms">' +
        '<div class="err-detail" title="' + esc(t.detail) + '">' + esc(t.detail) + "</div>" +
        '<div class="err-bar-wrap"><div class="err-bar" style="--w:' + (num(t.count) / max).toFixed(4) + '"></div></div>' +
        '<div class="err-count">× ' + num(t.count) + "</div></div>";
    }).join("");
  }
  var _histSeq = 0;
  async function loadHistory() {
    var seq = ++_histSeq;
    // 骨架屏与错误态互斥（同 loadAcl 等面板）：失败必须 renderError 覆盖掉骨架屏
    withSkeleton("#historyFeed", skeletonRows(2, 60));
    var d;
    try {
      d = await jget("/api/history");
    } catch (e) {
      if (seq !== _histSeq || (e && e.aborted)) return;
      renderError("#historyFeed", e.message);
      return;
    }
    if (seq !== _histSeq) return;  // 快速切面板时丢弃过期响应
    var feed = $("#historyFeed");
    var rows = (d && d.history) || [];
    if (!rows.length) {
      feed.innerHTML = '<div class="empty-state">各会话还没有播放记录</div>';
      return;
    }
    feed.innerHTML = rows.map(function (r) {
      var names = (r.items || []).slice(0, 3).map(function (it) {
        return esc(it.name) + " - " + esc(it.artist);
      }).join("；");
      return '<div class="feed-item"><span class="fi-scope">' + esc(r.scope) + "</span>" +
        '<span class="fi-txt">' + names + ((r.items || []).length > 3 ? " …" : "") + "</span></div>";
    }).join("");
  }

  /* 图表骨架屏必须落在 svg 的兄弟节点 #chartSkeleton 上：
     SVG 元素不能容纳 HTML 子节点，<div class="skeleton"> 塞进 #trendChart 不会渲染 */
  function chartSkeleton(html) {
    var sk = $("#chartSkeleton"), wrap = $("#chartWrap"), svg = $("#trendChart");
    if (sk) { sk.innerHTML = html; sk.hidden = false; }
    if (wrap) wrap.classList.add("has-state");
    if (svg) svg.setAttribute("aria-busy", "true");
  }
  function chartError(msg) {
    var sk = $("#chartSkeleton"), wrap = $("#chartWrap"), svg = $("#trendChart");
    if (sk) {
      sk.hidden = false;
      sk.innerHTML = '<div class="empty-state">⚠ ' + esc(msg) +
        '<br><button class="btn btn-ghost btn-sm" style="margin-top:10px" data-act="panel-retry">重试</button></div>';
    }
    if (wrap) wrap.classList.add("has-state");
    if (svg) svg.setAttribute("aria-busy", "false");
  }
  function chartReady() {
    var sk = $("#chartSkeleton"), wrap = $("#chartWrap"), svg = $("#trendChart");
    if (sk) { sk.hidden = true; sk.innerHTML = ""; }
    if (wrap) wrap.classList.remove("has-state");
    if (svg) svg.setAttribute("aria-busy", "false");
  }

  function drawTrend(days) {
    var svg = $("#trendChart");
    if (!svg) return;
    var W = 720, H = 220, padL = 34, padB = 26, padT = 12;
    var max = 1;
    // 任一字段为 null/"" 时直接相加得 NaN，Math.max(1, NaN) = NaN → 整图静默空白
    days.forEach(function (d) {
      max = Math.max(max, num(d.ncm) + num(d.kg) + num(d.qq));
    });
    var bw = (W - padL - 8) / Math.max(days.length, 1);
    var scale = function (v) { return (H - padB - padT) * (v / max); };
    var parts = [];
    // 空数据时「近 0 天……」读着像故障：标题直接给「暂无数据」
    parts.push("<title id=\"trendA11y\">" + (days.length
      ? "近 " + days.length + " 天网易云 / 酷狗 / QQ 音乐调用次数堆叠柱状图，逐日数据见下方数据表"
      : "暂无数据") + "</title>");
    parts.push("<desc>" + days.map(function (d) {
      return esc(d.label) + " 网易云 " + num(d.ncm) + "、酷狗 " + num(d.kg) + "、QQ " + num(d.qq);
    }).join("；") + "</desc>");
    for (var g = 1; g <= 4; g++) {
      var gy = H - padB - (H - padB - padT) * g / 4;
      // 轴/网格颜色走 currentColor，由 .chart-wrap 的 color 传入主题 token，
      // 不再硬编码 #c9a8ba（深色主题下几乎不可见）
      parts.push('<line x1="' + padL + '" y1="' + gy + '" x2="' + (W - 4) + '" y2="' + gy +
        '" stroke="currentColor" stroke-opacity=".22" stroke-dasharray="3 6"/>');
      parts.push('<text x="' + (padL - 6) + '" y="' + (gy + 4) + '" text-anchor="end" font-size="9" fill="currentColor">' +
        fmtCount(Math.round(max * g / 4)) + "</text>");
    }
    days.forEach(function (d, i) {
      var x = padL + i * bw + bw * 0.18;
      var w = bw * 0.64;
      var hn = scale(num(d.ncm)), hk = scale(num(d.kg)), hq = scale(num(d.qq));
      var y = H - padB;
      if (hn > 0) { y -= hn; parts.push('<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + hn + '" rx="3" fill="var(--ncm)" opacity=".92"><title>网易云 ' + num(d.ncm) + "</title></rect>"); }
      if (hk > 0) { y -= hk; parts.push('<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + hk + '" rx="3" fill="var(--kg)" opacity=".92"><title>酷狗 ' + num(d.kg) + "</title></rect>"); }
      if (hq > 0) { y -= hq; parts.push('<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + hq + '" rx="3" fill="var(--qq)" opacity=".92"><title>QQ ' + num(d.qq) + "</title></rect>"); }
      parts.push('<text x="' + (x + w / 2) + '" y="' + (H - 8) + '" text-anchor="middle" font-size="9" fill="currentColor">' + esc(d.label) + "</text>");
    });
    svg.innerHTML = parts.join("");
    chartReady();
    // 键盘/读屏用户拿不到 <rect><title>（hover-only）：补一份 sr-only 数据表
    var old = $("#trendTable");
    if (old) old.remove();
    if (days.length) {
      var tbl = document.createElement("table");
      tbl.id = "trendTable";
      tbl.className = "sr-only";
      tbl.innerHTML = "<caption>近 " + days.length + " 天调用次数</caption><thead><tr><th scope=\"col\">日期</th>" +
        "<th scope=\"col\">网易云</th><th scope=\"col\">酷狗</th><th scope=\"col\">QQ</th></tr></thead><tbody>" +
        days.map(function (d) {
          return "<tr><th scope=\"row\">" + esc(d.label) + "</th><td>" + num(d.ncm) + "</td><td>" + num(d.kg) + "</td><td>" + num(d.qq) + "</td></tr>";
        }).join("") + "</tbody>";
      $("#chartWrap").appendChild(tbl);
    }
  }

  function drawActions(byAction) {
    var rows = [];
    Object.keys(byAction).forEach(function (src) {
      Object.keys(byAction[src] || {}).forEach(function (act) {
        rows.push({ src: src, act: act, n: num(byAction[src][act]) });
      });
    });
    rows.sort(function (a, b) { return b.n - a.n; });
    var box = $("#actionBars");
    if (!rows.length) {
      box.innerHTML = '<div class="empty-state">今天还没有调用</div>';
      return;
    }
    var max = rows[0].n || 1;
    var colors = { ncm: "var(--ncm)", kg: "var(--kg)", qq: "var(--qq)" };
    var shown = rows.slice(0, 8);
    box.innerHTML = shown.map(function (r, i) {
      return '<div class="bar-row" style="animation:rowPop .4s var(--bounce) both;animation-delay:' + i * 50 + 'ms">' +
        "<span>" + srcTagHtml(r.src, "src-tag") + " " + esc(ACT_NAMES[r.act] || r.act) + "</span>" +
        '<span class="bar-track"><span class="bar-fill" style="--bc:' + (colors[r.src] || "var(--blue)") + ';--w:0"></span></span>' +
        '<span class="bar-num">' + fmtCount(r.n) + "</span></div>";
    }).join("");
    // 先落 0 再在下一帧设目标值，触发 scaleX 过渡
    // （width 动画每帧触发布局，scaleX 走合成层）
    requestAnimationFrame(function () {
      $$(".bar-row", box).forEach(function (row, i) {
        var fill = row.querySelector(".bar-fill");
        if (fill && shown[i]) fill.style.setProperty("--w", (Math.max(0.04, shown[i].n / max)).toFixed(4));
      });
    });
  }

  function renderRecentTable(recent) {
    var tb = $("#recentTbl tbody");
    if (!recent.length) {
      tb.innerHTML = '<tr><td colspan="5"><div class="empty-state">暂无流水</div></td></tr>';
      return;
    }
    tb.innerHTML = recent.slice(0, 40).map(function (r) {
      return "<tr><td>" + fmtDateTime(r.ts) + "</td>" +
        "<td>" + srcTagHtml(r.source, "src-tag") + "</td>" +
        "<td>" + esc(ACT_NAMES[r.action] || r.action) + "</td>" +
        '<td><span class="badge ' + (r.ok ? "ok" : "err") + '">' + (r.ok ? "成功" : "失败") + "</span></td>" +
        '<td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(r.detail || "--") + "</td></tr>";
    }).join("");
  }

  /* ─────────── 黑白名单 ─────────── */
  var aclData = { mode: "off", blacklist: [], whitelist: [] };
  var _aclSeq = 0;
  var _aclSaving = false;
  async function loadAcl() {
    var seq = ++_aclSeq;
    withSkeleton("#blackList", skeletonRows(3, 44));
    withSkeleton("#whiteList", skeletonRows(3, 44));
    var d;
    try {
      d = await jget("/api/acl");
    } catch (e) {
      if (seq !== _aclSeq || (e && e.aborted)) return;
      renderError("#blackList", e.message);
      renderError("#whiteList", e.message);
      return;
    }
    if (seq !== _aclSeq) return; // 快速切面板时丢弃过期响应
    if (!d) { renderError("#blackList", "名单数据为空"); renderError("#whiteList", "名单数据为空"); return; }
    setLoaded("#blackList");
    setLoaded("#whiteList");
    aclData = d;
    $$("#aclModeSeg button").forEach(function (b) {
      var on = b.dataset.mode === d.mode;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    var hints = {
      off: "当前不限制任何用户/群，所有功能对所有人开放。",
      blacklist: "黑名单模式：名单内的用户或群无法使用点歌等功能。适合只屏蔽少数捣蛋用户。",
      whitelist: "白名单模式：仅名单内的用户或群可用。适合只开放给指定群使用。"
    };
    $("#aclHint").textContent = hints[d.mode] || "";
    renderList("#blackList", d.blacklist || []);
    renderList("#whiteList", d.whitelist || []);
    $("#blackCount").textContent = (d.blacklist || []).length + " 项";
    $("#whiteCount").textContent = (d.whitelist || []).length + " 项";
  }
  function renderList(sel, items) {
    var box = $(sel);
    if (!items.length) {
      box.innerHTML = '<div class="empty-state">暂无条目</div>';
      return;
    }
    box.innerHTML = items.map(function (it, i) {
      return '<div class="le-item"><span>' + esc(it) + '</span><button data-act="acl-del" data-list="' + sel.slice(1) + '" data-i="' + i + '" title="删除" aria-label="删除 ' + esc(it) + '">✕</button></div>';
    }).join("");
  }
  // 必须 return：调用方（事件委托层）靠返回的 promise 决定何时解 busy 锁。
  // handler 必须 return saveAcl(...)：否则 restore() 当场执行、按钮立刻可再点，
  // 连点用同一份旧快照互相覆盖 → 丢条目。
  function saveAcl(patch) {
    // set_acl 是整体覆盖语义，重入会丢数据。
    // resolve 值约定：true = 已保存成功，false = 失败 / 正在保存中
    // （aclAdd 靠它决定是否清空输入框；其余调用方忽略该值，行为不变）
    if (_aclSaving) { toast("正在保存，请稍候", "warn"); return Promise.resolve(false); }
    _aclSaving = true;
    // 先发合并副本，成功后才写回 aclData：提前 Object.assign 的话保存失败
    // 内存里会留下幽灵数据，下一次任意成功保存会把失败过的操作连带提交，
    // aclCopy 导出的也是没保存成功的脏数据
    var merged = Object.assign({}, aclData, patch);
    return jpost("/api/acl/save", merged).then(function (r) {
      _aclSaving = false;
      if (r.ok) { aclData = merged; toast("黑白名单已保存", "ok"); return loadAcl().then(function () { return true; }); }
      toast(r.error || "保存失败", "error");
      return false;
    }, function (e) {
      _aclSaving = false;
      toast(e.message, "error");
      return false;
    });
  }
  function aclAdd(kind, inputSel) {
    var input = $(inputSel);
    var v = input.value.trim();
    if (!v) { toast("请输入 QQ 号或群号", "warn"); return Promise.resolve(); }
    // 支持一次粘贴多条：逗号 / 空格 / 分号 / 换行分隔
    var parts = v.split(/[,，;；\s]+/).map(function (s) { return s.trim(); }).filter(function (s) { return s; });
    var items = (aclData[kind] || []).slice();
    var added = 0;
    parts.forEach(function (p) {
      if (!items.includes(p)) { items.push(p); added++; }
    });
    if (!added) { toast("条目都已存在", "warn"); return Promise.resolve(); }
    var patch = {}; patch[kind] = items;
    // 保存成功才清输入：失败 / 忙时保留，用户不用重新粘贴长名单
    return saveAcl(patch).then(function (ok) { if (ok) input.value = ""; });
  }
  function aclDel(key, idx) {
    var items = (aclData[key] || []).slice();
    items.splice(idx, 1);
    var patch = {}; patch[key] = items;
    return saveAcl(patch);
  }
  function aclCopy(kind) {
    var items = aclData[kind] || [];
    if (!items.length) { toast("列表是空的", "warn"); return; }
    var text = items.join("\n");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast("已复制 " + items.length + " 条到剪贴板", "ok"); },
        function () { toast("复制失败，请在列表里手动选中复制", "error"); });
    } else {
      toast("浏览器不支持剪贴板 API", "warn");
    }
  }

  /* ─────────── 配置 ─────────── */
  var cfgData = null;
  var _cfgSeq = 0;
  var CFG_BOOLS = [
    ["enable", "总开关", "关闭后点歌等功能静默"],
    ["enableSongRequest", "允许点歌", "点歌/听N/播放"],
    ["enableResolve", "链接解析", "自动识别分享链接"],
    ["resolveCards", "解析分享卡片", "解析 JSON 音乐卡片"],
    ["renderListCard", "图片卡片", "列表渲染为图片（需 Playwright）"],
    ["sendTextInfo", "歌曲识别信息", "发歌前发送文本说明"],
    ["sendVocal", "语音发送", "以语音消息发送"],
    ["uploadFile", "文件发送", "以群/好友文件发送"],
    ["disableHighQualityVocal", "语音降级", "PC QQ 兼容（16kHz 单声道）"],
    ["ffmpegCompress", "失败压缩重试", "发送失败时压成 mp3 重试"],
    ["sendNativeCard", "原生音乐卡片", "OneBot 平台官方小程序卡"],
    ["qqofficialAdapt", "QQ 官方适配", "合并消息节省配额"],
    ["qqofficialChunkedUpload", "官方分片上传", "QQ 官方通道发送超过 10MB 的文件时启用"]
  ];
  // 面板开关必须与配置 schema 一一对应（总开关等 5 项曾在面板里缺失，
  // 用户会误以为插件没关掉）。改配置分组时对照 _conf_schema.json 核对。
  var CORE_TOGGLES = ["enable", "enableSongRequest"];
  var FEATURE_TOGGLES = ["enableResolve", "resolveCards", "renderListCard"];
  async function loadConfig() {
    var seq = ++_cfgSeq;
    withSkeleton("#configBody", skeletonRows(4, 88));
    var d;
    try {
      d = await jget("/api/config");
    } catch (e) {
      if (seq !== _cfgSeq || (e && e.aborted)) return;
      renderError("#configBody", e.message);
      return;
    }
    if (seq !== _cfgSeq) return;
    setLoaded("#configBody");
    // 后端返回 200 但没有 config 字段时不能悄悄留着骨架屏：
    // renderConfig 会直接 return，用户永远看到 shimmer
    if (!d || !d.config) { renderError("#configBody", "配置数据为空，请检查插件端 /api/config 响应"); return; }
    cfgData = d.config;
    renderConfig();
  }
  function has(id) { return !!$("#cfg-" + id); }
  function bind(id) {
    var el = $("#cfg-" + id);
    return el ? el.value : "";
  }
  // 开关取值统一走这里：裸 $("#cfg-x").classList 无 null 保护，
  // 任一节点缺失即抛 TypeError，按钮连 pending 态都进不去
  function swVal(id) {
    var el = $("#cfg-" + id);
    return !!(el && el.classList.contains("on"));
  }
  // 合法 0 会被 `Number(x) || 默认值` 改写成默认值（maxList=0→10）
  function numOr(raw, dflt) {
    if (raw === "" || raw == null) return dflt;
    var n = Number(raw);
    return Number.isFinite(n) ? n : dflt;
  }
  function renderConfig() {
    var c = cfgData;
    var body = $("#configBody");
    if (!c) return;
    // 分组缺键兜底空对象（同下方 scheduler 的写法）：配置缺某一分组时
    // c.ncm.apiBase 这类访问会直接抛 TypeError，整个配置面板渲染不出来
    c.ncm = c.ncm || {};
    c.kg = c.kg || {};
    c.qq = c.qq || {};
    c.webui = c.webui || {};
    var html = "";

    // count 由调用处显式传入：按 inner 里 "cfg-item" 出现次数统计，
    // label 含该字面量就会算错
    function group(title, inner, count) {
      return '<details class="cfg-group" open><summary><span class="arrow" aria-hidden="true">▶</span>' + esc(title) +
        '<span class="cfg-count">' + (count != null ? count : 0) + " 项</span></summary>" +
        '<div class="cfg-items">' + inner + "</div></details>";
    }
    function textItem(id, label, desc, value, type, extra) {
      return '<div class="cfg-item"><label for="cfg-' + esc(id) + '">' + esc(label) + "</label>" +
        '<input class="input" id="cfg-' + esc(id) + '" type="' + (type || "text") + '"' +
        (extra || "") + ' value="' + esc(value == null ? "" : value) + '" />' +
        (desc ? '<div class="desc">' + esc(desc) + "</div>" : "") + "</div>";
    }
    // role="switch" 用真正的 button + aria-labelledby，读屏会念「标签名，开关，已打开/关闭」
    function boolItem(id, label, desc, value) {
      return '<div class="cfg-item"><div class="sw-row">' +
        '<span id="lbl-' + esc(id) + '">' + esc(label) + "</span>" +
        '<button type="button" class="sw' + (value ? " on" : "") + '" id="cfg-' + esc(id) +
        '" role="switch" aria-checked="' + (value ? "true" : "false") + '" aria-labelledby="lbl-' + esc(id) + '"></button>' +
        "</div>" +
        (desc ? '<div class="desc">' + esc(desc) + "</div>" : "") + "</div>";
    }
    function selectItem(id, label, desc, value, options) {
      return '<div class="cfg-item"><label for="cfg-' + esc(id) + '">' + esc(label) + "</label>" +
        '<select class="input" id="cfg-' + esc(id) + '">' +
        options.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (o[0] === value ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join("") +
        "</select>" + (desc ? '<div class="desc">' + esc(desc) + "</div>" : "") + "</div>";
    }
    function staticItem(label, text) {
      return '<div class="cfg-item"><span class="cfg-label">' + esc(label) + '</span><div class="desc">' + esc(text) + "</div></div>";
    }

    /* 基础组：总开关 / 允许点歌放最上面 */
    var general = "";
    var generalCount = 0;
    general += boolItem("enable", "总开关", "关闭后点歌等功能静默", c.enable); generalCount++;
    general += boolItem("enableSongRequest", "允许点歌", "点歌 / 听N / 播放", c.enableSongRequest); generalCount++;
    general += selectItem("defaultSource", "默认音源", "聚合模式下决定同名歌曲的首选版本与版本排序", c.defaultSource,
      [["auto", "聚合（三平台混搜）"], ["ncm", "网易云音乐"], ["kg", "酷狗音乐"], ["qq", "QQ音乐"]]); generalCount++;
    general += textItem("maxList", "点歌列表数量", "1-20", c.maxList, "number", ' min="1" max="20" step="1"'); generalCount++;
    general += textItem("identifyPrefix", "识别信息前缀", "", c.identifyPrefix); generalCount++;
    general += textItem("cooldownSec", "点歌冷却 (秒)", "同群两次点歌最小间隔，0 = 关闭", c.cooldownSec, "number", ' min="0" step="1"'); generalCount++;
    general += textItem("rateLimitMs", "音源限速 (ms)", "每请求最小间隔，防突发风控，0 = 关闭", c.rateLimitMs, "number", ' min="0" step="100"'); generalCount++;
    html += group("基础", general, generalCount);

    /* 功能开关：解析 / 卡片 / 图片渲染 */
    var feat = "";
    FEATURE_TOGGLES.forEach(function (id) {
      var spec = CFG_BOOLS.filter(function (s) { return s[0] === id; })[0];
      if (spec) feat += boolItem(spec[0], spec[1], spec[2], c[spec[0]]);
    });
    html += group("功能开关", feat, FEATURE_TOGGLES.length);

    var srcQ = [["auto", "自动"], ["standard", "标准"], ["higher", "较高"], ["exhigh", "极高"], ["lossless", "无损"], ["hires", "Hi-Res"], ["jyeffect", "高清臻音"], ["sky", "沉浸环绕"], ["dolby", "杜比"], ["jymaster", "超清母带"]];
    var kgQ = [["auto", "自动"], ["128", "标准"], ["320", "HQ"], ["flac", "无损"], ["high", "Hi-Res"], ["super", "Super"], ["viper_clear", "蝰蛇超清"], ["viper_tape", "蝰蛇母带"]];
    var qqQ = [["auto", "自动"], ["128", "标准"], ["320", "HQ"], ["flac", "无损"], ["atmos", "全景声"], ["master", "臻品母带"], ["atmos_db", "杜比"]];

    var ncmHtml = textItem("ncm-apiBase", "API 地址", "网易云 API 服务，默认已填公共实例；自建默认端口 3000", c.ncm.apiBase);
    ncmHtml += selectItem("ncm-quality", "最高音质", "", c.ncm.quality, srcQ);
    ncmHtml += boolItem("ncm-qualityUnblock", "VIP 歌自动解灰", "拿不到 URL 时用 unblock 音源兜底", c.ncm.qualityUnblock);
    ncmHtml += staticItem("登录状态", c.ncm.hasCookie ? "已有 Cookie" : "匿名（可在「账号扫码」登录）");
    html += group("网易云音乐", ncmHtml, 4);

    var kgHtml = textItem("kg-apiBase", "API 地址", "酷狗 API 服务，本机自建默认端口 4000", c.kg.apiBase);
    kgHtml += selectItem("kg-quality", "最高音质", "", c.kg.quality, kgQ);
    kgHtml += boolItem("kg-trialFallback", "VIP 歌发试听", "未登录时发 60 秒试听", c.kg.trialFallback);
    kgHtml += staticItem("登录状态", c.kg.hasCookie ? "已有 Cookie" : "匿名（搜索需登录，请先「账号扫码」）");
    html += group("酷狗音乐", kgHtml, 4);

    var qqHtml = selectItem("qq-quality", "最高音质", "", c.qq.quality, qqQ);
    qqHtml += boolItem("qq-trialFallback", "VIP 歌发试听", "匿名时发试听片段", c.qq.trialFallback);
    qqHtml += staticItem("登录状态", c.qq.hasCredential ? "已有凭证" : "匿名（免费歌可完整播放）");
    html += group("QQ音乐（内置直连，无需 API 服务）", qqHtml, 3);

    var delivery = "";
    var deliveryCount = 0;
    CFG_BOOLS.forEach(function (spec) {
      if (CORE_TOGGLES.indexOf(spec[0]) >= 0 || FEATURE_TOGGLES.indexOf(spec[0]) >= 0) return;
      delivery += boolItem(spec[0], spec[1], spec[2], c[spec[0]]);
      deliveryCount++;
    });
    delivery += textItem("compressBitrate", "压缩码率 (kbps)", "0 = 不压缩", c.compressBitrate, "number", ' min="0" max="320" step="8"'); deliveryCount++;
    delivery += textItem("downloadTimeout", "下载超时 (ms)", "", c.downloadTimeout, "number", ' min="1000" step="1000"'); deliveryCount++;
    delivery += textItem("keepFileSec", "临时文件保留 (秒)", "", c.keepFileSec, "number", ' min="0" step="10"'); deliveryCount++;
    html += group("发送通道（多平台适配）", delivery, deliveryCount);

    var webuiHtml = boolItem("webui-enable", "启用 WebUI", "", c.webui.enable);
    webuiHtml += textItem("webui-host", "监听地址", "0.0.0.0 允许局域网访问", c.webui.host);
    webuiHtml += textItem("webui-port", "端口", "改动需重载插件生效", c.webui.port, "number", ' min="1" max="65535" step="1"');
    html += group("WebUI 面板", webuiHtml, 3);

    var sched = c.scheduler || {};
    var schedHtml = boolItem("sched-enable", "启用定时任务", "每日签到 / 凭证保活 / 订阅推送", sched.enable);
    schedHtml += textItem("sched-signinHour", "执行时刻 (小时)", "0-23，分钟随机错峰", sched.signinHour, "number", ' min="0" max="23" step="1"');
    schedHtml += boolItem("sched-ncmSignin", "网易云每日签到", "需已登录网易云", sched.ncmSignin);
    schedHtml += boolItem("sched-qqRefresh", "QQ 凭证保活", "每日刷新降低掉线概率", sched.qqRefresh);
    html += group("定时任务与订阅推送", schedHtml, 4);

    body.innerHTML = html;
    // 开关是 <button>，原生即可回车/空格触发；只同步 aria-checked
    $$(".sw", body).forEach(function (sw) {
      sw.addEventListener("click", function () {
        sw.classList.toggle("on");
        sw.setAttribute("aria-checked", sw.classList.contains("on") ? "true" : "false");
      });
    });
  }
  function collectConfig() {
    var patch = {};
    patch.defaultSource = bind("defaultSource");
    patch.maxList = numOr(bind("maxList"), 10);
    patch.identifyPrefix = bind("identifyPrefix");
    patch.cooldownSec = numOr(bind("cooldownSec"), 0);
    patch.rateLimitMs = numOr(bind("rateLimitMs"), 0);
    patch.ncm = { apiBase: bind("ncm-apiBase"), quality: bind("ncm-quality"), qualityUnblock: swVal("ncm-qualityUnblock") };
    patch.kg = { apiBase: bind("kg-apiBase"), quality: bind("kg-quality"), trialFallback: swVal("kg-trialFallback") };
    patch.qq = { quality: bind("qq-quality"), trialFallback: swVal("qq-trialFallback") };
    CFG_BOOLS.forEach(function (spec) {
      if (has(spec[0])) patch[spec[0]] = swVal(spec[0]);
    });
    patch.compressBitrate = numOr(bind("compressBitrate"), 128);
    patch.downloadTimeout = numOr(bind("downloadTimeout"), 120000);
    patch.keepFileSec = numOr(bind("keepFileSec"), 60);
    patch.webui = { enable: swVal("webui-enable"), host: bind("webui-host"), port: numOr(bind("webui-port"), 17818) };
    patch.scheduler = {
      enable: swVal("sched-enable"),
      signinHour: numOr(bind("sched-signinHour"), 0),
      ncmSignin: swVal("sched-ncmSignin"),
      qqRefresh: swVal("sched-qqRefresh")
    };
    return patch;
  }
  function syncConfigView(patch) {
    // 原地更新受影响字段，不整块重渲染 #configBody：
    // 保存后原地更新、不做全量重渲染：其它分组未保存的编辑不能丢、焦点不能飞
    Object.keys(patch).forEach(function (k) {
      var v = patch[k];
      if (v && typeof v === "object") { Object.assign(cfgData[k] || (cfgData[k] = {}), v); }
      else cfgData[k] = v;
    });
  }
  async function saveConfig() {
    if (!cfgData) return;
    var patch = collectConfig();
    var btn = $('[data-act="cfg-save"]');
    if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }
    try {
      var r = await jpost("/api/config/save", { config: patch });
      if (r.rejected && r.rejected.length) {
        // 有被拒项时本地 patch 不等于实际生效值：原地同步会把「没存进去的值」
        // 留在面板上误导用户，必须重拉服务端真实配置回填
        toast("部分项被拒绝：" + r.rejected.join(", "), "warn");
        var fresh = await jget("/api/config");
        if (fresh && fresh.config) { cfgData = fresh.config; renderConfig(); }
      } else {
        toast("配置已保存" + (r.note ? " " + r.note : ""), "ok");
        syncConfigView(patch);
      }
    } catch (e) {
      toast(e.message, "error");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "保存全部"; }
    }
  }

  /* ─────────── 链接解析工具 ─────────── */
  var _resolveCtrl = null;
  async function doResolve() {
    var text = $("#resolveInput").value.trim();
    if (!text) { toast("先粘贴链接或口令文本", "warn"); return; }
    var box = $("#resolveResult");
    if (_resolveCtrl) _resolveCtrl.abort();
    var ctrl = new AbortController();
    _resolveCtrl = ctrl;
    box.hidden = false;
    box.innerHTML = '<div class="skeleton" aria-hidden="true" style="height:80px"></div>';
    try {
      var r = await jpost("/api/resolve", { text: text });
      var srcName = SRC_NAMES[r.source] || "未知音源";
      var hits = (r.matched || []).map(function (s) { return SRC_NAMES[s] || s; }).join(" / ");
      box.innerHTML =
        '<span class="rr-badge rr-src">' + esc(srcName) + "</span>" +
        (r.type ? '<span class="rr-badge rr-type">' + esc(r.type) + "</span>" : "") +
        (r.playable ? '<span class="rr-badge rr-play">可自动点歌 ✓</span>' : "") +
        '<div class="rr-name">' + (r.name ? esc(r.name) : "未能解析出具体内容") + "</div>" +
        '<div class="rr-sub">识别到的平台特征：' + esc(hits || "无") + (r.error ? "<br>⚠ " + esc(r.error) : "") +
        "<br>展开后链接：" + esc(r.expanded || "") + "</div>";
    } catch (e) {
      if (e && e.aborted) return;
      box.innerHTML = '<div class="rr-name">解析失败</div><div class="rr-sub">' + esc(e.message) + "</div>";
    }
  }

  /* ─────────── 会话 ─────────── */
  var _ssSeq = 0;
  async function loadSessions() {
    var seq = ++_ssSeq;
    withSkeleton("#sessTbl tbody", '<tr><td colspan="4"><div class="skeleton" aria-hidden="true" style="height:60px"></div></td></tr>');
    var d;
    try {
      d = await jget("/api/auth/sessions");
    } catch (e) {
      if (seq !== _ssSeq || (e && e.aborted)) return;
      renderError("#sessTbl tbody", e.message);
      return;
    }
    if (seq !== _ssSeq) return;
    if (!d) { renderError("#sessTbl tbody", "会话数据为空"); return; }
    setLoaded("#sessTbl tbody");
    var tb = $("#sessTbl tbody");
    if (!(d.sessions || []).length) {
      tb.innerHTML = '<tr><td colspan="4"><div class="empty-state">暂无其他会话</div></td></tr>';
      return;
    }
    tb.innerHTML = d.sessions.map(function (s) {
      return "<tr><td>" + esc(s.id) + (s.current ? ' <span class="badge ok">当前</span>' : "") + "</td>" +
        "<td>" + fmtDateTime(s.created) + "</td>" +
        "<td>" + fmtDateTime(s.lastSeen) + "</td>" +
        "<td>" + (s.current ? "--" : '<button class="btn btn-ghost btn-sm" data-act="sess-revoke" data-id="' + esc(s.id) + '">下线</button>') + "</td></tr>";
    }).join("");
  }

  /* ─────────── 运行日志 ─────────── */
  var _logSeq = 0;
  var _logTimer = null;
  var _logBusy = false;
  var _clearedAt = 0;   // 清屏时刻（秒），之后过滤更早条目
  function logLine(e) {
    var cls = e.level === "ERROR" ? "err" : e.level === "WARNING" ? "warn" : "info";
    // 后端存的是结构化字段：级别徽标 / 时间 / 正文分开渲染，
    // ts（秒级 unix）同时是「清屏」过滤的判据
    return '<div class="log-line log-' + cls + '"><span class="log-lv">' + esc(e.level) + "</span>" +
      '<span class="log-ts">' + fmtTime(e.ts) + "</span>" + esc(e.msg) + "</div>";
  }
  function renderLogEntries(entries) {
    var box = $("#logConsole");
    if (!entries.length) {
      box.innerHTML = '<div class="empty-state">暂无日志</div>';
      return;
    }
    box.innerHTML = entries.map(logLine).join("");
    box.scrollTop = box.scrollHeight;
  }
  var LOG_MAX = 400;   // DOM 节点数封顶：面板开久了 insertAdjacentHTML 会累积上万节点
  function appendLogEntries(entries) {
    var box = $("#logConsole");
    // 清屏后不立刻被 2.5s 后的新日志覆盖掉「已清屏」提示：过滤更早条目
    entries = entries.filter(function (e) { return num(e.ts) >= _clearedAt; });
    // 先判空再清理：全被过滤时直接退，否则 empty-state 被抹掉后控制台变无提示黑屏
    if (!entries.length) return;
    if (box.querySelector(".empty-state")) box.innerHTML = "";
    // 用户上滑查看历史时不要抢滚动
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    var wrap = document.createElement("div");
    wrap.innerHTML = entries.map(logLine).join("");
    while (wrap.firstChild) box.appendChild(wrap.firstChild);
    var over = box.childElementCount - LOG_MAX;
    while (over-- > 0 && box.firstElementChild) box.removeChild(box.firstElementChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
  function stopLogsPoll() {
    if (_logTimer) { clearTimeout(_logTimer); _logTimer = null; }
    _logBusy = false;
  }
  async function loadLogsPanel() {
    stopLogsPoll();
    _clearedAt = 0;
    $("#logConsole").innerHTML = '<div class="skeleton" aria-hidden="true" style="height:60px"></div>';
    try {
      var d = await jget("/api/logs");
      // 服务端重启后 seq 归零：检测回退并全量重取，
      // 否则 after=<旧大值> 永远等不到新日志
      if (d.seq < _logSeq) {
        _logSeq = 0;
        var full = await jget("/api/logs");
        _logSeq = num(full.seq);
        renderLogEntries(full.entries || []);
      } else {
        _logSeq = num(d.seq);
        renderLogEntries(d.entries || []);
      }
    } catch (e) {
      // 与其它面板 loader 一致：失败渲染错误态 + 重试按钮，别让骨架屏长亮
      if (!(e && e.aborted)) renderError("#logConsole", e.message);
    }
    scheduleLogPoll();
  }
  // 递归 setTimeout 而非 setInterval：interval 会与上一次请求重叠，
  // 没有 in-flight 守卫时同一批日志可能被追加两次
  function scheduleLogPoll() {
    if (_logTimer) clearTimeout(_logTimer);
    _logTimer = setTimeout(pollLogsOnce, 2500);
  }
  async function pollLogsOnce() {
    _logTimer = null;
    // 只有真的离开日志面板才停表。隐藏页签 / 登录遮罩 / 上一轮未完这三个早退
    // 必须照常排下一轮：直接 return 会杀掉自递归定时器，切回页签后日志永久停更
    if (curPanel !== "logs") return;
    if (document.hidden || !$("#loginMask").hidden || _logBusy) { scheduleLogPoll(); return; }
    _logBusy = true;
    try {
      var d2 = await jget("/api/logs?after=" + _logSeq);
      if (num(d2.seq) < _logSeq) {
        // seq 回退 = 服务端重启，必须全量重取
        _logSeq = 0;
        var full = await jget("/api/logs");
        _logSeq = num(full.seq);
        renderLogEntries(full.entries || []);
      } else {
        _logSeq = num(d2.seq);
        if ((d2.entries || []).length) appendLogEntries(d2.entries);
      }
    } catch (e) { /* 下轮重试 */ }
    _logBusy = false;
    if (curPanel === "logs") scheduleLogPoll();
  }

  /* ─────────── 确认对话框（替代原生 confirm） ─────────── */
  var _confirmPrevFocus = null;
  var _confirmResolve = null;
  function confirmBox(opts) {
    return new Promise(function (resolve) {
      var mask = $("#confirmMask");
      var dlg = $("#confirmDialog");
      $("#confirmTitle").textContent = opts.title || "请确认";
      var txt = $("#confirmText");
      txt.textContent = opts.text || "";
      txt.className = "confirm-text" + (opts.danger ? " danger" : "");
      $("#confirmOk").textContent = opts.okText || "确定";
      _confirmPrevFocus = document.activeElement;
      _confirmResolve = resolve;
      mask.hidden = false;
      if (dlg) dlg.focus();
    });
  }
  function closeConfirm(ok) {
    var mask = $("#confirmMask");
    if (mask.hidden) return;
    mask.hidden = true;
    var r = _confirmResolve;
    _confirmResolve = null;
    if (_confirmPrevFocus && _confirmPrevFocus.focus) _confirmPrevFocus.focus();
    _confirmPrevFocus = null;
    if (r) r(ok);
  }
  $("#confirmOk").addEventListener("click", function () { closeConfirm(true); });
  $("#confirmCancel").addEventListener("click", function () { closeConfirm(false); });
  $("#confirmMask").addEventListener("click", function (e) {
    if (e.target === this) closeConfirm(false);
  });

  /* ─────────── 事件委托 ─────────── */
  // 统一 busy-restore 的豁免名单（modal-x 走 class 判断，这两类走 action 判断）：
  // 按钮文案本身就是状态——试听写「⏸ 播放中」，promise 结束后被 restore 覆盖回
  // 「▶ 试听」会把播放态冲掉；投递有自己的防重锁与结果提示
  var BUSY_SELF_MANAGED = { preview: true, deliver: true };
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-act]");
    if (!t) return;
    var act = t.dataset.act;
    var actMap = {
      "theme": function () { cycleTheme(); },
      "more-toggle": function () { moreOpen ? closeMore() : openMore(); },
      "logout": async function () {
        // 登出必须停掉所有后台活动：否则音频继续播、二维码仍在轮询 /api/qr/status
        var ok = await confirmBox({ title: "退出登录", text: "退出后需要重新输入管理密码才能回到面板。", okText: "退出" });
        if (!ok) return;
        // 先清干净现场（关弹窗会归还焦点），再通知服务端
        stopPreview();
        abortPreview();
        closeQR();
        stopLogsPoll();
        clearInterval(overviewTimer);
        clearToasts();
        try { await jpost("/api/auth/logout", {}); } catch (err) { /* 401 也算已登出 */ }
        authGen++;
        // showLogin 前先 hideLogin()：清掉 #loginPwd 已输入的密码与 #loginErr 残留，
        // 下次登录不能看到上次的错误
        hideLogin();
        showLogin();
      },
      "svc-check": async function () {
        // 不自己改文案：外层委托已经统一处理 data-busy（文案透明 + 转圈）
        try {
          var r = await jget("/api/service/check");
          var lines = [];
          var bad = 0;
          ["ncm", "kg", "qq"].forEach(function (s) {
            var item = r[s] || {};
            // ok 是结构化字段，直接用它计数。渲染后的字符串行首恒是「网易云：」
            // 这类音源名前缀，拿「✗」做 indexOf 前缀匹配永远打不中，全挂也会弹 ok 色
            if (!item.ok) bad++;
            lines.push(SRC_NAMES[s] + "：" + (item.ok ? "✓ " : "✗ ") + (item.msg || ""));
          });
          // 合成一条 toast：一次发 3 条会互相挤掉，且读屏连播三段
          toast(lines.join("；"), bad ? "warn" : "ok");
          loadOverview();
        } catch (err) { toast(err.message, "error"); }
      },
      "do-search": doSearch,
      "do-resolve": doResolve,
      // 错误态里的「重试」：按当前面板重新走对应 loader
      "panel-retry": function () {
        var loaders = { overview: loadOverview, accounts: loadAccounts, stats: loadStats,
          acl: loadAcl, config: loadConfig, sessions: loadSessions, logs: loadLogsPanel };
        var fn = loaders[curPanel];
        if (fn) fn().catch(function (e) { if (e && !e.aborted) toast(e.message, "error"); });
      },
      "preview": function () { playPreview(t.dataset.source, t.dataset.sid, t); },
      "deliver": function () { deliverPlay(t.dataset.source, t.dataset.sid, t); },
      "remote-refresh": loadScopes,
      "history-refresh": function () { loadHistory().catch(function () {}); },
      "logs-clear": function () {
        // 记时刻而不是只清 DOM：不停轮询的话 2.5s 后新日志会立刻覆盖「已清屏」
        _clearedAt = Math.floor(Date.now() / 1000);
        var box = $("#logConsole");
        // 只同步 seq，失败无碍：本地已清屏，下一轮轮询会照常带回新日志
        if (_logSeq) { jget("/api/logs?after=" + _logSeq).then(function (d) { _logSeq = num(d.seq); }).catch(function () {}); }
        box.innerHTML = '<div class="empty-state">已清屏（仅本地显示）</div>';
      },
      "acl-copy-black": function () { aclCopy("blacklist"); },
      "acl-copy-white": function () { aclCopy("whitelist"); },
      "qr": function () { openQR(t.dataset.src); },
      "qr-close": closeQR,
      "qr-cancel": closeQR,
      "qr-refresh": async function () {
        if (qrSource) { closeQR(); openQR(qrSource); }
      },
      "acct-logout": async function () {
        var src = t.dataset.src;
        var ok = await confirmBox({ title: "退出 " + SRC_NAMES[src] + " 登录", text: "该平台的凭证会从插件中删除，需要重新扫码。", okText: "退出登录", danger: true });
        if (!ok) return;
        try { await jpost("/api/accounts/logout", { source: src }); toast("已退出登录", "ok"); loadAccounts(); loadOverview(); }
        catch (e) { toast(e.message, "error"); }
      },
      "acct-refresh": async function () {
        try {
          var r = await jpost("/api/accounts/refresh", { source: t.dataset.src });
          toast(r.msg || (r.ok ? "刷新成功" : "刷新失败"), r.ok ? "ok" : "warn");
          loadAccounts();
        } catch (e) { toast(e.message, "error"); }
      },
      "cookie-save": async function () {
        var src = $("#cookieSource").value;
        var value = $("#cookieValue").value.trim();
        if (!value) { toast("请粘贴凭证内容", "warn"); return; }
        try {
          await jpost("/api/accounts/cookie", { source: src, value: value });
          $("#cookieValue").value = "";
          toast("凭证已导入", "ok");
          loadAccounts();
        } catch (e) { toast(e.message, "error"); }
      },
      "stats-reset": async function () {
        var ok = await confirmBox({ title: "清空每日统计", text: "将清空近 14 天的每日统计（累计总数保留）。此操作不可撤销。", okText: "清空", danger: true });
        if (!ok) return;
        try { await jpost("/api/stats/reset", { keepTotals: true }); toast("统计已清空", "ok"); loadStats(); loadOverview(); }
        catch (e) { toast(e.message, "error"); }
      },
      // 必须 return saveAcl(...)：否则 restore() 立刻执行、按钮当场可再点
      "acl-add-black": function () { return aclAdd("blacklist", "#blackInput"); },
      "acl-add-white": function () { return aclAdd("whitelist", "#whiteInput"); },
      "acl-del": function () {
        var key = t.dataset.list === "blackList" ? "blacklist" : "whitelist";
        return aclDel(key, Number(t.dataset.i));
      },
      "cfg-save": saveConfig,
      "sess-revoke": async function () {
        try { await jpost("/api/auth/sessions/revoke", { id: t.dataset.id }); toast("会话已下线", "ok"); loadSessions(); }
        catch (e) { toast(e.message, "error"); }
      },
      "revoke-others": async function () {
        var ok = await confirmBox({ title: "下线其他会话", text: "除当前浏览器外的所有登录会话都会立即失效。", okText: "全部下线", danger: true });
        if (!ok) return;
        try { await jpost("/api/auth/sessions/revoke", { id: "*" }); toast("其他会话已下线", "ok"); loadSessions(); }
        catch (e) { toast(e.message, "error"); }
      },
      "pwd-change": async function () {
        var oldV = $("#oldPwd").value, newV = $("#newPwd").value;
        if (!newV || newV.length < 6) { toast("新密码至少 6 位", "warn"); return; }
        try {
          await jpost("/api/auth/change-password", { old: oldV, password: newV });
          $("#oldPwd").value = ""; $("#newPwd").value = "";
          toast("密码已修改，其他会话已下线", "ok");
        } catch (e) { toast(e.message, "error"); }
      }
    };
    if (actMap[act]) {
      e.preventDefault();
      // 统一防重复提交：连点会并发打向上游（搜索）或用旧快照覆盖新数据
      // （黑白名单 set_acl 是整体覆盖语义），这里做幂等锁 + 转圈反馈
      if (t.dataset.busy) return;
      var restore = null;
      // 「×」「✕」这类窄图标按钮不能写「处理中…」（会撑破 32~44px 的方块）：
      // modal-x 关闭钮、bn-item 底栏项、黑白名单 le-item 里的 32px 删除钮
      // （无 .btn 类，转圈样式不生效；防连点由 saveAcl 的 _aclSaving 锁兜底）
      if (t.tagName === "BUTTON" && t.textContent.trim() && !t.classList.contains("modal-x") &&
          !t.classList.contains("bn-item") && !t.closest(".le-item") && !BUSY_SELF_MANAGED[act]) {
        t.dataset.busy = "1";
        t.dataset.busyText = t.textContent;
        t.textContent = "处理中…";
        restore = function () {
          delete t.dataset.busy;
          t.textContent = t.dataset.busyText || "处理中…";
          delete t.dataset.busyText;
        };
      }
      var r;
      try {
        r = actMap[act]();
      } catch (err) {
        if (restore) restore();
        toast(err.message || "操作失败", "error");
        return;
      }
      if (r && typeof r.then === "function") {
        r.then(function () { if (restore) restore(); },
               function () { if (restore) restore(); });
      } else if (restore) {
        restore();
      }
    }
  });
  $("#aclModeSeg").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-mode]");
    if (!b || b.dataset.mode === aclData.mode) return;
    saveAcl({ mode: b.dataset.mode });
  });
  $("#blackInput").addEventListener("keydown", function (e) { if (e.key === "Enter") aclAdd("blacklist", "#blackInput"); });
  $("#whiteInput").addEventListener("keydown", function (e) { if (e.key === "Enter") aclAdd("whitelist", "#whiteInput"); });
  $("#resolveInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) doResolve();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (!$("#confirmMask").hidden) { closeConfirm(false); return; }
      if (!$("#qrMask").hidden) { closeQR(); return; }
      if (moreOpen) { closeMore(); return; }
      return;
    }
    // 焦点陷阱：弹窗打开时 Tab 在弹窗内循环，否则键盘用户会 Tab 到背景内容上。
    // 原 $$ 只接受 1 个参数，这里传的 #qrDialog 被静默丢弃 → 扫全文档 → 不回绕。
    if (e.key !== "Tab") return;
    var dlgSel = $("#qrMask").hidden ? (!$("#confirmMask").hidden ? "#confirmDialog" : null) : "#qrDialog";
    if (!dlgSel) return;
    var f = $$(FOCUSABLE, $(dlgSel));
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || !$(dlgSel).contains(document.activeElement))) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  });

  /* ─────────── 轮询 ─────────── */
  var overviewTimer = null;
  function startOverviewPoll() {
    clearInterval(overviewTimer);
    overviewTimer = setInterval(function () {
      if (document.hidden || !$("#loginMask").hidden) return;
      if (curPanel === "overview") loadOverview(true).catch(function () {});
    }, 10000);
  }
  // 统一清理所有定时器：overviewTimer / qrTimer / _logTimer / _focusTimer
  // 分散在各处，必须有一个地方能一次停干净
  function stopAllPolls() {
    clearInterval(overviewTimer);
    overviewTimer = null;
    stopLogsPoll();
    stopPreview();
    abortPreview();
    if (_searchCtrl) { _searchCtrl.abort(); _searchCtrl = null; }
    if (_resolveCtrl) { _resolveCtrl.abort(); _resolveCtrl = null; }
    clearTimeout(_debounceTimer);
    _debounceTimer = null;
    clearTimeout(_focusTimer);
    _focusTimer = null;
    closeQR();
  }

  /* ─────────── 启动 ─────────── */
  function boot() {
    makePetals();
    renderNav();
    measureScroll();
    startOverviewPoll();
    // 必须重跑「当前」面板：token 过期后重新登录时，停在配置/统计面板的
    // 用户不能一直看着旧骨架屏（那些 loader 根本没被再调用）
    switchPanel(curPanel);
  }

  async function init() {
    makePetals();
    renderNav();
    measureScroll();
    try {
      var meta = await jget("/api/meta");
      $("#chipVersion").innerHTML = '<span class="dot"></span>v' + esc(meta.version);
      if (!meta.authed) { showLogin(); return; }
      boot();
    } catch (e) {
      // 断连与未登录必须可区分。toast 要放在 showLogin 之后入队：showLogin 内部
      // 会 clearToasts()，先弹的会被自己清掉；toastBox(z:500) 在登录遮罩(z:200)
      // 之外且层级更高，登录框上能正常看到这条错误
      showLogin();
      toast("无法连接面板服务：" + e.message, "error");
    }
  }
  window.addEventListener("beforeunload", stopAllPolls);
  init();
})();
