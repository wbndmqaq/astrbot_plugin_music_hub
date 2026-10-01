/* Music Hub 管理面板（原生 JS 单页应用，无构建依赖） */
(function () {
  "use strict";

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtCount(n) {
    n = Number(n) || 0;
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
  var REDUCED = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var ACT_NAMES = { search: "搜索", play: "点歌", url: "取流", lyric: "歌词", detail: "详情", comment: "评论", mv: "MV", rank: "排行榜", playlist: "歌单", explore: "浏览", login: "登录", resolve: "解析", other: "其他" };
  var SRC_NAMES = { ncm: "网易云", kg: "酷狗", qq: "QQ音乐" };
  var SRC_MARK = { ncm: "网", kg: "狗", qq: "Q" };

  /* ─────────── Toast ─────────── */
  function toast(msg, cls) {
    var box = $("#toastBox");
    var el = document.createElement("div");
    el.className = "toast-msg toast-" + (cls || "ok");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () { el.classList.add("toast-hide"); }, 2600);
    setTimeout(function () { el.remove(); }, 3000);
  }

  /* ─────────── API ─────────── */
  var authGen = 0;
  async function api(url, opt) {
    opt = opt || {};
    var gen = authGen;
    var r = await fetch(url, opt);
    if (!r.ok) {
      var b = {};
      try { b = await r.json(); } catch (e) {}
      if (r.status === 401 && gen === authGen) showLogin();
      var err = new Error(b.error || ("HTTP " + r.status));
      err.status = r.status;
      throw err;
    }
    var ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
  }
  function jget(u) { return api(u); }
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
    if (REDUCED) return;
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

  /* ─────────── 滚动进度 ─────────── */
  var ticking = false;
  window.addEventListener("scroll", function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var el = $("#scrollProgress");
      var h = document.documentElement.scrollHeight - window.innerHeight;
      var p = h > 0 ? window.scrollY / h : 0;
      el.style.transform = "scaleX(" + p.toFixed(4) + ")";
      ticking = false;
    });
  }, { passive: true });

  /* ─────────── countUp ─────────── */
  function countUp(el, target) {
    if (REDUCED) { el.textContent = fmtCount(target); return; }
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
    overview: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3.5" y="3.5" width="7.5" height="7.5" rx="2.5"/><rect x="13" y="3.5" width="7.5" height="7.5" rx="2.5"/><rect x="3.5" y="13" width="7.5" height="7.5" rx="2.5"/><rect x="13" y="13" width="7.5" height="7.5" rx="2.5"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l5 5"/></svg>',
    accounts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20c1.2-3.6 4-5.4 7.5-5.4S18.3 16.4 19.5 20"/></svg>',
    stats: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 20V10M10 20V4M16 20v-7M21 20H3.5"/></svg>',
    acl: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l7.5 3v5.4c0 4.6-3 8-7.5 9.6-4.5-1.6-7.5-5-7.5-9.6V6z"/><path d="M9 12l2.2 2.2L15.4 10"/></svg>',
    config: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.8 1.8M16.7 16.7l1.8 1.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8"/></svg>',
    resolve: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>',
    sessions: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="10.5" width="16" height="10" rx="3"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/></svg>',
    logs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 5.5h16M4 10h11M4 14.5h13M4 19h7"/></svg>'
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
  var curPanel = "overview";

  function renderNav() {
    var side = $("#sideList"), bottom = $("#bottomNav");
    side.innerHTML = "";
    bottom.innerHTML = "";
    NAV_ITEMS.forEach(function (item) {
      var b = document.createElement("button");
      b.className = "side-item" + (item.id === curPanel ? " on" : "");
      b.dataset.panel = item.id;
      b.innerHTML = '<span class="bubble">' + ICONS[item.id] + '</span><span>' + esc(item.name) + "</span>";
      b.addEventListener("click", function () { switchPanel(item.id); });
      side.appendChild(b);
      var mb = document.createElement("button");
      mb.className = "bn-item" + (item.id === curPanel ? " on" : "");
      mb.dataset.panel = item.id;
      mb.innerHTML = ICONS[item.id] + "<span>" + esc(item.name) + "</span>";
      mb.addEventListener("click", function () { switchPanel(item.id); });
      bottom.appendChild(mb);
    });
  }
  function switchPanel(id) {
    curPanel = id;
    if (id !== "logs") stopLogsPoll();
    $$(".panel").forEach(function (p) { p.classList.toggle("on", p.id === "panel-" + id); });
    $$(".side-item,.bn-item").forEach(function (b) { b.classList.toggle("on", b.dataset.panel === id); });
    window.scrollTo({ top: 0, behavior: REDUCED ? "auto" : "smooth" });
    var loaders = { overview: loadOverview, search: loadSearchExtras, accounts: loadAccounts, stats: loadStats, acl: loadAcl, config: loadConfig, resolve: null, sessions: loadSessions, logs: loadLogsPanel };
    var fn = loaders[id];
    if (typeof fn === "function") {
      Promise.resolve(fn()).catch(function (e) { toast(e.message, "error"); });
    }
  }
  function loadSearchExtras() {
    focusSearch();
    loadScopes();
  }
  function focusSearch() {
    setTimeout(function () { var el = $("#searchInput"); if (el && window.innerWidth > 768) el.focus(); }, 200);
  }
  function httpsUrl(url) {
    var s = String(url || "");
    return s.startsWith("http://") ? s.replace("http://", "https://") : s;
  }

  /* ─────────── 登录 ─────────── */
  var loginShown = false;
  function showLogin() {
    if (loginShown) return;
    loginShown = true;
    $("#loginMask").hidden = false;
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
      btn.textContent = "进入面板 →";
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

  /* ─────────── 总览 ─────────── */
  var _ovSeq = 0;
  async function loadOverview() {
    var seq = ++_ovSeq;
    var d = await jget("/api/overview");
    if (seq !== _ovSeq) return;
    $("#chipVersion").innerHTML = '<span class="dot"></span>v' + esc(d.version);
    var master = $("#chipMaster");
    master.className = "chip " + (d.enable ? "" : "warn");
    master.innerHTML = '<span class="dot"></span>' + (d.enable ? "运行中" : "已关闭");
    var aclChip = $("#chipAcl");
    var aclMode = d.acl && d.acl.mode;
    aclChip.className = "chip" + (aclMode === "off" ? "" : " warn");
    aclChip.innerHTML = '<span class="dot"></span>ACL ' + (aclMode === "off" ? "关闭" : aclMode === "blacklist" ? "黑名单" : "白名单");

    var grid = $("#statGrid");
    var cards = [
      { label: "今日调用", num: d.todayTotal || 0, foot: "全部音源合计", icon: "♪" },
      { label: "网易云 · 今日", num: (d.today && d.today.ncm) || 0, foot: "累计 " + fmtCount((d.total && d.total.ncm) || 0), tone: "ncm", icon: "网" },
      { label: "酷狗 · 今日", num: (d.today && d.today.kg) || 0, foot: "累计 " + fmtCount((d.total && d.total.kg) || 0), tone: "kg", icon: "狗" },
      { label: "QQ · 今日", num: (d.today && d.today.qq) || 0, foot: "累计 " + fmtCount((d.total && d.total.qq) || 0), tone: "qq", icon: "Q" }
    ];
    grid.innerHTML = cards.map(function (c) {
      return '<div class="stat-card' + (c.tone ? " tone-" + c.tone : "") + '">' +
        '<div class="icon">' + c.icon + "</div>" +
        '<div class="label">' + esc(c.label) + "</div>" +
        '<div class="num" data-n="' + c.num + '">0</div>' +
        '<div class="foot">' + esc(c.foot) + "</div></div>";
    }).join("");
    grid.querySelectorAll(".num").forEach(function (el) { countUp(el, Number(el.dataset.n) || 0); });

    renderSvc(d.sources || []);
    renderFeed(d.recent || []);
    $("#recentHint").textContent = (d.recent || []).length ? "最新 " + d.recent.length + " 条" : "";
  }

  function renderSvc(sources) {
    var colors = { ncm: "var(--ncm)", kg: "var(--kg)", qq: "var(--qq)" };
    $("#svcGrid").innerHTML = sources.map(function (s) {
      var state = s.enabled ? (s.logged ? "ok" : "wait") : "bad";
      var tag = !s.enabled ? "未配置" : s.logged ? "已登录" : s.source === "qq" ? "匿名可用" : "匿名";
      var tagCls = !s.enabled ? "bad" : s.logged ? "ok" : "wait";
      return '<div class="svc" style="--sc:' + (colors[s.source] || "#888") + '">' +
        '<div class="svc-head"><span class="svc-dot ' + (state === "wait" ? "wait" : "") + '"></span>' +
        '<span class="svc-name">' + esc(s.name) + '</span><span class="svc-tag ' + tagCls + '">' + esc(tag) + "</span></div>" +
        '<div class="svc-meta">API：' + esc(s.apiBase) + "<br>音质：" + esc(s.quality) + "</div></div>";
    }).join("");
  }

  function renderFeed(recent) {
    var feed = $("#recentFeed");
    if (!recent.length) {
      feed.innerHTML = '<div class="empty-state">暂无调用记录，去群里点首歌试试</div>';
      return;
    }
    feed.innerHTML = recent.map(function (r, i) {
      return '<div class="feed-item' + (r.ok ? "" : " err") + '" style="animation-delay:' + Math.min(i * 40, 200) + 'ms">' +
        '<span class="fi-time">' + fmtTime(r.ts) + "</span>" +
        '<span class="fi-src ' + esc(r.source) + '">' + esc(SRC_NAMES[r.source] || r.source) + "</span>" +
        '<span class="fi-txt">' + esc(ACT_NAMES[r.action] || r.action) + (r.detail ? " · " + esc(r.detail) : "") + "</span></div>";
    }).join("");
  }

  /* ─────────── 多音源搜索 ─────────── */
  var _searchSeq = 0;
  var searchGroups = [];
  var searchItems = [];
  var searchFilter = "all";
  var searchType = "song";
  var _audio = null;
  var _previewBtn = null;
  var _previewSeq = 0;

  function stopPreview() {
    _previewSeq++; // 作废在途请求，防止迟到响应复活旧试听
    if (_audio) { _audio.pause(); _audio = null; }
    if (_previewBtn) { _previewBtn.textContent = "▶ 试听"; _previewBtn.classList.remove("playing"); _previewBtn = null; }
  }
  async function playPreview(source, sid, btn) {
    if (_previewBtn === btn) { stopPreview(); return; }
    stopPreview();
    var seq = _previewSeq;
    btn.disabled = true; btn.textContent = "加载中…";
    try {
      var r = await jget("/api/preview?source=" + encodeURIComponent(source) + "&sid=" + encodeURIComponent(sid));
      if (seq !== _previewSeq) return; // 等待期间用户又点了别的试听
      if (!r.url) throw new Error(r.error || "未取到试听链接");
      _audio = new Audio(r.url);
      _previewBtn = btn;
      _audio.play().catch(function () {});
      btn.textContent = "⏸ 播放中" + (r.label ? " · " + r.label : "");
      btn.classList.add("playing");
      _audio.onended = stopPreview;
      _audio.onerror = stopPreview;
    } catch (e) {
      if (seq === _previewSeq) toast(e.message, "error");
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
  async function loadScopes() {
    var sel = $("#remoteScope");
    try {
      var d = await jget("/api/remote/scopes");
      var cur = sel.value;
      sel.innerHTML = (d.scopes || []).map(function (s) {
        return '<option value="' + esc(s.umo) + '">' + esc(s.scope) + "</option>";
      }).join("") || '<option value="">暂无会话（群里先发一次指令）</option>';
      $("#remoteBar").hidden = false;
      if (cur) sel.value = cur;
    } catch (e) { $("#remoteBar").hidden = false; }
  }
  async function doSearch() {
    var keyword = $("#searchInput").value.trim();
    if (!keyword) { toast("先输入关键词", "warn"); return; }
    var seq = ++_searchSeq;
    var box = $("#searchResults");
    stopPreview();
    box.innerHTML = '<div class="skeleton" style="height:76px"></div><div class="skeleton" style="height:76px"></div><div class="skeleton" style="height:76px"></div>';
    var d;
    try {
      d = await jget("/api/search?keyword=" + encodeURIComponent(keyword) + "&type=" + encodeURIComponent(searchType));
    } catch (e) {
      if (seq === _searchSeq) box.innerHTML = '<div class="empty-state">⚠ ' + esc(e.message) + "</div>";
      return;
    }
    if (seq !== _searchSeq) return;
    searchGroups = d.groups || [];
    searchItems = d.items || [];
    $("#srcFilter").hidden = !(searchType === "song" && searchGroups.length);
    renderSearchResults();
    var empty = searchType === "song" ? !searchGroups.length : !searchItems.length;
    if (empty) box.innerHTML = '<div class="empty-state">没有搜到「' + esc(keyword) + '」相关的歌曲</div>';
    else toast("找到 " + (searchType === "song" ? searchGroups.length : searchItems.length) + " 条结果", "ok");
  }
  function renderSearchResults() {
    var box = $("#searchResults");
    if (searchType !== "song") { renderTypedResults(box); return; }
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
        return '<span class="vchip v-' + esc(v.source) + '" title="' + esc(v.sourceName) + (v.pay ? " · VIP" : "") + '">' +
          esc(SRC_MARK[v.source] || "?") + (v.pay ? '<small style="opacity:.85">VIP</small>' : "") + "</span>";
      }).join("");
      var primary = (g.versions || [])[0] || {};
      var canPlay = primary.sid && !primary.pay;
      return '<div class="result-row" style="animation-delay:' + Math.min(i * 45, 240) + 'ms">' +
        (g.cover
          ? '<img class="result-cover" src="' + esc(httpsUrl(g.cover)) + '" referrerpolicy="no-referrer" />'
          : '<div class="result-cover-ph">♪</div>') +
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
  function renderTypedResults(box) {
    var kindName = { playlist: "歌单", album: "专辑", artist: "歌手" };
    box.innerHTML = searchItems.map(function (it, i) {
      return '<div class="result-row" style="animation-delay:' + Math.min(i * 45, 240) + 'ms">' +
        (it.cover
          ? '<img class="result-cover" src="' + esc(httpsUrl(it.cover)) + '" referrerpolicy="no-referrer" />'
          : '<div class="result-cover-ph">♪</div>') +
        '<div class="result-main"><div class="result-name">' + esc(it.name) + "</div>" +
        '<div class="result-sub">' + esc(it.sub || "") + (it.count ? " · " + esc(it.count) + " 首" : "") + "</div></div>" +
        '<div class="result-vers"><span class="src-dot src-' + esc(it.source) + '">' + esc(it.sourceName || it.source) + "</span></div>" +
        '<div class="result-dur"><span class="badge">' + (kindName[searchType] || searchType) + "</span></div></div>";
    }).join("");
    bindCoverFallback(box);
  }
  function bindCoverFallback(box) {
    box.querySelectorAll("img.result-cover").forEach(function (img) {
      img.addEventListener("error", function () {
        var ph = document.createElement("div");
        ph.className = "result-cover-ph";
        ph.textContent = "♪";
        img.replaceWith(ph);
      });
    });
  }
  $("#searchTabs").addEventListener("click", function (e) {
    var b = e.target.closest(".stab");
    if (!b) return;
    searchType = b.dataset.t || "song";
    $$("#searchTabs .stab").forEach(function (x) { x.classList.toggle("on", x === b); });
    $("#srcFilter").hidden = searchType !== "song";
    if ($("#searchInput").value.trim()) doSearch();
  });
  $("#srcFilter").addEventListener("click", function (e) {
    var b = e.target.closest(".sfilter");
    if (!b) return;
    searchFilter = b.dataset.f;
    $$(".sfilter").forEach(function (x) { x.classList.toggle("on", x === b); });
    renderSearchResults();
  });
  $("#searchInput").addEventListener("keydown", function (e) { if (e.key === "Enter") doSearch(); });

  /* ─────────── 账号扫码 ─────────── */
  var _acctSeq = 0;
  async function loadAccounts() {
    var seq = ++_acctSeq;
    var d = await jget("/api/accounts");
    if (seq !== _acctSeq) return;
    var colors = { ncm: "var(--ncm)", kg: "var(--kg)", qq: "var(--qq)" };
    var marks = { ncm: "网", kg: "狗", qq: "Q" };
    $("#acctGrid").innerHTML = (d.accounts || []).map(function (a, i) {
      var ok = a.loggedIn;
      var ava = ok && a.avatar
        ? '<img src="' + esc(httpsUrl(a.avatar)) + '" alt="" referrerpolicy="no-referrer" />'
        : esc(marks[a.source] || "♪");
      return '<div class="acct" style="--sc:' + (colors[a.source] || "#888") + ';animation-delay:' + i * 70 + 'ms">' +
        '<div class="acct-head"><div class="acct-ava' + (ok ? " ok" : "") + '">' + ava + "</div>" +
        '<div><div class="acct-name">' + esc(a.name) + "</div>" +
        '<div class="acct-sub">' + (ok ? esc(a.nickname || a.uid || "已登录") : "未登录") + "</div></div>" +
        '<span class="acct-badge ' + (ok ? "ok" : "off") + '">' + (ok ? "已登录" : "未登录") + "</span></div>" +
        '<div class="acct-meta"><span>音质 <b>' + esc(a.quality) + "</b></span>" +
        (a.vip ? '<span>' + esc(a.vip) + "</span>" : "") +
        (a.enabled ? "" : '<span style="color:var(--red)">API 未配置</span>') + "</div>" +
        '<div class="acct-actions">' +
        '<button class="btn btn-gold btn-sm" data-act="qr" data-src="' + esc(a.source) + '"' + (a.enabled ? "" : " disabled") + ">扫码登录</button>" +
        '<button class="btn btn-ghost btn-sm" data-act="acct-refresh" data-src="' + esc(a.source) + '"' + (a.source === "qq" && ok ? "" : " disabled") + ">刷新凭证</button>" +
        '<button class="btn btn-ghost btn-sm" data-act="acct-logout" data-src="' + esc(a.source) + '"' + (ok ? "" : " disabled") + ">退出登录</button>" +
        "</div></div>";
    }).join("");
  }

  /* 扫码弹窗 */
  var qrTicket = null, qrTimer = null, qrSource = null;
  async function openQR(source) {
    qrSource = source;
    $("#qrMask").hidden = false;
    $("#qrTitle").textContent = SRC_NAMES[source] + " · 扫码登录";
    $("#qrImg").src = "";
    $("#qrImgWrap").classList.remove("expired");
    setQRState("正在获取二维码…", "");
    try {
      var r = await jpost("/api/qr/start", { source: source, type: "qq" });
      qrTicket = r.ticket;
      if (r.qr) { $("#qrImg").src = r.qr; setQRState("请使用手机 App 扫码", ""); }
      else if (r.qrUrl) { setQRState("请打开链接扫码：" + r.qrUrl, ""); }
      else { setQRState("未获取到二维码，请刷新重试", "bad"); return; }
      pollQR();
    } catch (e) {
      setQRState("创建失败：" + e.message, "bad");
    }
  }
  function pollQR() {
    clearInterval(qrTimer);
    qrTimer = setInterval(async function () {
      if (!qrTicket) { clearInterval(qrTimer); return; }
      try {
        var s = await jget("/api/qr/status?ticket=" + encodeURIComponent(qrTicket));
        if (s.state === "wait") { setQRState("等待扫码…", ""); }
        else if (s.state === "scanned") { setQRState("已扫码，请在手机上确认", ""); }
        else if (s.state === "done") {
          clearInterval(qrTimer);
          setQRState("登录成功 " + (s.nickname || ""), "ok");
          toast(SRC_NAMES[qrSource] + " 登录成功", "ok");
          setTimeout(closeQR, 900);
          loadAccounts();
        } else if (s.state === "timeout") {
          clearInterval(qrTimer);
          $("#qrImgWrap").classList.add("expired");
          setQRState("二维码已过期，请刷新", "bad");
        } else if (s.state === "refuse" || s.state === "cancel") {
          clearInterval(qrTimer);
          setQRState("已取消：" + (s.msg || ""), "bad");
        }
      } catch (e) {
        clearInterval(qrTimer);
        setQRState("查询失败：" + e.message, "bad");
      }
    }, 1500);
  }
  function setQRState(text, cls) {
    var el = $("#qrState");
    el.textContent = text;
    el.className = "qr-state " + cls;
  }
  function closeQR() {
    clearInterval(qrTimer);
    if (qrTicket) jpost("/api/qr/cancel", { ticket: qrTicket }).catch(function () {});
    qrTicket = null;
    $("#qrMask").hidden = true;
  }

  /* ─────────── 统计 ─────────── */
  var _stSeq = 0;
  async function loadStats() {
    var seq = ++_stSeq;
    var d = await jget("/api/stats?days=14");
    if (seq !== _stSeq) return;
    var cards = [
      { label: "今日调用", num: d.todayTotal || 0, foot: "全部音源", icon: "♪" },
      { label: "累计调用", num: d.totalAll || 0, foot: "历史总计", icon: "Σ" },
      { label: "网易云累计", num: (d.total && d.total.ncm) || 0, tone: "ncm", foot: "今日 " + ((d.today && d.today.ncm) || 0), icon: "网" },
      { label: "酷狗累计", num: (d.total && d.total.kg) || 0, tone: "kg", foot: "今日 " + ((d.today && d.today.kg) || 0), icon: "狗" },
      { label: "QQ 累计", num: (d.total && d.total.qq) || 0, tone: "qq", foot: "今日 " + ((d.today && d.today.qq) || 0), icon: "Q" }
    ];
    var grid = $("#statGrid2");
    grid.innerHTML = cards.map(function (c) {
      return '<div class="stat-card' + (c.tone ? " tone-" + c.tone : "") + '">' +
        '<div class="icon">' + c.icon + "</div>" +
        '<div class="label">' + esc(c.label) + "</div>" +
        '<div class="num" data-n="' + c.num + '">0</div>' +
        '<div class="foot">' + esc(c.foot) + "</div></div>";
    }).join("");
    grid.querySelectorAll(".num").forEach(function (el) { countUp(el, Number(el.dataset.n) || 0); });

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
    var max = Math.max.apply(null, top.map(function (t) { return t.count; })) || 1;
    box.innerHTML = top.map(function (t, i) {
      return '<div class="err-row" style="animation-delay:' + Math.min(i * 40, 200) + 'ms">' +
        '<div class="err-detail" title="' + esc(t.detail) + '">' + esc(t.detail) + "</div>" +
        '<div class="err-bar-wrap"><div class="err-bar" style="width:' + Math.round(t.count / max * 100) + '%"></div></div>' +
        '<div class="err-count">× ' + t.count + "</div></div>";
    }).join("");
  }
  async function loadHistory() {
    var d = await jget("/api/history");
    var feed = $("#historyFeed");
    var rows = d.history || [];
    if (!rows.length) {
      feed.innerHTML = '<div class="empty-state">各会话还没有播放记录</div>';
      return;
    }
    feed.innerHTML = rows.map(function (r) {
      var names = (r.items || []).slice(0, 3).map(function (it) {
        return esc(it.name) + " - " + esc(it.artist);
      }).join("；");
      return '<div class="feed-item"><span class="fi-scope">' + esc(r.scope) + "</span>" +
        '<span class="fi-txt">' + names + (r.items.length > 3 ? " …" : "") + "</span></div>";
    }).join("");
  }

  function drawTrend(days) {
    var svg = $("#trendChart");
    var W = 720, H = 220, padL = 34, padB = 26, padT = 12;
    var max = 1;
    days.forEach(function (d) { max = Math.max(max, d.ncm + d.kg + d.qq); });
    var bw = (W - padL - 8) / Math.max(days.length, 1);
    var scale = function (v) { return (H - padB - padT) * (v / max); };
    var parts = [];
    for (var g = 1; g <= 4; g++) {
      var gy = H - padB - (H - padB - padT) * g / 4;
      parts.push('<line x1="' + padL + '" y1="' + gy + '" x2="' + (W - 4) + '" y2="' + gy + '" stroke="rgba(240,79,152,.14)" stroke-dasharray="3 6"/>');
      parts.push('<text x="' + (padL - 6) + '" y="' + (gy + 4) + '" text-anchor="end" font-size="9" fill="#c9a8ba">' + fmtCount(Math.round(max * g / 4)) + "</text>");
    }
    days.forEach(function (d, i) {
      var x = padL + i * bw + bw * 0.18;
      var w = bw * 0.64;
      var hn = scale(d.ncm), hk = scale(d.kg), hq = scale(d.qq);
      var y = H - padB;
      if (hn > 0) { y -= hn; parts.push('<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + hn + '" rx="3" fill="var(--ncm)" opacity=".92"><title>网易云 ' + d.ncm + "</title></rect>"); }
      if (hk > 0) { y -= hk; parts.push('<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + hk + '" rx="3" fill="var(--kg)" opacity=".92"><title>酷狗 ' + d.kg + "</title></rect>"); }
      if (hq > 0) { y -= hq; parts.push('<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + hq + '" rx="3" fill="var(--qq)" opacity=".92"><title>QQ ' + d.qq + "</title></rect>"); }
      parts.push('<text x="' + (x + w / 2) + '" y="' + (H - 8) + '" text-anchor="middle" font-size="9" fill="#c9a8ba">' + esc(d.label) + "</text>");
    });
    svg.innerHTML = parts.join("");
  }

  function drawActions(byAction) {
    var rows = [];
    Object.keys(byAction).forEach(function (src) {
      Object.keys(byAction[src] || {}).forEach(function (act) {
        rows.push({ src: src, act: act, n: byAction[src][act] });
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
    box.innerHTML = rows.slice(0, 8).map(function (r, i) {
      return '<div class="bar-row" style="animation:rowPop .4s var(--bounce) both;animation-delay:' + i * 50 + 'ms">' +
        '<span><span class="src-tag ' + esc(r.src) + '">' + esc(SRC_NAMES[r.src]) + "</span> " + esc(ACT_NAMES[r.act] || r.act) + "</span>" +
        '<span class="bar-track"><span class="bar-fill" style="--bc:' + (colors[r.src] || "var(--blue)") + '" data-w="' + Math.max(4, Math.round(r.n / max * 100)) + '%"></span></span>' +
        '<span class="bar-num">' + fmtCount(r.n) + "</span></div>";
    }).join("");
    setTimeout(function () {
      box.querySelectorAll(".bar-fill").forEach(function (el) { el.style.width = el.dataset.w; });
    }, 80);
  }

  function renderRecentTable(recent) {
    var tb = $("#recentTbl tbody");
    if (!recent.length) {
      tb.innerHTML = '<tr><td colspan="5"><div class="empty-state">暂无流水</div></td></tr>';
      return;
    }
    tb.innerHTML = recent.slice(0, 40).map(function (r) {
      return "<tr><td>" + fmtDateTime(r.ts) + "</td>" +
        '<td><span class="src-tag ' + esc(r.source) + '">' + esc(SRC_NAMES[r.source] || r.source) + "</span></td>" +
        "<td>" + esc(ACT_NAMES[r.action] || r.action) + "</td>" +
        '<td><span class="badge ' + (r.ok ? "ok" : "err") + '">' + (r.ok ? "成功" : "失败") + "</span></td>" +
        '<td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(r.detail || "--") + "</td></tr>";
    }).join("");
  }

  /* ─────────── 黑白名单 ─────────── */
  var aclData = { mode: "off", blacklist: [], whitelist: [] };
  var _aclSeq = 0;
  async function loadAcl() {
    var seq = ++_aclSeq;
    var d = await jget("/api/acl");
    if (seq !== _aclSeq) return; // 快速切面板时丢弃过期响应
    aclData = d;
    $$("#aclModeSeg button").forEach(function (b) { b.classList.toggle("on", b.dataset.mode === d.mode); });
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
      return '<div class="le-item"><span>' + esc(it) + '</span><button data-act="acl-del" data-list="' + sel.slice(1) + '" data-i="' + i + '" title="删除">✕</button></div>';
    }).join("");
  }
  async function saveAcl(patch) {
    Object.assign(aclData, patch);
    var r = await jpost("/api/acl/save", aclData);
    if (r.ok) { toast("黑白名单已保存", "ok"); loadAcl(); }
    else toast(r.error || "保存失败", "error");
  }
  function aclAdd(kind, inputSel) {
    var input = $(inputSel);
    var v = input.value.trim();
    if (!v) { toast("请输入 QQ 号或群号", "warn"); return; }
    // 支持一次粘贴多条：逗号 / 空格 / 分号 / 换行分隔
    var parts = v.split(/[,，;；\s]+/).map(function (s) { return s.trim(); }).filter(function (s) { return s; });
    var items = aclData[kind] || [];
    var added = 0;
    parts.forEach(function (p) {
      if (!items.includes(p)) { items.push(p); added++; }
    });
    if (!added) { toast("条目都已存在", "warn"); return; }
    var patch = {}; patch[kind] = items;
    input.value = "";
    saveAcl(patch);
  }
  function aclCopy(kind) {
    var items = aclData[kind] || [];
    if (!items.length) { toast("列表是空的", "warn"); return; }
    var text = items.join("\n");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast("已复制 " + items.length + " 条到剪贴板", "ok"); },
        function () { toast("复制失败，请手动选择导出", "error"); });
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
    ["qqofficialChunkedUpload", "官方分片上传", "qq_official >10MB 文件"]
  ];
  async function loadConfig() {
    var seq = ++_cfgSeq;
    var d = await jget("/api/config");
    if (seq !== _cfgSeq) return;
    cfgData = d.config;
    renderConfig();
  }
  function bind(id) {
    var el = $("#cfg-" + id);
    return el ? el.value : "";
  }
  function renderConfig() {
    var c = cfgData;
    var body = $("#configBody");
    var html = "";

    function group(title, inner) {
      var count = inner.split("cfg-item").length - 1;
      return '<details class="cfg-group" open><summary><span class="arrow">▶</span>' + esc(title) +
        '<span class="cfg-count">' + count + " 项</span></summary>" +
        '<div class="cfg-items">' + inner + "</div></details>";
    }
    function textItem(id, label, desc, value, type) {
      return '<div class="cfg-item"><label>' + esc(label) + '</label><input class="input" id="cfg-' + esc(id) +
        '" type="' + (type || "text") + '" value="' + esc(value == null ? "" : value) + '" />' +
        (desc ? '<div class="desc">' + esc(desc) + "</div>" : "") + "</div>";
    }
    function boolItem(id, label, desc, value) {
      return '<div class="cfg-item"><div class="sw-row"><label>' + esc(label) + "</label>" +
        '<div class="sw' + (value ? " on" : "") + '" data-sw="' + esc(id) + '" role="switch" aria-checked="' + !!value + '" tabindex="0"></div></div>' +
        (desc ? '<div class="desc">' + esc(desc) + "</div>" : "") + "</div>";
    }
    function selectItem(id, label, desc, value, options) {
      return '<div class="cfg-item"><label>' + esc(label) + "</label>" +
        '<select class="input" id="cfg-' + esc(id) + '">' +
        options.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (o[0] === value ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join("") +
        "</select>" + (desc ? '<div class="desc">' + esc(desc) + "</div>" : "") + "</div>";
    }

    var general = "";
    general += selectItem("defaultSource", "默认音源", "聚合模式下决定同名歌曲的首选版本与版本排序", c.defaultSource,
      [["auto", "聚合（三平台混搜）"], ["ncm", "网易云音乐"], ["kg", "酷狗音乐"], ["qq", "QQ音乐"]]);
    general += textItem("maxList", "点歌列表数量", "1-20", c.maxList, "number");
    general += textItem("identifyPrefix", "识别信息前缀", "", c.identifyPrefix);
    general += textItem("cooldownSec", "点歌冷却 (秒)", "同群两次点歌最小间隔，0 = 关闭", c.cooldownSec, "number");
    general += textItem("rateLimitMs", "音源限速 (ms)", "每请求最小间隔，防突发风控，0 = 关闭", c.rateLimitMs, "number");
    html += group("基础", general);

    var srcQ = [["auto", "自动"], ["standard", "标准"], ["higher", "较高"], ["exhigh", "极高"], ["lossless", "无损"], ["hires", "Hi-Res"], ["jyeffect", "高清臻音"], ["sky", "沉浸环绕"], ["dolby", "杜比"], ["jymaster", "超清母带"]];
    var kgQ = [["auto", "自动"], ["128", "标准"], ["320", "HQ"], ["flac", "无损"], ["high", "Hi-Res"], ["super", "Super"], ["viper_clear", "蝰蛇超清"], ["viper_tape", "蝰蛇母带"]];
    var qqQ = [["auto", "自动"], ["128", "标准"], ["320", "HQ"], ["flac", "无损"], ["atmos", "全景声"], ["master", "臻品母带"], ["atmos_db", "杜比"]];

    var ncmHtml = textItem("ncm-apiBase", "API 地址", "api-enhanced 服务，如 http://127.0.0.1:3000", c.ncm.apiBase);
    ncmHtml += selectItem("ncm-quality", "最高音质", "", c.ncm.quality, srcQ);
    ncmHtml += boolItem("ncm-qualityUnblock", "VIP 歌自动解灰", "拿不到 URL 时用 unblock 音源兜底", c.ncm.qualityUnblock);
    ncmHtml += '<div class="cfg-item"><label>登录状态</label><div class="desc">' + (c.ncm.hasCookie ? "已有 Cookie" : "匿名（可在「账号扫码」登录）") + "</div></div>";
    html += group("网易云音乐", ncmHtml);

    var kgHtml = textItem("kg-apiBase", "API 地址", "KuGouMusicApi 服务，如 http://127.0.0.1:4000", c.kg.apiBase);
    kgHtml += selectItem("kg-quality", "最高音质", "", c.kg.quality, kgQ);
    kgHtml += boolItem("kg-trialFallback", "VIP 歌发试听", "未登录时发 60 秒试听", c.kg.trialFallback);
    kgHtml += '<div class="cfg-item"><label>登录状态</label><div class="desc">' + (c.kg.hasCookie ? "已有 Cookie" : "匿名（搜索需登录，请先「账号扫码」）") + "</div></div>";
    html += group("酷狗音乐", kgHtml);

    var qqHtml = selectItem("qq-quality", "最高音质", "", c.qq.quality, qqQ);
    qqHtml += boolItem("qq-trialFallback", "VIP 歌发试听", "匿名时发试听片段", c.qq.trialFallback);
    qqHtml += '<div class="cfg-item"><label>登录状态</label><div class="desc">' + (c.qq.hasCredential ? "已有凭证" : "匿名（免费歌可完整播放）") + "</div></div>";
    html += group("QQ音乐（内置直连，无需 API 服务）", qqHtml);

    var delivery = "";
    var skipToggles = ["enable", "enableSongRequest", "enableResolve", "resolveCards", "renderListCard"];
    CFG_BOOLS.forEach(function (spec) {
      if (skipToggles.indexOf(spec[0]) < 0) {
        delivery += boolItem(spec[0], spec[1], spec[2], c[spec[0]]);
      }
    });
    delivery += textItem("compressBitrate", "压缩码率 (kbps)", "", c.compressBitrate, "number");
    delivery += textItem("downloadTimeout", "下载超时 (ms)", "", c.downloadTimeout, "number");
    delivery += textItem("keepFileSec", "临时文件保留 (秒)", "", c.keepFileSec, "number");
    html += group("发送通道（多平台适配）", delivery);

    var webuiHtml = boolItem("webui-enable", "启用 WebUI", "", c.webui.enable);
    webuiHtml += textItem("webui-host", "监听地址", "0.0.0.0 允许局域网访问", c.webui.host);
    webuiHtml += textItem("webui-port", "端口", "改动需重载插件生效", c.webui.port, "number");
    html += group("WebUI 面板", webuiHtml);

    var sched = c.scheduler || {};
    var schedHtml = boolItem("sched-enable", "启用定时任务", "每日签到 / 凭证保活 / 订阅推送", sched.enable);
    schedHtml += textItem("sched-signinHour", "执行时刻 (小时)", "0-23，分钟随机错峰", sched.signinHour, "number");
    schedHtml += boolItem("sched-ncmSignin", "网易云每日签到", "需已登录网易云", sched.ncmSignin);
    schedHtml += boolItem("sched-qqRefresh", "QQ 凭证保活", "每日刷新降低掉线概率", sched.qqRefresh);
    html += group("定时任务与订阅推送", schedHtml);

    body.innerHTML = html;
    body.querySelectorAll(".sw").forEach(function (sw) {
      sw.addEventListener("click", function () { sw.classList.toggle("on"); sw.setAttribute("aria-checked", sw.classList.contains("on")); });
      sw.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sw.click(); } });
    });
  }
  async function saveConfig() {
    if (!cfgData) return;
    var patch = {};
    patch.defaultSource = bind("defaultSource");
    patch.maxList = Number(bind("maxList")) || 10;
    patch.identifyPrefix = bind("identifyPrefix");
    patch.cooldownSec = Number(bind("cooldownSec")) || 0;
    patch.rateLimitMs = Number(bind("rateLimitMs")) || 0;
    patch.ncm = { apiBase: bind("ncm-apiBase"), quality: bind("ncm-quality"), qualityUnblock: $("#cfg-ncm-qualityUnblock").classList.contains("on") };
    patch.kg = { apiBase: bind("kg-apiBase"), quality: bind("kg-quality"), trialFallback: $("#cfg-kg-trialFallback").classList.contains("on") };
    patch.qq = { quality: bind("qq-quality"), trialFallback: $("#cfg-qq-trialFallback").classList.contains("on") };
    CFG_BOOLS.forEach(function (spec) {
      var el = $("#cfg-" + spec[0]);
      if (el) patch[spec[0]] = el.classList.contains("on");
    });
    patch.compressBitrate = Number(bind("compressBitrate")) || 128;
    patch.downloadTimeout = Number(bind("downloadTimeout")) || 120000;
    patch.keepFileSec = Number(bind("keepFileSec")) || 60;
    patch.webui = { enable: $("#cfg-webui-enable").classList.contains("on"), host: bind("webui-host"), port: Number(bind("webui-port")) || 17818 };
    patch.scheduler = {
      enable: $("#cfg-sched-enable").classList.contains("on"),
      signinHour: Number(bind("sched-signinHour")) || 0,
      ncmSignin: $("#cfg-sched-ncmSignin").classList.contains("on"),
      qqRefresh: $("#cfg-sched-qqRefresh").classList.contains("on")
    };
    var btn = $('[data-act="cfg-save"]');
    btn.disabled = true; btn.textContent = "保存中…";
    try {
      var r = await jpost("/api/config/save", { config: patch });
      if (r.rejected && r.rejected.length) toast("部分项被拒绝：" + r.rejected.join(", "), "warn");
      else toast("配置已保存" + (r.note ? " " + r.note : ""), "ok");
      loadConfig();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      btn.disabled = false; btn.textContent = "保存全部";
    }
  }

  /* ─────────── 链接解析工具 ─────────── */
  async function doResolve() {
    var text = $("#resolveInput").value.trim();
    if (!text) { toast("先粘贴链接或口令文本", "warn"); return; }
    var box = $("#resolveResult");
    box.hidden = false;
    box.innerHTML = '<div class="skeleton" style="height:80px"></div>';
    try {
      var r = await jpost("/api/resolve", { text: text });
      var srcName = SRC_NAMES[r.source] || "未知音源";
      var hits = (r.matched || []).map(function (s) { return SRC_NAMES[s] || s; }).join(" / ");
      box.innerHTML =
        '<span class="rr-badge rr-src">' + esc(srcName) + "</span>" +
        (r.type ? '<span class="rr-badge rr-type">' + esc(r.type) + "</span>" : "") +
        (r.playable ? '<span class="rr-badge" style="background:rgba(62,207,154,.15);color:var(--green)">可自动点歌 ✓</span>' : "") +
        '<div class="rr-name">' + (r.name ? esc(r.name) : "未能解析出具体内容") + "</div>" +
        '<div class="rr-sub">识别到的平台特征：' + esc(hits || "无") + (r.error ? "<br>⚠ " + esc(r.error) : "") +
        "<br>展开后链接：" + esc(r.expanded || "") + "</div>";
    } catch (e) {
      box.innerHTML = '<div class="rr-name">解析失败</div><div class="rr-sub">' + esc(e.message) + "</div>";
    }
  }

  /* ─────────── 会话 ─────────── */
  var _ssSeq = 0;
  async function loadSessions() {
    var seq = ++_ssSeq;
    var d = await jget("/api/auth/sessions");
    if (seq !== _ssSeq) return;
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
  function logLine(e) {
    var cls = e.level === "ERROR" ? "err" : e.level === "WARNING" ? "warn" : "info";
    return '<div class="log-line log-' + cls + '"><span class="log-lv">' + esc(e.level) + "</span>" + esc(e.msg) + "</div>";
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
  function appendLogEntries(entries) {
    var box = $("#logConsole");
    if (box.querySelector(".empty-state")) box.innerHTML = "";
    box.insertAdjacentHTML("beforeend", entries.map(logLine).join(""));
    box.scrollTop = box.scrollHeight;
  }
  function stopLogsPoll() {
    if (_logTimer) { clearInterval(_logTimer); _logTimer = null; }
  }
  async function loadLogsPanel() {
    stopLogsPoll();
    $("#logConsole").innerHTML = '<div class="skeleton" style="height:60px"></div>';
    try {
      var d = await jget("/api/logs");
      _logSeq = d.seq;
      renderLogEntries(d.entries || []);
    } catch (e) { /* 面板静默 */ }
    _logTimer = setInterval(async function () {
      if (curPanel !== "logs" || document.hidden || !$("#loginMask").hidden) return;
      try {
        var d2 = await jget("/api/logs?after=" + _logSeq);
        _logSeq = d2.seq;
        if ((d2.entries || []).length) appendLogEntries(d2.entries);
      } catch (e) { /* 下轮重试 */ }
    }, 2500);
  }

  /* ─────────── 事件委托 ─────────── */
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-act]");
    if (!t) return;
    var act = t.dataset.act;
    var actMap = {
      "theme": function () { cycleTheme(); },
      "logout": async function () {
        await jpost("/api/auth/logout", {});
        authGen++;
        showLogin();
      },
      "login": doLogin,
      "svc-check": async function () {
        t.disabled = true; t.textContent = "检测中…";
        try {
          var r = await jget("/api/service/check");
          ["ncm", "kg", "qq"].forEach(function (s) {
            var item = r[s] || {};
            toast(SRC_NAMES[s] + "：" + (item.ok ? "✓ " : "✗ ") + (item.msg || ""), item.ok ? "ok" : "error");
          });
          loadOverview();
        } catch (err) { toast(err.message, "error"); }
        finally { t.disabled = false; t.textContent = "检测连通性"; }
      },
      "do-search": doSearch,
      "do-resolve": doResolve,
      "preview": function () { playPreview(t.dataset.source, t.dataset.sid, t); },
      "deliver": function () { deliverPlay(t.dataset.source, t.dataset.sid, t); },
      "remote-refresh": loadScopes,
      "history-refresh": function () { loadHistory().catch(function () {}); },
      "logs-clear": function () {
        $("#logConsole").innerHTML = '<div class="empty-state">已清屏（仅本地显示）</div>';
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
        if (!confirm("确定退出 " + SRC_NAMES[t.dataset.src] + " 登录？")) return;
        try { await jpost("/api/accounts/logout", { source: t.dataset.src }); toast("已退出登录", "ok"); loadAccounts(); loadOverview(); }
        catch (e) { toast(e.message, "error"); }
      },
      "acct-refresh": async function () {
        t.disabled = true; t.textContent = "刷新中…";
        try {
          var r = await jpost("/api/accounts/refresh", { source: t.dataset.src });
          toast(r.msg || (r.ok ? "刷新成功" : "刷新失败"), r.ok ? "ok" : "warn");
          loadAccounts();
        } catch (e) { toast(e.message, "error"); }
        finally { t.disabled = false; t.textContent = "刷新凭证"; }
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
        if (!confirm("确定清空每日统计？（累计数保留）")) return;
        try { await jpost("/api/stats/reset", { keepTotals: true }); toast("统计已清空", "ok"); loadStats(); loadOverview(); }
        catch (e) { toast(e.message, "error"); }
      },
      "acl-add-black": function () { aclAdd("blacklist", "#blackInput"); },
      "acl-add-white": function () { aclAdd("whitelist", "#whiteInput"); },
      "acl-del": function () {
        var key = t.dataset.list === "blackList" ? "blacklist" : "whitelist";
        var items = (aclData[key] || []).slice();
        items.splice(Number(t.dataset.i), 1);
        var patch = {}; patch[key] = items;
        saveAcl(patch);
      },
      "cfg-save": saveConfig,
      "sess-revoke": async function () {
        try { await jpost("/api/auth/sessions/revoke", { id: t.dataset.id }); toast("会话已下线", "ok"); loadSessions(); }
        catch (e) { toast(e.message, "error"); }
      },
      "revoke-others": async function () {
        if (!confirm("下线除当前外的所有会话？")) return;
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
    if (actMap[act]) { e.preventDefault(); actMap[act](); }
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
    if (e.key === "Escape") { if (!$("#qrMask").hidden) closeQR(); }
  });

  /* ─────────── 轮询 ─────────── */
  setInterval(function () {
    if (document.hidden || !$("#loginMask").hidden) return;
    if (curPanel === "overview") loadOverview().catch(function () {});
  }, 10000);

  /* ─────────── 启动 ─────────── */
  function boot() {
    makePetals();
    renderNav();
    loadOverview().catch(function (e) { toast(e.message, "error"); });
  }
  

  async function init() {
    makePetals();
    renderNav();
    try {
      var meta = await jget("/api/meta");
      $("#chipVersion").innerHTML = '<span class="dot"></span>v' + esc(meta.version);
      if (!meta.authed) { showLogin(); return; }
      boot();
    } catch (e) {
      showLogin();
    }
  }
  init();
})();
