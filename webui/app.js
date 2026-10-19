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
  // 最近调用流水用：非今天的条目带 MM-DD 前缀，跨天记录不再无法区分
  function fmtFeedTime(ts) {
    if (!ts) return "--";
    var d = new Date(ts * 1000);
    var now = new Date();
    if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()) {
      return fmtTime(ts);
    }
    return ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2) + " " + fmtTime(ts);
  }
  function fmtDateTime(ts) {
    if (!ts) return "--";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2) + " " + fmtTime(ts);
  }
  // 减少动态效果实时降级：CSS 由 @media (prefers-reduced-motion) 直接生效，
  // JS 只需提供实时查询（motionReduced 供动画代码判断），不设类切换
  // （.reduced-motion 选择器不存在，切换是空操作）
  var reduceQuery = window.matchMedia ? matchMedia("(prefers-reduced-motion: reduce)") : null;
  function motionReduced() { return !!(reduceQuery && reduceQuery.matches); }
  var ACT_NAMES = { search: "搜索", play: "点歌", url: "取流", lyric: "歌词", detail: "详情", comment: "评论", mv: "MV", rank: "排行榜", playlist: "歌单", explore: "浏览", login: "登录", resolve: "解析", other: "其他" };
  var SRC_NAMES = { ncm: "网易云", kg: "酷狗", qq: "QQ音乐" };

  /* ─────────── Toast ─────────── */
  function srcLogoImg(source, cls) {
    // 平台 logo 统一经白名单映射再拼静态路径（与 srcTagHtml 同一口径）：
    // esc() 不拦 "/"，异常 source 直接拼进 URL 会构成同源路径遍历 URL（仅 404，
    // 且有全局 img error 兜底，但口径必须统一）
    if (!SRC_NAMES[source]) return "";
    return '<img class="' + (cls || "") + '" src="/webui/logos/' + esc(source) + '.png" alt="" />';
  }
  function srcTagHtml(source, cls) {
    // 平台名经白名单映射后再拼静态资源路径：esc() 不拦 "/"，异常 source 直接拼
    // 进 URL 会构成（同源静态文件内的）路径遍历；映射不中的回退内联音符占位
    var known = SRC_NAMES[source];
    if (!known) {
      // 未知音源不把原始名拼进 class（会产出任意 class token，可能撞上既有类名）：
      // 统一固定占位类 src-other，原名只进文本（esc 转义）
      return '<span class="' + cls + ' src-other">♪ ' + esc(source) + "</span>";
    }
    return (
      '<span class="' + cls + " " + esc(source) + '">' +
      '<img src="/webui/logos/' + esc(source) + '.png" alt="" />' +
      esc(known) + "</span>"
    );
  }

  // 音源名 → class token 的白名单映射（vchip/src-dot 等直接拼 class 的位点统一
  // 走这里）：未知音源回退固定类 src-other，原始名不进 class（防任意 class token）
  function srcClass(source) {
    return SRC_NAMES[source] ? esc(source) : "src-other";
  }

  // 服务端来源键的颜色查表走 own-property 白名单口径（与 srcClass 一致）：
  // 裸 table[key] 命中原型链（"constructor"/"hasOwnProperty"）会取到函数，
  // String(fn) 被拼进 style 属性——不构成逃逸但样式尽毁
  function srcVar(table, key, fallback) {
    return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : fallback;
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
    while (box.childElementCount > TOAST_MAX) {
      // 被挤出容器的条目先清定时器：游离节点上的 _t1/_t2 触发是无害空转，但没必要留
      var dropped = box.firstElementChild;
      clearTimeout(dropped._t1);
      clearTimeout(dropped._t2);
      box.removeChild(dropped);
    }
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
  var DEFAULT_TIMEOUT = 15000;   // 单请求超时，慢接口在调用处覆盖
  // 分散在各处的轮询间隔集中声明：调整节奏时改这里即可，不必多点同步
  var SEARCH_TIMEOUT_MS = 25000; // 搜索是三平台并行扇出 + 去重，比普通请求慢
  var QR_POLL_MS = 1500;         // 扫码状态轮询
  var LOG_POLL_MS = 2500;        // 运行日志轮询
  var OVERVIEW_POLL_MS = 10000;  // 总览静默轮询
  function withTimeout(opt, ms) {
    // 裸 fetch 没有超时：上游挂住时按钮永远停在 pending
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, ms || DEFAULT_TIMEOUT);
    var outer = opt.signal;
    var onOuterAbort = function () { ctrl.abort(); };
    if (outer) {
      if (outer.aborted) ctrl.abort();
      else outer.addEventListener("abort", onOuterAbort);
    }
    opt.signal = ctrl.signal;
    return {
      opt: opt,
      done: function () {
        clearTimeout(timer);
        // 外层 signal 的 abort 监听必须随 settle 移除：请求结束后 outer 再 abort
        // 时监听器只会白触发一次，还会把本请求的 ctrl 长期滞留在 outer 上
        if (outer) outer.removeEventListener("abort", onOuterAbort);
      }
    };
  }
  async function api(url, opt, timeout) {
    opt = opt || {};
    var gen = authGen;
    var t = withTimeout(opt, timeout);
    try {
      var r = await fetch(url, t.opt);
      // 超时覆盖必须延伸到 body 读取：服务端 body 中途停顿时 r.json()/r.text()
      // 会永久挂起（骨架屏长亮）；abort 后读到 AbortError，统一走下方 catch。
      // body 解析失败单独兜住：非 JSON 的 2xx（如反代错误页）按无 body 处理，
      // 而不是把 SyntaxError 裸抛给各 loader
      var data = null;
      try {
        var ct = r.headers.get("content-type") || "";
        data = ct.includes("json") ? await r.json() : await r.text();
      } catch (be) {
        if (be && be.name === "AbortError") throw be;
        data = null;
      }
      if (!r.ok) {
        var b = {};
        try { b = typeof data === "string" ? JSON.parse(data) : (data || {}); } catch (e) {}
        if (r.status === 401 && gen === authGen && !(opt && opt.skip401)) enterLoggedOut();
        var err2 = new Error(b.error || ("HTTP " + r.status));
        err2.status = r.status;
        throw err2;
      }
      // 2xx 协议归一：body 解析出标量（string/number）一律视为协议错误。
      // 所有 /api/* 都应返回 JSON 对象（core/webui/_util.py json_response），
      // 2xx 拿到 string 只可能是反代/网关注入的错误页；原样返回会让各 loader
      // 把 string 当空数据降级出假空态（如 loadSessions 显示「暂无会话」）。
      // 按协议错误抛出，走与 HTTP 错误相同的 catch 链（2xx 必非 401，
      // skip401 语义不受影响）
      if (data !== null && typeof data !== "object") {
        var err3 = new Error("响应格式异常");
        err3.status = r.status;
        throw err3;
      }
      if (data === null) {
        // 2xx + JSON 解析失败（Content-Type 声明 json 但体为空/截断）：返回 null
        // 会让 loadScopes/saveConfig/openQR/doResolve 等调用点抛 TypeError 把
        // V8 原文当错误信息弹给用户——统一按协议错误走 HTTP 错误相同的 catch 链
        var err4 = new Error("响应格式异常（空响应体）");
        err4.status = r.status;
        throw err4;
      }
      return data;
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
  }
  function jget(u, opt, timeout) { return api(u, opt, timeout); }
  async function jpost(u, body, opt) {
    var o = opt || {};
    o.method = "POST";
    o.headers = o.headers || { "Content-Type": "application/json" };
    o.body = JSON.stringify(body || {});
    return api(u, o);
  }

  /* ─────────── 主题 ─────────── */
  // 与 index.html 两枚 meta theme-color、style.css 的 --bg（light/dark 两处）
  // 三方保持一致：meta 标签吃不到 CSS 变量，重复不可避免——改配色时三处同步
  // （手动切换主题后移动端地址栏跟着变，meta 的 prefers-color-scheme 只跟随系统）
  var THEME_META_COLORS = { light: "#fff0f6", dark: "#171129" };
  function syncThemeMeta() {
    var mode = document.documentElement.dataset.theme || "light";
    var metas = document.querySelectorAll('meta[name="theme-color"]');
    for (var i = 0; i < metas.length; i++) {
      metas[i].setAttribute("media", "");
      metas[i].setAttribute("content", THEME_META_COLORS[mode] || THEME_META_COLORS.light);
    }
  }
  function applyTheme(mode) {
    var dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    var d = document.documentElement;
    d.dataset.themeMode = mode;
    d.dataset.theme = mode === "auto" ? (dark ? "dark" : "light") : mode;
    try { localStorage.setItem("mh-theme", mode); } catch (e) {}
    syncThemeMeta();
  }
  function cycleTheme() {
    var cur = document.documentElement.dataset.themeMode || "auto";
    var next = cur === "auto" ? "dark" : cur === "dark" ? "light" : "auto";
    applyTheme(next);
    toast("主题：" + (next === "auto" ? "跟随系统" : next === "dark" ? "夜间紫" : "樱花粉"), "ok");
  }
  if (window.matchMedia) {
    // 与下方 reduceQuery 同一守卫口径：旧 Safari 的 MediaQueryList 没有
    // addEventListener（只有 addListener），IIFE 顶层抛错会挂掉整个应用
    var darkQuery = matchMedia("(prefers-color-scheme: dark)");
    var onSchemeChange = function () {
      if (document.documentElement.dataset.themeMode === "auto") applyTheme("auto");
    };
    if (darkQuery.addEventListener) darkQuery.addEventListener("change", onSchemeChange);
    else if (darkQuery.addListener) darkQuery.addListener(onSchemeChange);
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
    // 可访问名称直接给终值：动画中间帧不被读屏当现值播报
    el.setAttribute("aria-label", String(target));
    // 后台页签的 rAF 不触发（浏览器节流），动画会永远停在首帧「0」直到下一次
    // 轮询 patchNumbers 才纠正——隐藏时直接写终值
    if (motionReduced() || document.hidden) { el.textContent = fmtCount(target); return; }
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

  /* ─────────── 自定义下拉框（替代原生 <select> 弹层） ───────────
     原生 select 的展开列表由浏览器绘制：套不进面板主题、做不了动画。
     原 <select> 留在 DOM（display:none）只作 value 载体，展示与交互全部由
     手写 widget 承接；选值后同步回 select 并派发 change，业务代码零改动。 */
  var CSEL_SEQ = 0;
  var CSEL_OPEN = null;  // 全局同时只允许一个展开中的下拉

  function cselClose(w) {
    if (!w || !w.isOpen) return;
    w.isOpen = false;
    if (CSEL_OPEN === w) CSEL_OPEN = null;
    w.root.classList.remove("open");
    w.btn.setAttribute("aria-expanded", "false");
    w.btn.removeAttribute("aria-activedescendant");
    var pop = w.pop;
    var finish = function () {
      pop.hidden = true;
      pop.classList.remove("closing");
      pop.removeEventListener("animationend", finish);
      clearTimeout(w._closeTimer);
      w._closeFinish = null;
    };
    if (motionReduced()) { finish(); return; }
    pop.classList.add("closing");
    pop.addEventListener("animationend", finish);
    w._closeFinish = finish;
    w._closeTimer = setTimeout(finish, 240);  // animationend 兜底：动画被中断时也能收起
  }

  // 收起动画进行中重新展开：必须撤销挂起的 finish，否则它会在新动画结束时
  // 把弹层再藏掉（closing 类的 forwards 填充也会把弹层停在透明态）
  function cselCancelClose(w) {
    if (!w._closeFinish) return;
    w.pop.removeEventListener("animationend", w._closeFinish);
    clearTimeout(w._closeTimer);
    w.pop.classList.remove("closing");
    w._closeFinish = null;
  }

  function cselRenderOptions(w) {
    var sel = w.sel;
    var opts = [];
    for (var i = 0; i < sel.options.length; i++) {
      opts.push({ value: sel.options[i].value, text: sel.options[i].textContent });
    }
    w.opts = opts;
    var cur = sel.value;
    var html = "";
    for (var j = 0; j < opts.length; j++) {
      var on = opts[j].value === cur;
      html += '<div class="csel-opt' + (on ? " sel" : "") + '" role="option" data-i="' + j +
        '" id="' + w.optIdPrefix + j + '" aria-selected="' + (on ? "true" : "false") + '">' +
        esc(opts[j].text) + "</div>";
    }
    w.pop.innerHTML = html;
    var label = "";
    for (var k = 0; k < opts.length; k++) {
      if (opts[k].value === cur) { label = opts[k].text; break; }
    }
    w.labelEl.textContent = label;
  }

  function cselSetActive(w, idx) {
    if (!w.opts.length) return;
    w.active = Math.max(0, Math.min(w.opts.length - 1, idx));
    var nodes = w.pop.children;
    for (var i = 0; i < nodes.length; i++) nodes[i].classList.toggle("active", i === w.active);
    w.btn.setAttribute("aria-activedescendant", w.optIdPrefix + w.active);
    // 手动滚动替代 scrollIntoView：弹层是 fixed 定位，scrollIntoView 会连带滚动页面
    var node = nodes[w.active];
    if (!node) return;
    var top = node.offsetTop, bottom = top + node.offsetHeight;
    if (top < w.pop.scrollTop) w.pop.scrollTop = top;
    else if (bottom > w.pop.scrollTop + w.pop.clientHeight) w.pop.scrollTop = bottom - w.pop.clientHeight;
  }

  // 弹层 portal 到 body 后用 fixed 定位：配置分组为折叠动画设了 overflow:hidden，
  // 绝对定位的弹层会被裁掉；同时按视口剩余空间动态决定上/下展开与列表高度
  function cselPosition(w) {
    var r = w.btn.getBoundingClientRect();
    var below = window.innerHeight - r.bottom - 12;
    var above = r.top - 12;
    var up = below < 236 && above > below;  // 236 ≈ 弹层常规高度，余量留给边距
    var h = Math.max(96, Math.min(264, (up ? above : below) - 16));
    w.pop.style.left = r.left + "px";
    w.pop.style.width = r.width + "px";
    w.pop.style.maxHeight = h + "px";
    if (up) {
      w.pop.style.top = "auto";
      w.pop.style.bottom = window.innerHeight - r.top + 8 + "px";
    } else {
      w.pop.style.bottom = "auto";
      w.pop.style.top = r.bottom + 8 + "px";
    }
    w.pop.classList.toggle("up", up);
  }

  function cselOpen(w) {
    if (w.isOpen || !w.opts.length) return;
    cselClose(CSEL_OPEN);
    cselCancelClose(w);
    w.isOpen = true;
    CSEL_OPEN = w;
    w.pop.hidden = false;
    cselPosition(w);
    w.root.classList.add("open");
    w.btn.setAttribute("aria-expanded", "true");
    var selIdx = 0;
    for (var i = 0; i < w.opts.length; i++) {
      if (w.opts[i].value === w.sel.value) { selIdx = i; break; }
    }
    cselSetActive(w, selIdx);
  }

  function cselPick(w, idx) {
    var o = w.opts[idx];
    if (!o) return;
    if (w.sel.value !== o.value) {
      w.sel.value = o.value;
      try { w.sel.dispatchEvent(new Event("change", { bubbles: true })); } catch (err) { /* 老浏览器忽略 */ }
    }
    cselRenderOptions(w);
    cselClose(w);
    w.btn.focus();
  }

  // 原生 select 的首字快速定位：连续输入 500ms 内的字符拼成前缀循环匹配
  function cselTypeAhead(w, ch) {
    var now = Date.now();
    w.typeBuf = (now - (w.typeAt || 0) < 500 ? w.typeBuf : "") + ch.toLowerCase();
    w.typeAt = now;
    clearTimeout(w.typeTimer);
    w.typeTimer = setTimeout(function () { w.typeBuf = ""; }, 500);
    for (var i = 0; i < w.opts.length; i++) {
      var idx = ((w.active < 0 ? -1 : w.active) + 1 + i) % w.opts.length;
      if ((w.opts[idx].text || "").toLowerCase().indexOf(w.typeBuf) === 0) { cselSetActive(w, idx); return; }
    }
  }

  function cselLabelFor(sel) {
    var lbl = sel.id ? document.querySelector('label[for="' + sel.id + '"]') : null;
    return lbl ? lbl.textContent.trim() : "";
  }

  function enhanceSelect(sel) {
    if (!sel || sel.dataset.cselSrc) return;
    sel.dataset.cselSrc = "1";
    var n = ++CSEL_SEQ;
    var root = document.createElement("div");
    root.className = "csel";
    if (sel.classList.contains("sel-narrow")) root.classList.add("sel-narrow");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "csel-btn input";
    var lbl = cselLabelFor(sel);
    if (lbl) btn.setAttribute("aria-label", lbl);
    btn.setAttribute("role", "combobox");
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-autocomplete", "list");
    btn.innerHTML = '<span class="csel-label"></span>' +
      '<svg class="csel-arrow" viewBox="0 0 12 7" width="12" height="7" aria-hidden="true">' +
      '<path d="M1 1l5 5 5-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    var pop = document.createElement("div");
    pop.className = "csel-pop";
    pop.id = "csel-pop-" + n;
    pop.setAttribute("role", "listbox");
    pop.hidden = true;
    btn.setAttribute("aria-controls", pop.id);
    root.appendChild(btn);
    // 弹层挂到 body（fixed 定位）：避免被折叠组/卡片等 overflow:hidden 祖先裁剪
    document.body.appendChild(pop);
    pop._cselOwner = sel;  // 面板重渲染后按属主是否断连清孤儿弹层（见 cselSweep）
    sel.insertAdjacentElement("afterend", root);
    sel.tabIndex = -1;

    var w = {
      sel: sel, root: root, btn: btn, pop: pop,
      labelEl: btn.querySelector(".csel-label"),
      optIdPrefix: "csel-opt-" + n + "-",
      opts: [], active: -1, isOpen: false, typeBuf: "", typeAt: 0, typeTimer: null
    };
    cselRenderOptions(w);

    btn.addEventListener("keydown", function (e) {
      var k = e.key;
      if (!w.isOpen) {
        if (k === "ArrowDown" || k === "ArrowUp" || k === "Enter" || k === " ") {
          e.preventDefault(); cselOpen(w);
        } else if (k.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
          cselOpen(w); cselSetActive(w, 0); cselTypeAhead(w, k);
        }
        // 其余按键（含 Esc）未展开时不拦截，原样冒泡给全局处理器
        return;
      }
      if (k === "ArrowDown") { e.preventDefault(); cselSetActive(w, w.active + 1); }
      else if (k === "ArrowUp") { e.preventDefault(); cselSetActive(w, w.active - 1); }
      else if (k === "Home") { e.preventDefault(); cselSetActive(w, 0); }
      else if (k === "End") { e.preventDefault(); cselSetActive(w, w.opts.length - 1); }
      else if (k === "PageDown") { e.preventDefault(); cselSetActive(w, w.active + 7); }
      else if (k === "PageUp") { e.preventDefault(); cselSetActive(w, w.active - 7); }
      else if (k === "Enter" || k === " ") { e.preventDefault(); cselPick(w, w.active); }
      else if (k === "Escape") { e.preventDefault(); e.stopPropagation(); cselClose(w); }
      else if (k === "Tab") { cselClose(w); }  // 不拦 Tab：焦点照常走，仅收起弹层
      else if (k.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) cselTypeAhead(w, k);
    });
    btn.addEventListener("click", function () { w.isOpen ? cselClose(w) : cselOpen(w); });
    pop.addEventListener("click", function (e) {
      var opt = e.target.closest(".csel-opt");
      if (opt) cselPick(w, Number(opt.dataset.i));
    });
    pop.addEventListener("mousemove", function (e) {
      var opt = e.target.closest(".csel-opt");
      if (opt && Number(opt.dataset.i) !== w.active) cselSetActive(w, Number(opt.dataset.i));
    });
    sel._csel = w;
  }

  // 面板重渲染（innerHTML 整体重建）会销毁 .csel root，但 portal 到 body 的弹层
  // 不会随之消失——按「属主 select 已断连」清掉孤儿弹层，否则反复进出配置页
  // 会无界累积（弹层及其 click/mousemove 监听器都是活引用）
  function cselSweep() {
    $$(".csel-pop").forEach(function (pop) {
      var owner = pop._cselOwner;
      if (!owner || !owner.isConnected) {
        if (CSEL_OPEN && CSEL_OPEN.pop === pop) CSEL_OPEN = null;
        pop.remove();
      }
    });
  }
  function enhanceSelects(root) {
    cselSweep();
    var list = (root || document).querySelectorAll("select.input");
    for (var i = 0; i < list.length; i++) enhanceSelect(list[i]);
  }
  // loadScopes 等直接改写 select innerHTML/value 的调用点之后手动同步一次
  function syncSelect(sel) {
    if (sel && sel._csel) { cselClose(sel._csel); cselRenderOptions(sel._csel); }
  }
  document.addEventListener("click", function (e) {
    // 弹层 portal 到 body 了：点外判定必须同时认 root 和 pop
    if (CSEL_OPEN && !CSEL_OPEN.root.contains(e.target) && !CSEL_OPEN.pop.contains(e.target)) {
      cselClose(CSEL_OPEN);
    }
  });
  window.addEventListener("resize", function () { cselClose(CSEL_OPEN); });
  // 弹层是 fixed 定位：页面/容器滚动时实时跟随按钮，否则会视觉脱锚
  var _cselRepositionQueued = false;
  window.addEventListener("scroll", function () {
    if (!CSEL_OPEN || _cselRepositionQueued) return;
    _cselRepositionQueued = true;
    requestAnimationFrame(function () {
      _cselRepositionQueued = false;
      if (CSEL_OPEN) cselPosition(CSEL_OPEN);
    });
  }, { capture: true, passive: true });

  /* ─────────── 自定义 tooltip（替代浏览器原生 title 气泡） ───────────
     原生 title 提示由浏览器绘制，样式不可控且延迟约 1s；改读 data-tip
     属性，悬停 / 键盘聚焦 300ms 后浮出，滚动或移出即收。 */
  var tipEl = null, tipTarget = null, tipTimer = null;
  function tipShow(target) {
    var text = target.getAttribute("data-tip");
    if (!text) return;
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "tip";
      tipEl.setAttribute("role", "tooltip");
      tipEl.hidden = true;
      document.body.appendChild(tipEl);
    }
    tipEl.textContent = text;
    tipTarget = target;
    var r = target.getBoundingClientRect();
    tipEl.hidden = false;
    tipEl.style.left = "0px"; tipEl.style.top = "0px";  // 先归零再测量，避免首次定位跳动
    var tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
    var x = Math.min(Math.max(8, r.left + r.width / 2 - tw / 2), window.innerWidth - tw - 8);
    var y = r.top - th - 8;
    if (y < 8) y = r.bottom + 8;  // 上方放不下 → 目标下方
    tipEl.style.left = x + "px";
    tipEl.style.top = y + "px";
  }
  function tipHide() {
    clearTimeout(tipTimer);
    tipTimer = null;
    tipTarget = null;
    if (tipEl) tipEl.hidden = true;
  }
  document.addEventListener("mouseover", function (e) {
    var t = e.target.closest ? e.target.closest("[data-tip]") : null;
    if (t === tipTarget) return;
    clearTimeout(tipTimer);
    if (!t) { tipHide(); return; }
    tipHide();
    tipTimer = setTimeout(function () { tipShow(t); }, 300);
  });
  document.addEventListener("focusin", function (e) {
    var t = e.target.closest ? e.target.closest("[data-tip]") : null;
    clearTimeout(tipTimer);
    if (!t) { tipHide(); return; }
    tipHide();
    tipTimer = setTimeout(function () { tipShow(t); }, 300);
  });
  document.addEventListener("focusout", tipHide);
  document.addEventListener("click", tipHide);
  document.addEventListener("scroll", tipHide, true);
  // role=button 的 data-tip 宿主（详情单元格等）用 Enter/Space 立即出泡/收起：
  // 按钮 role 承诺了键盘激活行为，没有 handler 就是假 affordance
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    var t = e.target.closest ? e.target.closest('[data-tip][role="button"]') : null;
    if (!t) return;
    e.preventDefault();
    if (tipTarget === t && tipEl && !tipEl.hidden) tipHide();
    else tipShow(t);
  });

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
    ncm: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17A8.5 8.5 0 0 0 12 3.5z" fill="currentColor" stroke="none"/></svg>',
    kg: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 14c3-6 13-6 16 0"/><circle cx="12" cy="15" r="2.4"/><path d="M6 8.5c1.6 1.2 2.4 1.2 4 0M14 8.5c1.6 1.2 2.4 1.2 4 0"/></svg>',
    qq: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 11.5c0-4 3.6-7 8-7s8 3 8 7c0 4-3.6 7.5-8 7.5-1 0-2-.2-2.8-.5L5 20l1-3.2A7 7 0 0 1 4 11.5z"/></svg>'
  };

  /* 平台 logo 加载失败统一兜底：换成对应 SVG 图标。
     error 事件不冒泡，用捕获监听——覆盖 innerHTML 注入的全部 logo 位点
     （统计卡/流水/版本芯片/音源状态/账号卡），后端加音源或静态白名单
     未同步时不再出现碎图；未知音源没有对应图标，直接隐藏 */
  document.addEventListener(
    "error",
    function (e) {
      var t = e.target;
      if (!(t && t.tagName === "IMG") || t.dataset.fbDone) return;
      var m = (t.getAttribute("src") || "").match(/\/webui\/logos\/([a-z]+)\.png/);
      if (!m) return;
      t.dataset.fbDone = "1";
      if (ICONS[m[1]]) t.src = "data:image/svg+xml," + encodeURIComponent(ICONS[m[1]]);
      else t.style.visibility = "hidden";
    },
    true
  );
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
    // 焦点主动移入抽屉：焦点陷阱只圈住 Tab 循环，不移入的话键盘/读屏用户
    // 感知不到抽屉已展开（焦点还停在被遮住的底栏按钮上）。
    // 归还目标不能在此捕获：renderNav 刚重建了底栏，「更多」按钮是全新节点，
    // closeMore 按选择器重新查询（捕获的 activeElement 只会是即将被销毁的旧节点）
    var first = $$(FOCUSABLE, sheet)[0];
    if (first) first.focus();
  }
  function closeMore() {
    if (!moreOpen) return;
    moreOpen = false;
    var sheet = $("#moreSheet");
    if (sheet) sheet.hidden = true;
    renderNav();
    // 焦点归还底栏的「更多」按钮：renderNav 在打开与关闭时各重建一次底栏，
    // 捕获式归还（openMore 时存 activeElement）拿到的是已被销毁的旧节点
    var trigger = $('button[data-act="more-toggle"]');
    if (trigger) trigger.focus();
  }

  // 面板 loader 唯一事实来源：switchPanel 与 panel-retry 共用，避免两份
  // 字面量漂移（retry 版此前漏了 search，搜索面板错误态重试成了空操作）
  // 面板级在途请求闸：离开面板时中止该面板未完成的初载请求（响应不再渲染进
  // 隐藏面板、也不再浪费带宽）。loaders 用 panelSignal(panel) 取 signal 传入 jget
  var _panelCtrls = {};
  function panelSignal(panel) {
    var c = _panelCtrls[panel];
    if (!c || c.signal.aborted) {
      c = new AbortController();
      _panelCtrls[panel] = c;
    }
    return c.signal;
  }
  function abortPanelRequests(panel) {
    var c = _panelCtrls[panel];
    if (c) {
      c.abort();
      delete _panelCtrls[panel];
    }
  }
  var PANEL_LOADERS = { overview: loadOverview, search: loadSearchExtras, accounts: loadAccounts,
    stats: loadStats, acl: loadAcl, config: loadConfig, resolve: null, sessions: loadSessions, logs: loadLogsPanel };
  function switchPanel(id) {
    curPanel = id;
    closeMore();
    if (id !== "logs") stopLogsPoll();
    // 中止其它面板的在途初载（与上方搜索扇出中止同口径；各 loader 的 seq 守卫
    // 仍保留——abort 只是省流量，代际判定照旧兜并发重入）
    Object.keys(_panelCtrls).forEach(function (k) {
      if (k !== id) abortPanelRequests(k);
    });
    // 离开搜索面板即停止试听：否则音频在后台继续播，且按钮已随 DOM 销毁，
    // _previewBtn 指向游离节点，再点其它试听时状态判定彻底错乱
    if (id !== "search") {
      stopPreview(); abortPreview();
      // 在途的搜索防抖一并取消：250ms 内切走时定时器到点仍会向三平台
      // 扇出一次无人看到的搜索并渲染进隐藏面板（浪费上游配额）
      clearTimeout(_debounceTimer);
      _debounceTimer = null;
      // 已扇出的搜索请求一并中止：请求不随面板隐藏而取消，响应回来仍会
      // 渲染进隐藏面板（与 teardownLocal 的清理同口径）
      if (_searchCtrl) { _searchCtrl.abort(); _searchCtrl = null; }
    }
    $$(".panel").forEach(function (p) { p.classList.toggle("on", p.id === "panel-" + id); });
    $$(".side-item,.bn-item").forEach(function (b) {
      var on = b.dataset.panel === id;
      b.classList.toggle("on", on);
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    window.scrollTo({ top: 0, behavior: motionReduced() ? "auto" : "smooth" });
    var fn = PANEL_LOADERS[id];
    if (typeof fn === "function") {
      Promise.resolve(fn())
        .then(function () {
          // 异步渲染完成后再测一次：面板切换帧里 scrollHeight 还是旧值，
          // 配置/日志这类大块内容渲染后进度条比例会失准
          requestAnimationFrame(measureScroll);
        })
        .catch(function (e) {
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
    // 首访/回到搜索页：结果区空白时给一条引导空态。残留骨架屏（搜索进行中切走、
    // switchPanel abort 后 catch 跳过渲染）也归位成空态——否则回来永远停在 shimmer
    var box = $("#searchResults");
    if (box && (!box.childElementCount || box.querySelector(".skeleton"))) {
      box.innerHTML = '<div class="empty-state">输入关键词开始搜索</div>';
    }
  }
  var _focusTimer = null;
  function focusSearch() {
    // 200ms 延迟 + 触发时校验 curPanel：面板已切走时绝不抢焦点。
    // clearTimeout 只防快速连续触发导致定时器堆叠，无需在 switchPanel 里取消。
    // 键盘用户正在 Tab 导航时（activeElement 不是 body）不抢焦点：
    // 焦点突然飞进输入框会打断动线（WCAG 2.4.3 焦点顺序）
    clearTimeout(_focusTimer);
    _focusTimer = setTimeout(function () {
      _focusTimer = null;
      var el = $("#searchInput");
      if (
        el &&
        window.innerWidth > 768 &&
        curPanel === "search" &&
        (document.activeElement === document.body || document.activeElement === null)
      )
        el.focus();
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
  // 手动登出 / 401 共用的现场清理：停音频、停二维码/日志轮询、收悬浮层、
  // 取消在途请求。必须收敛到一处——原先三处各自复制，已漏过确认框与防抖定时器
  function teardownLocal() {
    stopPreview();
    abortPreview();
    closeQR();
    stopLogsPoll();
    clearInterval(overviewTimer);
    // 悬浮层一并收掉：自定义确认框（z-index 高于登录遮罩）若开着，
    // 会悬浮在登录页上永不关闭，_confirmResolve 悬挂泄漏
    closeConfirm(false);
    closeMore();
    // 在途的搜索防抖一并取消：定时器到点会向三平台扇出一次无人看到的搜索
    clearTimeout(_debounceTimer);
    _debounceTimer = null;
    clearTimeout(_focusTimer);
    _focusTimer = null;
    // 在途搜索/解析一并中止：登出后旧响应回来不应渲染进已登出的面板
    if (_searchCtrl) { _searchCtrl.abort(); _searchCtrl = null; }
    if (_resolveCtrl) { _resolveCtrl.abort(); _resolveCtrl = null; }
  }
  // 登出与 401 必须走同一条路径：清理现场后各面板 seq 整体自增作废——
  // 重新登录 boot() 会重跑当前面板 loader，迟到的旧响应（含旧关键词搜索结果）不得顶掉新数据
  function enterLoggedOut() {
    // 单次守卫：会话过期后多个在途请求会接连吃 401，第一个已收完现场；
    // 重复 teardown 会让 closeQR/定时器清理路径被反复驱动（曾是 cancel 无限
    // 循环的放大器）。重新登录 hideLogin 会复位 loginShown
    if (loginShown) return;
    teardownLocal();
    invalidatePanelState();
    showLogin();
  }
  // teardown 之后的「现场作废」段，登出与 401 两条路径共用（手动登出曾漏掉
  // 这一半：代际未推进、配置快照未清——重登后 loadConfig 早退，展示登出前的
  // 旧配置 DOM，编辑保存会以陈旧基线整包覆盖服务端配置）
  function invalidatePanelState() {
    _ovSeq++; _searchSeq++; _scopeSeq++; _acctSeq++; _stSeq++; _histSeq++;
    _aclSeq++; _cfgSeq++; _ssSeq++; _logSeq++; _logLoadSeq++;
    // 配置页「已加载」状态一并作废：重登后 boot() 重跑 loadConfig，保留快照
    // 会被早退判据拦下，展示的是重登前的旧配置 DOM（编辑保存以陈旧基线覆盖）
    _cfgForget();
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
  // Enter 键与按钮可同时触发 doLogin：请求进行中直接拒绝重入，避免并发双 POST
  var _loginInFlight = false;
  async function doLogin() {
    if (_loginInFlight) return;
    var btn = $("#loginBtn");
    var pwd = $("#loginPwd").value;
    if (!pwd) { shakeLogin("请输入密码"); return; }
    _loginInFlight = true;
    btn.disabled = true;
    btn.textContent = "验证中…";
    try {
      // skip401：登录接口自身的 401 是「密码错误」业务反馈，
      // 不该触发全局 enterLoggedOut（会多余地 closeQR/清轮询）
      await jpost("/api/auth/login", { password: pwd }, { skip401: true });
      authGen++;
      hideLogin();
      boot();
    } catch (e) {
      shakeLogin(e.message || "密码错误");
    } finally {
      _loginInFlight = false;
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
    var inner = '<div class="empty-state">⚠ ' + esc(msg) +
      '<br><button class="btn btn-ghost btn-sm" style="margin-top:10px" data-act="panel-retry">重试</button></div>';
    if (el.tagName === "TBODY") {
      // tbody 的合法子元素只有 tr：直接塞 div 会被 HTML 解析器 foster-parent 到表格外
      var table = el.closest("table");
      var headRow = table && table.querySelector("thead tr");
      var cols = headRow ? headRow.children.length : 1;
      el.innerHTML = '<tr><td colspan="' + cols + '">' + inner + "</td></tr>";
      return;
    }
    el.innerHTML = inner;
  }
  function setLoaded(sel) { busy(sel, false); }
  // 统计卡 markup 两处共用（总览 / 调用统计）：icon 优先用 c.src 的平台 logo，
  // 否则取 ICONS[c.ico]；总览的静默轮询不走这里（走 patchNumbers 原地更新）
  function statCardHtml(cards) {
    return cards.map(function (c) {
      return '<div class="stat-card' + (c.tone ? " tone-" + c.tone : "") + '">' +
        '<div class="icon">' +
        (c.src ? srcLogoImg(c.src) : ICONS[c.ico]) + "</div>" +
        '<div class="label">' + esc(c.label) + "</div>" +
        '<div class="num" role="img" aria-label="' + num(c.num) + '" data-n="' + num(c.num) + '">0</div>' +
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
      d = await jget("/api/overview", { signal: panelSignal("overview") });
    } catch (e) {
      if (seq !== _ovSeq || (e && e.aborted)) return;
      // 错误必须写进容器（骨架屏与错误态互斥），否则骨架屏会一直闪；
      // 顶栏状态芯片一并复位为「状态未知」，否则初次加载失败后会永久
      // 停留在初始文案「加载中」（中性灰点），错误态下显示假状态
      if (!silent) {
        renderError("#statGrid", e.message);
        renderError("#svcGrid", e.message);
        var mc = $("#chipMaster");
        if (mc) {
          mc.className = "chip";
          mc.innerHTML = '<span class="dot"></span>状态未知';
        }
      }
      return;
    }
    if (seq !== _ovSeq) return;
    if (!d) { if (!silent) { renderError("#statGrid", "总览数据为空"); renderError("#svcGrid", "总览数据为空"); } return; }
    setLoaded("#statGrid");
    setLoaded("#svcGrid");
    var chip = $("#chipVersion");
    chip.innerHTML = '<span class="dot"></span>v' + esc(d.version);
    // init 时未登录拿到的是空版本（/api/meta 仅对已登录会话下发）会把徽章藏掉，
    // 登录后走本路径渲染真版本号，hidden 也要一并复位，否则徽章永远不出现
    chip.hidden = !d.version;
    var master = $("#chipMaster");
    // 加载完成前用中性灰点，绿色只表示「运行中」
    master.className = "chip " + (d.enable ? "ok" : "warn");
    master.innerHTML = '<span class="dot"></span>' + (d.enable ? "运行中" : "已关闭");
    var aclChip = $("#chipAcl");
    var aclMode = d.acl && d.acl.mode;
    aclChip.className = "chip" + (aclMode === "off" ? "" : " warn");
    // 未知模式显式显示原值：后端加新模式时兜底分支静默显示「白名单」会造成误读
    var aclText = aclMode === "off" ? "关闭" : aclMode === "blacklist" ? "黑名单" : aclMode === "whitelist" ? "白名单" : esc(aclMode || "关闭");
    aclChip.innerHTML = '<span class="dot"></span>ACL ' + aclText;

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
      // 终值同步可访问名称：数字滚动动画的中间帧不被读屏当现值播报
      el.setAttribute("aria-label", String(c.num));
      if (foots[i] && c.foot != null) foots[i].textContent = c.foot;
    });
  }

  function renderSvc(sources) {
    var grid = $("#svcGrid");
    if (!sources.length) {
      grid.classList.remove("no-anim");
      grid.removeAttribute("data-rendered");
      grid.removeAttribute("data-svc-sig");
      grid.innerHTML = '<div class="empty-state">没有可用的音源服务</div>';
      return;
    }
    // 与 statGrid 同理：音源状态每 10 秒轮询一次，不能整块重建。
    // 判据用显式标记而不是 childElementCount —— 后者与骨架屏的行数
    // （skeletonRows(3,84) 恰好也是 3）会撞车，导致骨架屏永久驻留。
    // 来源签名补齐「数量不变但音源集合变化」的盲区：改配置增删平台后
    // 若只看数量会命中原地补丁分支，卡片身份（data-src/logo/名字）停留在首渲染值
    var svcSig = sources.map(function (s) { return s.source; }).join(",");
    if (grid.getAttribute("data-rendered") !== "1" || grid.childElementCount !== sources.length ||
        grid.getAttribute("data-svc-sig") !== svcSig) {
      grid.classList.remove("no-anim");
      grid.innerHTML = sources.map(function (s) {
        var ava = srcLogoImg(s.source, "svc-logo");
        return '<div class="svc" data-src="' + esc(s.source) + '">' +
          '<div class="svc-head">' + ava +
          '<span class="svc-name">' + esc(s.name) + '</span><span class="svc-tag"></span></div>' +
          '<div class="svc-meta"></div></div>';
      }).join("");
      grid.setAttribute("data-rendered", "1");
      grid.setAttribute("data-svc-sig", svcSig);
    } else {
      grid.classList.add("no-anim");
    }
    // 按 data-src 拼 querySelector 会对引号/反斜杠敏感，改索引对位：
    // 上面的重建分支保证 .svc 数量与 sources 一一对应
    var cards = $$(".svc", grid);
    sources.forEach(function (s, i) {
      var card = cards[i];
      if (!card) return;
      // 依赖缺失优先于登录态展示：配置开着但客户端自检不过（qqmusic-api-python
      // 未装等）时，搜到的结果永远不含该源——标「已登录/匿名可用」会误导
      var missing = !!s.enabled && s.ready === false;
      var tag = !s.enabled ? "未配置" : missing ? "依赖缺失" : s.logged ? "已登录" : s.source === "qq" ? "匿名可用" : "匿名";
      var tagEl = card.querySelector(".svc-tag");
      tagEl.className = "svc-tag " + (!s.enabled || missing ? "bad" : s.logged ? "ok" : "wait");
      tagEl.textContent = tag;
      // 概览的「已登录」按配置有无登录凭证判定（不实时探测）：凭证失效时与
      // 「账号」页的实时状态会不一致——tooltip 说明口径，避免误读成矛盾
      tagEl.title = missing
        ? "音源已配置但客户端不可用（运行依赖缺失或初始化失败），详情见运行日志"
        : s.logged ? "按配置中的登录凭证判定（不实时探测），实时状态见「账号」页" : "";
      card.querySelector(".svc-meta").innerHTML =
        "API：" + esc(s.apiBase) + "<br>音质：" + esc(s.quality);
    });
  }

  function renderFeed(recent, animate) {
    var feed = $("#recentFeed");
    if (!recent.length) {
      feed.classList.remove("no-anim");
      feed.removeAttribute("data-feed-sig");
      feed.innerHTML = '<div class="empty-state">暂无调用记录，去群里点首歌试试</div>';
      return;
    }
    // 内容没变就不整块重建：与 statGrid 的 patchNumbers 同一动机——
    // 轮询 innerHTML 会清掉用户的文本选区。feed 无逐项数字可 patch，
    // 用全量签名对比跳过无变化的重建（有变化时才换 innerHTML）
    var sig = JSON.stringify(recent);
    if (feed.getAttribute("data-feed-sig") === sig) return;
    feed.setAttribute("data-feed-sig", sig);
    feed.classList.toggle("no-anim", animate === false);
    feed.innerHTML = recent.map(function (r, i) {
      // 静默更新不写 animation-delay：入场动画只应在首载播一次，
      // 否则 10 秒轮询会让整列记录反复滑入
      var delay = animate === false ? "" : ' style="animation-delay:' + Math.min(i * 40, 200) + 'ms"';
      // 成功/失败不靠边框颜色区分（对白底约 1.4:1），加 ✓/✕ 图标并给读屏标签
      return '<div class="feed-item' + (r.ok ? "" : " err") + '"' + delay + ">" +
        '<span class="fi-ic ' + (r.ok ? "ok" : "bad") + '" aria-hidden="true">' + (r.ok ? "✓" : "✕") + "</span>" +
        '<span class="sr-only">' + (r.ok ? "成功：" : "失败：") + "</span>" +
        '<span class="fi-time">' + fmtFeedTime(r.ts) + "</span>" +
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
    if (_previewBtn) { _previewBtn.textContent = "▶︎ 试听"; _previewBtn.classList.remove("playing"); _previewBtn = null; }
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
      btn.textContent = "⏸︎ 播放中" + (r.label ? " · " + r.label : "");
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
      if (_previewBtn !== btn) btn.textContent = "▶︎ 试听";
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
  // loadScopes 失败提示的去重标记：成功后复位（见 loadScopes 的 catch）
  var _scopesFailToasted = false;
  async function loadScopes() {
    var sel = $("#remoteScope");
    if (!sel) return;
    var seq = ++_scopeSeq;
    try {
      var d = await jget("/api/remote/scopes", { signal: panelSignal("search") });
      if (seq !== _scopeSeq) return;  // 快速切面板时丢弃过期响应
      var cur = sel.value;
      sel.innerHTML = (d.scopes || []).map(function (s) {
        return '<option value="' + esc(s.umo) + '">' + esc(s.scope) + "</option>";
      }).join("") || '<option value="">暂无会话（群里先发一次指令）</option>';
      $("#remoteBar").hidden = false;
      // 按值遍历比对而不是拼属性选择器：umo 含引号时 querySelector 会抛
      // SyntaxError，被外层 catch 误报成「会话列表加载失败」
      var hasCur = false;
      for (var oi = 0; oi < sel.options.length; oi++) {
        if (sel.options[oi].value === cur) { hasCur = true; break; }
      }
      if (cur && hasCur) {
        sel.value = cur;
      } else if (sel.options.length && sel.selectedIndex < 0) {
        // 之前选中的会话已消失：回落第一项，避免自定义下拉按钮渲染成空白
        sel.selectedIndex = 0;
      }
      syncSelect(sel);  // 自定义下拉框重建选项与按钮文案（见 enhanceSelects）
      _scopesFailToasted = false;
    } catch (e) {
      if (seq !== _scopeSeq || (e && e.aborted)) return;
      // 失败也要展开投递条：保留手动刷新入口，且不能静默。
      // 同一次会话内连续失败只弹一次 toast（每次切到搜索页都重试，重复弹窗是噪音）
      $("#remoteBar").hidden = false;
      if (!_scopesFailToasted) {
        _scopesFailToasted = true;
        toast(e.message, "error");
      }
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
        { signal: ctrl.signal }, SEARCH_TIMEOUT_MS);
    } catch (e) {
      if (seq === _searchSeq && !(e && e.aborted)) {
        // 错误态带「重试」：与其它面板的错误范式一致，失败不再是一句死文案
        box.innerHTML = '<div class="empty-state">⚠ ' + esc(e.message) +
          '<div style="margin-top:12px"><button class="btn btn-ghost btn-sm" data-act="do-search">重试</button></div></div>';
      }
      return;
    }
    if (seq !== _searchSeq) return;
    searchGroups = d.groups || [];
    searchItems = d.items || [];
    updateFilterBar();
    renderSearchResults(true);
    var empty = searchType === "song" ? !searchGroups.length : !searchItems.length;
    // 读屏摘要写入独立的 live region：结果列表已移出 live 语义（整表进 live region
    // 会让读屏整段播报几十条结果）
    var live = $("#searchLive");
    if (live) live.textContent = empty ? "没有搜到相关内容" : "找到 " + (searchType === "song" ? searchGroups.length : searchItems.length) + " 条结果";
    if (empty) {
      // 零结果时优先展示后端聚合收集的登录提示（kg 禁匿名搜索是常态），
      // 只给一句「没有搜到」会让用户不知道该去扫码
      var notices = (d.notices || []).filter(Boolean);
      box.innerHTML = '<div class="empty-state">没有搜到「' + esc(keyword) + '」相关的' +
        esc(TYPED_NAMES[searchType] || "内容") +
        (notices.length ? "<br>" + notices.map(function (n) { return "ℹ " + esc(n); }).join("<br>") : "") +
        "</div>";
    } else {
      toast("找到 " + (searchType === "song" ? searchGroups.length : searchItems.length) + " 条结果", "ok");
      // 有结果但部分音源本次取数失败：结果顶部给一条提示，避免「只剩 QQ」
      // 被误读成「别的源没有这首歌」
      var partial = (d.notices || []).filter(Boolean);
      if (partial.length) {
        box.insertAdjacentHTML(
          "afterbegin",
          '<div class="src-notice">ℹ ' + partial.map(function (n) { return esc(n); }).join("<br>ℹ ") + "</div>"
        );
      }
    }
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
    if (!list.length && searchFilter !== "all" && searchGroups.length) {
      // 新搜索在激活的筛选下零命中时自动回落「全部」：保留筛选跨搜索是刻意的
      // （连续找同音源），但「结果比预期少还停在空态」是死胡同——回落并同步
      // 筛选条高亮，用户注意不到筛选条也不会卡住
      searchFilter = "all";
      $$(".sfilter").forEach(function (x) {
        var on = x.dataset.f === "all";
        x.classList.toggle("on", on);
        x.setAttribute("aria-pressed", on ? "true" : "false");
      });
      list = searchGroups;
    }
    if (!list.length) {
      box.innerHTML = '<div class="empty-state">该筛选条件下没有结果</div>';
      return;
    }
    box.innerHTML = list.map(function (g, i) {
      var vers = (g.versions || []).map(function (v) {
        // 类名不带 v- 前缀：CSS 定义的是 .vchip.ncm
        // aria-label 带上 data-tip 的完整信息（含 VIP）：tooltip 是纯视觉实现，读屏拿不到
        return '<span class="vchip ' + srcClass(v.source) + '" tabindex="0" role="img" aria-label="' +
          esc(v.sourceName) + (v.pay ? "（VIP 版本）" : "") + '" data-tip="' + esc(v.sourceName) + (v.pay ? " · VIP" : "") + '">' +
          srcLogoImg(v.source) +
          (v.pay ? "<small>VIP</small>" : "") + "</span>";
      }).join("");
      var primary = (g.versions || [])[0] || {};
      var canPlay = primary.sid && !primary.pay;
      return '<div class="result-row"' + (animate === false ? "" : ' style="animation-delay:' + Math.min(i * 45, 240) + 'ms"') + ">" +
        // httpsUrl 可能拒绝非 http(s) 地址返回空串：外层三元必须判净化结果，
        // 否则输出 src=""（部分浏览器会当当前页 URL 发请求）
        ((g.cover && httpsUrl(g.cover))
          ? '<img class="result-cover" src="' + esc(httpsUrl(g.cover)) + '" referrerpolicy="no-referrer" loading="lazy" decoding="async" alt="" />'
          : '<div class="result-cover-ph" aria-hidden="true">♪</div>') +
        '<div class="result-main"><div class="result-name">' + esc(g.name) + "</div>" +
        '<div class="result-sub">' + esc(g.artist) + (g.album ? " · " + esc(g.album) : "") + "</div></div>" +
        '<div class="result-vers">' + vers + "</div>" +
        '<div class="row-actions">' +
        // 无 sid 的歌（如酷狗 VIP 拿不到定位键）试听与投递都注定失败：
        // 两个按钮都不渲染，给占位文案而不是必报错的按钮
        (primary.sid
          ? (canPlay ? '<button class="btn btn-ghost btn-sm" data-act="preview" data-source="' + esc(primary.source) + '" data-sid="' + esc(primary.sid) + '">▶︎ 试听</button>' : "") +
            '<button class="btn btn-blue btn-sm" data-act="deliver" data-source="' + esc(primary.source) + '" data-sid="' + esc(primary.sid) + '">投递</button>'
          : '<span class="result-sub">暂无可播放版本</span>') +
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
          ? '<img class="result-cover" src="' + esc(httpsUrl(it.cover)) + '" referrerpolicy="no-referrer" loading="lazy" decoding="async" alt="" />'
          : '<div class="result-cover-ph" aria-hidden="true">♪</div>') +
        '<div class="result-main"><div class="result-name">' + esc(it.name) + "</div>" +
        '<div class="result-sub">' + esc(it.sub || "") + (it.count ? " · " + esc(it.count) + " 首" : "") + "</div></div>" +
        '<div class="result-vers"><span class="src-dot ' + srcClass(it.source) + '">' + esc(it.sourceName || it.source) + "</span></div>" +
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
    if ($("#searchInput").value.trim()) {
      doSearch();
    } else {
      // 空关键词切 tab：清掉旧类型的结果与筛选状态，别让「歌单」标签下挂着单曲结果
      searchGroups = [];
      searchItems = [];
      searchFilter = "all";
      $("#searchResults").innerHTML = '<div class="empty-state">输入关键词开始搜索</div>';
    }
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
    // 重建列表会销毁试听按钮所在的 DOM：先停掉在播的试听，否则 _audio 继续播放
    // 而界面上不再存在任何停止控件（与 doSearch 开头的清理同口径）
    stopPreview();
    abortPreview();
    renderSearchResults(false);
    // 读屏同步筛选后的条数：搜索时的 live 播报是未筛选总数，筛选后不更新
    // 会让读屏用户对「实际可见条数」拿着旧数
    var visible = $$("#searchResults .result-row").length;
    var live = $("#searchLive");
    if (live) live.textContent = searchFilter === "all" ? "已显示全部音源，" + visible + " 条结果" : "已按音源筛选，" + visible + " 条结果";
  });
  $("#searchInput").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); doSearch(); }
  });
  // 输入即搜：250ms debounce，避免每敲一个字都扇出三平台
  $("#searchInput").addEventListener("input", function () {
    clearTimeout(_debounceTimer);
    var v = this.value.trim();
    if (!v) {
      // 清空输入时同步清掉旧关键词的结果：结果区与输入框状态错配会误导
      searchGroups = [];
      searchItems = [];
      searchFilter = "all";
      updateFilterBar();
      $("#searchResults").innerHTML = '<div class="empty-state">输入关键词开始搜索</div>';
      return;
    }
    _debounceTimer = setTimeout(function () { doSearch(); }, 250);
  });

  /* ─────────── 账号扫码 ─────────── */
  var _acctSeq = 0;
  async function loadAccounts() {
    var seq = ++_acctSeq;
    withSkeleton("#acctGrid", skeletonRows(3, 110));
    var d;
    try {
      d = await jget("/api/accounts", { signal: panelSignal("accounts") });
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
        : srcLogoImg(a.source, "acct-src-logo");
      return '<div class="acct" style="--sc:' + srcVar(colors, a.source, "var(--sub, #888)") + ';--sci:' + srcVar(inks, a.source, "var(--sub)") + ';animation-delay:' + i * 70 + 'ms">' +
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
      // type 是二维码样式参数（后端按平台分支绘制），与音源枚举里的 qq 无关
      var r = await jpost("/api/qr/start", { source: source, type: "qq" });
      // 弹窗已关 / 已被另一次 openQR 取代：这次的 ticket 直接取消，
      // 否则会成为服务端孤儿会话，且下面的轮询还会一直打 /api/qr/status
      if (token !== _qrOpenToken || $("#qrMask").hidden) {
        if (r && r.ticket) jpost("/api/qr/cancel", { ticket: r.ticket }, { skip401: true }).catch(function () {});
        return;
      }
      qrTicket = r.ticket;
      // r.qr 是后端自产的 data:image/png;base64（core/login.py 组包），不是外链，
      // 过不了 httpsUrl 白名单（data: 被刻意丢弃）；这里钉死 data URI 形态——
      // 形态不对（被劫持/改字段才可能出现）就当没有图，落回 qrUrl 或错误提示
      var qrData = typeof r.qr === "string" && r.qr.indexOf("data:image/png;base64,") === 0 ? r.qr : "";
      if (qrData) { $("#qrImg").src = qrData; setQRState("请使用手机 App 扫码", ""); }
      else if (r.qrUrl) {
        // httpsUrl 白名单拒绝时不渲染 <a href="">（点击等于刷新当前页）：
        // 降级成纯文本展示，用户仍可复制
        var safeUrl = httpsUrl(r.qrUrl);
        if (safeUrl) {
          setQRStateHTML('<a href="' + esc(safeUrl) + '" target="_blank" rel="noopener">点此打开二维码页面</a>（或复制链接到浏览器）');
        } else {
          setQRStateHTML('<span class="mono">' + esc(r.qrUrl) + "</span>（复制到浏览器打开）");
        }
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
    qrTimer = setTimeout(function () { pollQROnce(token); }, QR_POLL_MS);
  }
  async function pollQROnce(token) {
    qrTimer = null;
    if (token !== _qrOpenToken || !qrTicket || $("#qrMask").hidden) return;
    // 页签在后台时暂停查询但照常重排（与 pollLogsOnce 同口径）：扫码弹窗开着
    // 切走页签不该空转轮询；直接 return 会杀掉自递归定时器，切回后不再感知扫码
    if (document.hidden) { scheduleQRPoll(token); return; }
    if (_qrPollBusy) {
      // 上一个（已作废代次的）查询仍在途：本轮让路但不放弃——原地重排。直接
      // return 会把新代次的轮询一次性杀死（旧请求返回时 token 失配早退、不负责
      // 恢复新轮询），「刷新二维码」后弹窗永久停在「请扫码」不感知扫码结果
      scheduleQRPoll(token);
      return;
    }
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
        // 捕获代次再关窗：裸定时器会在 900ms 窗口内关掉并 cancel 用户刚开的新
        // 扫码会话（closeQR 发的是当下 qrTicket，新会话被无声取消、弹窗闪退）
        var tok = _qrOpenToken;
        setTimeout(function () { if (tok === _qrOpenToken) closeQR(); }, 900);
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
    // 只在有真实 ticket 时发 cancel。start 在途关窗时 qrTicket 为 null，发出去
    // 的 { ticket: null } 会被服务端 str() 成 "None" 查表落空、必然浪费——
    // 真正的取消发生在 start 响应返回后的「弹窗已关」分支（token 不匹配即补发）
    if (qrTicket) {
      jpost("/api/qr/cancel", { ticket: qrTicket }, { skip401: true }).catch(function () {});
    }
    qrTicket = null;
    $("#qrMask").hidden = true;
    // isConnected 守卫对齐 closeMore：触发按钮随面板重渲染销毁后，
    // focus() 落在游离节点上等于焦点丢失到 body
    if (_qrPrevFocus && _qrPrevFocus.isConnected && _qrPrevFocus.focus) _qrPrevFocus.focus();
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
      d = await jget("/api/stats?days=14", { signal: panelSignal("stats") });
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
        // tabindex=0 让 data-tip 宿主可键盘聚焦：focusin 才有机会触发 tooltip。
        // aria-label 同步全文：tooltip 是纯视觉实现，读屏拿不到 data-tip
        // （与结果徽标 vchip 的 aria-label 做法一致）
        '<div class="err-detail" role="button" tabindex="0" aria-label="' + esc(t.detail) + '" data-tip="' + esc(t.detail) + '">' + esc(t.detail) + "</div>" +
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
      d = await jget("/api/history", { signal: panelSignal("stats") });
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
    if (svg) {
      // 二次加载失败时清掉上一轮的旧图：否则错误提示与旧图上下/叠加显示。
      // innerHTML 清空会带走 aria-labelledby 引用的 <title>，悬空引用让读屏
      // 退化成读「graphic」——错误态先摘掉引用，重绘成功后由绘制方补回
      svg.innerHTML = "";
      svg.removeAttribute("aria-labelledby");
      svg.setAttribute("aria-busy", "false");
    }
    // 上一轮成功时生成的 sr-only 数据表一并清掉：留着会让读屏在错误态
    // 继续播报陈旧的逐日数据（与 drawTrend 开头的移除对齐）
    var old = $("#trendTable");
    if (old) old.remove();
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
    // chartError 的错误态摘掉了 aria-labelledby（<title> 随 innerHTML 清空）：
    // 重绘成功后 <title id="trendA11y"> 已重建，引用要补回来
    svg.setAttribute("aria-labelledby", "trendA11y");
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
        '<span class="bar-track"><span class="bar-fill" style="--bc:' + srcVar(colors, r.src, "var(--blue)") + ';--w:0"></span></span>' +
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
        // 详情列单行截断（td.cell-detail 承担）：补 data-tip 让全文可查（tooltip
        // 系统支持键盘 focus 触发），aria-label 同步全文给读屏（与 err-detail 同口径）
        '<td class="cell-detail"><span role="button" tabindex="0" aria-label="' +
        esc(r.detail || "--") + '" data-tip="' + esc(r.detail || "--") + '">' + esc(r.detail || "--") + "</span></td></tr>";
    }).join("");
  }

  /* ─────────── 黑白名单 ─────────── */
  var aclData = { mode: "off", blacklist: [], whitelist: [] };
  var _aclSeq = 0;
  var _aclSaving = false;
  async function loadAcl(silent) {
    var seq = ++_aclSeq;
    // silent（保存后的刷新）：已有数据，跳过骨架屏原地重绘，否则每增删一条
    // 列表都闪一次 3 行 shimmer
    if (!silent) {
      withSkeleton("#blackList", skeletonRows(3, 44));
      withSkeleton("#whiteList", skeletonRows(3, 44));
    }
    var d;
    try {
      d = await jget("/api/acl", { signal: panelSignal("acl") });
    } catch (e) {
      if (seq !== _aclSeq || (e && e.aborted)) return;
      // silent（保存后的静默刷新）失败时保留现视图：数据已确认落盘（服务端
      // 返回 ok 才走到这里），把刚保存成功的列表打成错误态是误导
      if (!silent) {
        renderError("#blackList", e.message);
        renderError("#whiteList", e.message);
      }
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
      return '<div class="le-item"><span>' + esc(it) + '</span><button data-act="acl-del" data-list="' + sel.slice(1) + '" data-i="' + i + '" data-tip="删除" aria-label="删除 ' + esc(it) + '">✕</button></div>';
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
      // silent 重绘：数据已在本地（merged），成功后只需同步视图，不闪骨架屏。
      // r 需判空（2xx 空体/解析失败时 api() 返回 null）：直接 r.ok 会抛
      // TypeError 变成 unhandled rejection，输入框不清空、无提示
      if (r && r.ok) { aclData = merged; toast("黑白名单已保存", "ok"); return loadAcl(true).then(function () { return true; }); }
      toast((r && r.error) || "保存失败", "error");
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
  // 配置表单「干净态快照」：renderConfig 完成时记录全部控件值，cfgDirty() 用
  // 实时 DOM 与之比对。面板 DOM 常驻（切面板只隐藏不销毁），干净时重入不再
  // 重拉重渲染——原先每次进配置页都整块重建，改了一半的内容切个面板就丢
  var _cfgCleanSnapshot = "";
  function cfgFormSnapshot() {
    var parts = [];
    $$("#configBody input,#configBody select").forEach(function (el) { parts.push(el.id + "=" + el.value); });
    $$("#configBody .sw").forEach(function (el) { parts.push(el.id + "=" + (el.classList.contains("on") ? "1" : "0")); });
    return parts.join("|");
  }
  function cfgDirty() {
    return !!(_cfgCleanSnapshot && $("#configBody .cfg-group") && cfgFormSnapshot() !== _cfgCleanSnapshot);
  }
  function _cfgForget() {
    // 清掉「已加载」状态：错误态 / 登出现场里 DOM 已不代表服务端配置，
    // 保留快照会让 loadConfig 早退（重试按钮空转）、saveConfig 拿旧基线整包覆盖
    _cfgCleanSnapshot = "";
    cfgData = null;
  }
  async function loadConfig() {
    // 早退判据必须同时确认 DOM 在位：renderError 后 #configBody 没有 .cfg-group，
    // 只看快照会把错误态误判成「干净已加载」，错误态里的「重试」永久空转
    if (_cfgCleanSnapshot && $("#configBody .cfg-group") && !cfgDirty()) {
      // 已加载且无未保存修改：跳过重拉（DOM 与服务端一致），不闪骨架屏
      setLoaded("#configBody");
      return;
    }
    if (_cfgCleanSnapshot && cfgDirty()) {
      var go = await confirmBox({
        title: "未保存的配置",
        text: "配置页有未保存的修改，重新载入将丢弃这些修改。",
        okText: "丢弃并重载",
      });
      if (!go) return;
    }
    var seq = ++_cfgSeq;
    withSkeleton("#configBody", skeletonRows(4, 88));
    var d;
    try {
      d = await jget("/api/config", { signal: panelSignal("config") });
    } catch (e) {
      if (seq !== _cfgSeq || (e && e.aborted)) return;
      _cfgForget();
      renderError("#configBody", e.message);
      return;
    }
    if (seq !== _cfgSeq) return;
    setLoaded("#configBody");
    // 后端返回 200 但没有 config 字段时不能悄悄留着骨架屏：
    // renderConfig 会直接 return，用户永远看到 shimmer
    if (!d || !d.config) {
      _cfgForget();
      renderError("#configBody", "配置数据为空，请检查插件端 /api/config 响应");
      return;
    }
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
    // 折叠组不再用原生 <details>（展开是瞬间完成、无法动画）：
    // 手写 .cfg-head + grid-template-rows 0fr/1fr 过渡实现高度动画
    var _cfgSeq2 = 0;
    function group(title, inner, count) {
      var gid = "cfgbody-" + (++_cfgSeq2);
      return '<div class="cfg-group open">' +
        '<button type="button" class="cfg-head" aria-expanded="true" aria-controls="' + gid + '">' +
        '<span class="arrow" aria-hidden="true">▶︎</span>' + esc(title) +
        '<span class="cfg-count">' + (count != null ? count : 0) + " 项</span></button>" +
        '<div class="cfg-body" id="' + gid + '"><div class="cfg-body-in"><div class="cfg-items">' + inner + "</div></div></div></div>";
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
      // 配置里存着已下架档位（旧版面板曾提供 kg viper 系）时 select 匹配不到任何
      // option，value 变 ""：显示空白且保存必被服务端拒绝成死锁。回落到第一项
      // （恒为 auto），下次保存顺带把无效旧值归位
      var known = options.some(function (o) { return o[0] === value; });
      var cur = known ? value : options[0][0];
      return '<div class="cfg-item"><label for="cfg-' + esc(id) + '">' + esc(label) + "</label>" +
        '<select class="input" id="cfg-' + esc(id) + '">' +
        options.map(function (o) { return '<option value="' + esc(o[0]) + '"' + (o[0] === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>"; }).join("") +
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
    general += textItem("apiTimeoutSec", "音源请求超时 (秒)", "5-120，弱网或自建上游响应慢时调大，改完即时生效", c.apiTimeoutSec, "number", ' min="5" max="120" step="1"'); generalCount++;
    html += group("基础", general, generalCount);

    /* 功能开关：解析 / 卡片 / 图片渲染 */
    var feat = "";
    FEATURE_TOGGLES.forEach(function (id) {
      var spec = CFG_BOOLS.filter(function (s) { return s[0] === id; })[0];
      if (spec) feat += boolItem(spec[0], spec[1], spec[2], c[spec[0]]);
    });
    html += group("功能开关", feat, FEATURE_TOGGLES.length);

    var srcQ = [["auto", "自动"], ["standard", "标准"], ["higher", "较高"], ["exhigh", "极高"], ["lossless", "无损"], ["hires", "Hi-Res"], ["jyeffect", "高清臻音"], ["vivid", "臻音全景声"], ["sky", "沉浸环绕"], ["dolby", "杜比"], ["jymaster", "超清母带"]];
    // kg 只有旧 /song/url 支持的三档（quality.py 排除说明）：high/super/viper 属新端点，选了也无效
    var kgQ = [["auto", "自动"], ["128", "标准"], ["320", "HQ"], ["flac", "无损"]];
    var qqQ = [["auto", "自动"], ["128", "标准"], ["320", "HQ"], ["flac", "无损"], ["atmos", "全景声"], ["master", "臻品母带"], ["atmos_db", "杜比"]];

    var ncmHtml = textItem("ncm-apiBase", "API 地址", "网易云 API 服务（api-enhanced），留空则停用网易云音源；自建默认端口 3000", c.ncm.apiBase);
    ncmHtml += selectItem("ncm-quality", "最高音质", "", c.ncm.quality, srcQ);
    ncmHtml += boolItem("ncm-qualityUnblock", "VIP 歌自动解灰", "拿不到 URL 时用 unblock 音源兜底", c.ncm.qualityUnblock);
    ncmHtml += staticItem("登录状态", c.ncm.hasCookie ? "已有 Cookie" : "匿名（可在「账号扫码」登录）");
    html += group("网易云音乐", ncmHtml, 4);

    var kgHtml = textItem("kg-apiBase", "API 地址", "酷狗 API 服务（KuGouMusicApi），留空则停用酷狗音源；本机自建默认端口 4000", c.kg.apiBase);
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
    delivery += textItem("compressBitrate", "压缩码率 (kbps)", "32-320，与服务端限制一致（关闭压缩用「失败压缩重试」开关）", c.compressBitrate, "number", ' min="32" max="320" step="32"'); deliveryCount++;
    delivery += textItem("downloadTimeout", "下载超时 (ms)", "5000-600000", c.downloadTimeout, "number", ' min="5000" max="600000" step="5000"'); deliveryCount++;
    delivery += textItem("keepFileSec", "临时文件保留 (秒)", "5-3600", c.keepFileSec, "number", ' min="5" max="3600" step="5"'); deliveryCount++;
    html += group("发送通道（多平台适配）", delivery, deliveryCount);

    var webuiHtml = boolItem("webui-enable", "启用 WebUI", "", c.webui.enable);
    webuiHtml += textItem("webui-host", "监听地址", "0.0.0.0 允许局域网访问", c.webui.host);
    webuiHtml += textItem("webui-port", "端口", "改动需重载插件生效", c.webui.port, "number", ' min="1" max="65535" step="1"');
    webuiHtml += textItem("webui-sessionHours", "会话有效期 (小时)", "1-168，登录态的有效时长，改完对新登录生效", c.webui.sessionHours, "number", ' min="1" max="168" step="1"');
    webuiHtml += textItem("webui-hostAllowlist", "域名白名单", "逗号分隔完整主机名（精确匹配，不含子域名）；留空只允许 IP / localhost", (c.webui.hostAllowlist || []).join(", "));
    webuiHtml += boolItem("webui-secureCookie", "Cookie Secure 标志", "https 反代访问时开启；http 直连必须保持关闭，否则无法登录", c.webui.secureCookie);
    html += group("WebUI 面板", webuiHtml, 6);

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
    // 折叠组头部：点击切换 .open，高度动画交给 CSS 的 grid-template-rows 过渡
    $$(".cfg-head", body).forEach(function (head) {
      head.addEventListener("click", function () {
        var g = head.closest(".cfg-group");
        if (!g) return;
        var open = g.classList.toggle("open");
        head.setAttribute("aria-expanded", open ? "true" : "false");
      });
    });
    // 配置页的下拉框同样走自定义 widget（原生弹层无法套主题/动画）
    enhanceSelects(body);
    // 渲染完成即「干净态」基线：之后 cfgDirty() 以实时 DOM 与此比对
    _cfgCleanSnapshot = cfgFormSnapshot();
  }
  function collectConfig() {
    var patch = {};
    patch.defaultSource = bind("defaultSource");
    patch.maxList = numOr(bind("maxList"), 10);
    patch.identifyPrefix = bind("identifyPrefix");
    patch.cooldownSec = numOr(bind("cooldownSec"), 0);
    patch.rateLimitMs = numOr(bind("rateLimitMs"), 0);
    patch.apiTimeoutSec = numOr(bind("apiTimeoutSec"), 20);
    patch.ncm = { apiBase: bind("ncm-apiBase"), quality: bind("ncm-quality"), qualityUnblock: swVal("ncm-qualityUnblock") };
    patch.kg = { apiBase: bind("kg-apiBase"), quality: bind("kg-quality"), trialFallback: swVal("kg-trialFallback") };
    patch.qq = { quality: bind("qq-quality"), trialFallback: swVal("qq-trialFallback") };
    CFG_BOOLS.forEach(function (spec) {
      if (has(spec[0])) patch[spec[0]] = swVal(spec[0]);
    });
    patch.compressBitrate = numOr(bind("compressBitrate"), 128);
    patch.downloadTimeout = numOr(bind("downloadTimeout"), 120000);
    patch.keepFileSec = numOr(bind("keepFileSec"), 60);
    patch.webui = {
      enable: swVal("webui-enable"),
      host: bind("webui-host"),
      port: numOr(bind("webui-port"), 17818),
      sessionHours: numOr(bind("webui-sessionHours"), 12),
      // 逗号/中文逗号分隔 → 字符串数组（后端逐项归一：去空白/去前导点/小写）
      hostAllowlist: bind("webui-hostAllowlist")
        .split(/[,，]/)
        .map(function (s) { return s.trim(); })
        .filter(Boolean),
      secureCookie: swVal("webui-secureCookie"),
    };
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
  // 只回填一个配置控件（被拒项用）：开关按 role="switch" 的 .sw.on 结构，
  // 其余按输入框 value 赋值；select 是自定义下拉（.csel），只写隐藏载体的
  // value 可见标签不会变——必须走 syncSelect 同步按钮文案
  function setCfgField(id, v) {
    var el = $("#cfg-" + id);
    if (!el) return;
    if (el.classList.contains("sw")) {
      el.classList.toggle("on", !!v);
      el.setAttribute("aria-checked", el.classList.contains("on") ? "true" : "false");
    } else {
      el.value = v == null ? "" : String(v);
      if (el.tagName === "SELECT") syncSelect(el);
    }
  }
  async function saveConfig() {
    // DOM 在位守卫：错误态/骨架屏期间（.cfg-group 不存在）cfgData 仍是旧值，
    // collectConfig 对缺失控件全取默认值会整包 POST 覆盖服务端真实配置
    if (!cfgData || !$("#configBody .cfg-group")) {
      toast("配置页尚未加载完成，稍后再试", "warn");
      return;
    }
    var patch = collectConfig();
    var btn = $('[data-act="cfg-save"]');
    if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }
    try {
      var r = await jpost("/api/config/save", { config: patch });
      if (r.rejected && r.rejected.length) {
        // 有被拒项时本地 patch 不等于实际生效值：被拒字段必须回填服务端真实值
        // 否则面板上留着「没存进去的值」误导用户。只回填被拒字段、不全量重渲染
        // ——renderConfig() 会把其它分组未保存的编辑一并清掉（违背本页约定）
        var reasons = r.rejected.map(function (k) {
          var why = r.rejectedReasons && r.rejectedReasons[k];
          return why ? k + "（" + why + "）" : k;
        });
        toast("部分项被拒绝：" + reasons.join(", "), "warn");
        var fresh = await jget("/api/config");
        if (fresh && fresh.config) {
          cfgData = fresh.config;
          r.rejected.forEach(function (key) {
            var parts = String(key).split(".");
            if (parts.length === 2) {
              var node = fresh.config[parts[0]];
              if (node && typeof node === "object") {
                // 控件 id 前缀与配置节点名不同（scheduler 节点的控件 id 是 sched-*），
                // 直接拼节点名会 setCfgField 落空、被拒值留在界面上还被快照标成已保存
                var idPrefix = parts[0] === "scheduler" ? "sched" : parts[0];
                setCfgField(idPrefix + "-" + parts[1], node[parts[1]]);
              }
            } else {
              var topVal = fresh.config[key];
              if (topVal && typeof topVal === "object") {
                // 整节点被拒（如 scheduler「写入底层配置失败」）：节点全部字段都
                // 留在服务端值，逐一回填——只按节点名 setCfgField 会落空，用户
                // 未保存的值反被快照标成「已保存」
                var p2 = key === "scheduler" ? "sched" : key;
                Object.keys(topVal).forEach(function (f) {
                  setCfgField(p2 + "-" + f, topVal[f]);
                });
              } else {
                setCfgField(key, topVal);
              }
            }
          });
        }
      } else {
        toast("配置已保存" + (r.note ? " " + r.note : ""), "ok");
        syncConfigView(patch);
      }
      // 保存流程结束后的 DOM 状态即新基线：成功分支（DOM=已保存值）与回填分支
      // （被拒字段已还原成服务端值、其余未保存修改继续算脏）都成立
      _cfgCleanSnapshot = cfgFormSnapshot();
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
      // 失败态带重试入口（与其他面板错误态的 panel-retry 同款交互）
      box.innerHTML =
        '<div class="rr-name">解析失败</div><div class="rr-sub">' + esc(e.message) +
        '<br><button class="btn btn-ghost btn-sm" style="margin-top:10px" data-act="do-resolve">重试</button></div>';
    }
  }

  /* ─────────── 会话 ─────────── */
  var _ssSeq = 0;
  async function loadSessions() {
    var seq = ++_ssSeq;
    withSkeleton("#sessTbl tbody", '<tr><td colspan="4"><div class="skeleton" aria-hidden="true" style="height:60px"></div></td></tr>');
    var d;
    try {
      d = await jget("/api/auth/sessions", { signal: panelSignal("sessions") });
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
      return "<tr><td>" + esc(s.id) + (s.current ? ' <span class="badge ok">当前</span>' : "") +
        // 服务端检测到会话异 IP 重放（疑似被窃）时标警：_auth 下发 ipChanged
        // 就是给「我的会话」看的，漏渲染等于设计好的可见性缺失
        (s.ipChanged ? ' <span class="badge err" data-tip="该会话的来源 IP 发生变化，若非本人操作请立即下线">⚠ IP 变化</span>' : "") + "</td>" +
        "<td>" + fmtDateTime(s.created) + "</td>" +
        "<td>" + fmtDateTime(s.lastSeen) + "</td>" +
        "<td>" + (s.current ? "--" : '<button class="btn btn-ghost btn-sm" data-act="sess-revoke" data-id="' + esc(s.id) + '">下线</button>') + "</td></tr>";
    }).join("");
  }

  /* ─────────── 运行日志 ─────────── */
  var _logSeq = 0;
  var _logLoadSeq = 0; // loadLogsPanel 的进入序号：快速切走再切回时作废旧请求的渲染
  var _logTimer = null;
  var _logBusy = false;
  var _clearedAt = 0;   // 清屏界线（秒）：之后的条目才显示
  var _logNewestTs = 0; // 已见条目里最大的服务端 ts（清屏以它为界，避免客户端时钟偏移吞日志）
  function logLine(e) {
    var cls = e.level === "ERROR" ? "err" : e.level === "WARNING" ? "warn" : "info";
    // 后端存的是结构化字段：级别徽标 / 时间 / 正文分开渲染，
    // ts（秒级 unix）同时是「清屏」过滤的判据
    return '<div class="log-line log-' + cls + '"><span class="log-lv">' + esc(e.level) + "</span>" +
      '<span class="log-ts">' + fmtTime(e.ts) + "</span>" + esc(e.msg) + "</div>";
  }
  function renderLogEntries(entries) {
    var box = $("#logConsole");
    // 清屏界线同样要过滤整屏渲染：服务端重启走 seq 回退全量重取时，
    // 不过滤会把已清掉的日志整屏带回来（append 路径有过滤，这里也要有）。
    // 全量路径同样按 LOG_MAX 封顶：反复触发回退重取时整屏重建不受
    // append 路径的节点数上限约束，会无界累积
    entries = entries.filter(function (e) { return num(e.ts) > _clearedAt; }).slice(-LOG_MAX);
    if (!entries.length) {
      box.innerHTML = _clearedAt ? '<div class="empty-state">已清屏（仅本地显示）</div>' : '<div class="empty-state">暂无日志</div>';
      return;
    }
    _logNewestTs = entries.reduce(function (m, e) { return Math.max(m, num(e.ts)); }, _logNewestTs);
    box.innerHTML = entries.map(logLine).join("");
    box.scrollTop = box.scrollHeight;
  }
  var LOG_MAX = 400;   // DOM 节点数封顶：面板开久了 insertAdjacentHTML 会累积上万节点
  function appendLogEntries(entries) {
    var box = $("#logConsole");
    // 清屏后不立刻被 2.5s 后的新日志覆盖掉「已清屏」提示：过滤界线前的条目。
    // 严格大于：界线取的是清屏时最新一条的 ts，等于界线的恰是已被清掉的那条
    entries = entries.filter(function (e) { return num(e.ts) > _clearedAt; });
    _logNewestTs = entries.reduce(function (m, e) { return Math.max(m, num(e.ts)); }, _logNewestTs);
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
    // seq 守卫对齐其它面板 loader：快速切走再切回时，第一次的 await 返回后
    // 不得覆盖第二次的骨架屏（离开面板时 curPanel 变化，这里以进入序号判定）
    var seq = ++_logLoadSeq;
    $("#logConsole").innerHTML = '<div class="skeleton" aria-hidden="true" style="height:60px"></div>';
    try {
      var d = await jget("/api/logs", { signal: panelSignal("logs") });
      // api() 对 2xx 空体/解析失败统一抛协议错误（不再返回 null），d 恒为对象
      if (d.seq < _logSeq) {
        // 服务端重启后 seq 归零：检测回退并全量重取，
        // 否则 after=<旧大值> 永远等不到新日志
        _logSeq = 0;
        var full = await jget("/api/logs");
        if (seq !== _logLoadSeq) return;
        _logSeq = num(full.seq);
        renderLogEntries(full.entries || []);
      } else {
        if (seq !== _logLoadSeq) return;
        _logSeq = num(d.seq);
        renderLogEntries(d.entries || []);
      }
    } catch (e) {
      // 与其它面板 loader 一致：失败渲染错误态 + 重试按钮，别让骨架屏长亮
      if (!(e && e.aborted) && seq === _logLoadSeq) renderError("#logConsole", e.message);
    }
    if (seq === _logLoadSeq) scheduleLogPoll();
  }
  // 递归 setTimeout 而非 setInterval：interval 会与上一次请求重叠，
  // 没有 in-flight 守卫时同一批日志可能被追加两次
  function scheduleLogPoll() {
    if (_logTimer) clearTimeout(_logTimer);
    _logTimer = setTimeout(pollLogsOnce, LOG_POLL_MS);
  }
  async function pollLogsOnce() {
    _logTimer = null;
    // 只有真的离开日志面板才停表。隐藏页签 / 登录遮罩 / 上一轮未完这三个早退
    // 必须照常排下一轮：直接 return 会杀掉自递归定时器，切回页签后日志永久停更
    if (curPanel !== "logs") return;
    if (document.hidden || !$("#loginMask").hidden || _logBusy) { scheduleLogPoll(); return; }
    _logBusy = true;
    // 代际快照：in-flight 期间用户切走再切回，loadLogsPanel 会全量重渲染并推进
    // _logLoadSeq——旧请求返回后不得再追加（否则已有条目重复一遍）或回写旧 _logSeq
    var gen = _logLoadSeq;
    try {
      var d2 = await jget("/api/logs?after=" + _logSeq, { signal: panelSignal("logs") });
      if (gen !== _logLoadSeq) return;
      if (num(d2.seq) < _logSeq) {
        // seq 回退 = 服务端重启，必须全量重取
        _logSeq = 0;
        var full = await jget("/api/logs", { signal: panelSignal("logs") });
        if (gen !== _logLoadSeq) return;
        _logSeq = num(full.seq);
        renderLogEntries(full.entries || []);
      } else {
        _logSeq = num(d2.seq);
        if ((d2.entries || []).length) appendLogEntries(d2.entries);
      }
    } catch (e) { /* 下轮重试 */ }
    finally {
      // 复位必须走 finally：早退 return（代际失配）跳过复位会让轮询永久停摆
      _logBusy = false;
    }
    if (curPanel === "logs") scheduleLogPoll();
  }

  /* ─────────── 确认对话框（替代原生 confirm） ─────────── */
  var _confirmPrevFocus = null;
  var _confirmResolve = null;
  function confirmBox(opts) {
    // 重入守卫：mask 全屏拦截下物理上开不了第二个，但 promise 级的防重入
    // 保证并发调用不会覆盖 _confirmResolve 导致第一个调用永远挂起
    if (_confirmResolve) return Promise.resolve(false);
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
    if (_confirmPrevFocus && _confirmPrevFocus.isConnected) _confirmPrevFocus.focus();
    _confirmPrevFocus = null;
    if (r) r(ok);
  }
  $("#confirmOk").addEventListener("click", function () { closeConfirm(true); });
  $("#confirmCancel").addEventListener("click", function () { closeConfirm(false); });
  $("#confirmMask").addEventListener("click", function (e) {
    if (e.target === this) closeConfirm(false);
  });
  // 扫码弹窗与确认框同口径：点遮罩关闭（Esc 已有）。扫码轮询在 closeQR 里作废
  $("#qrMask").addEventListener("click", function (e) {
    if (e.target === this) closeQR();
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
        // 先清干净现场（关弹窗会归还焦点），再通知服务端。
        // 现场作废（代际推进 + _cfgForget）与 401 路径共用同一段：漏掉会让重登后
        // loadConfig 早退、以登出前的旧配置 DOM 为基线整包覆盖服务端配置
        teardownLocal();
        invalidatePanelState();
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
          loadOverview(true) /* 静默：不重建骨架屏、数字不重滚 */;
        } catch (err) { toast(err.message, "error"); }
      },
      "do-search": doSearch,
      "do-resolve": doResolve,
      // 错误态里的「重试」：按当前面板重新走对应 loader
      "panel-retry": function () {
        var fn = PANEL_LOADERS[curPanel];
        if (fn) fn().catch(function (e) { if (e && !e.aborted) toast(e.message, "error"); });
      },
      "preview": function () { playPreview(t.dataset.source, t.dataset.sid, t); },
      "deliver": function () { deliverPlay(t.dataset.source, t.dataset.sid, t); },
      "remote-refresh": loadScopes,
      "history-refresh": function () { loadHistory().catch(function () {}); },
      "logs-clear": function () {
        // 以**服务端**最新一条的 ts 为清屏界线：用客户端 Date.now() 的话，
        // 本机时钟快于服务端时，偏移窗口内的新日志会被整批吞掉。
        // 还没拉到过任何日志时退回本地时钟（空屏清屏，无实际影响）
        _clearedAt = _logNewestTs || Math.floor(Date.now() / 1000);
        var box = $("#logConsole");
        // 只同步 seq，失败无碍：本地已清屏，下一轮轮询会照常带回新日志。
        // 代际快照对齐 pollLogsOnce：in-flight 期间切走再重进日志面板，
        // _logLoadSeq 已推进，旧请求返回不得回写旧 _logSeq（after=旧值会漏日志）
        if (_logSeq) {
          var clearGen = _logLoadSeq;
          jget("/api/logs?after=" + _logSeq).then(function (d) {
            if (clearGen === _logLoadSeq && d) _logSeq = num(d.seq);
          }).catch(function () {});
        }
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
        if (!oldV) { toast("请输入当前密码", "warn"); $("#oldPwd").focus(); return; }
        if (!newV || newV.length < 6) { toast("新密码至少 6 位", "warn"); return; }
        // 与服务端同口径的弱口令前置校验（纯数字短密码在 600k PBKDF2 下仍是分钟级离线爆破）
        if (/^\d+$/.test(newV) && newV.length < 8) {
          toast("纯数字密码至少 8 位，建议混入字母或符号", "warn"); return;
        }
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
  // 凭证导入的 Enter 提交（同页 black/white 输入框都有，此处补齐）
  $("#cookieValue").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); var b = $('button[data-act="cookie-save"]'); if (b) b.click(); }
  });
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
    var dlgSel = !$("#loginMask").hidden
      ? ".login-box"   // 登录遮罩是全屏模态：Tab 不能穿透到被遮住的侧边栏/底栏
      : $("#qrMask").hidden
      ? (!$("#confirmMask").hidden ? "#confirmDialog" : (!$("#moreSheet").hidden ? "#moreSheet" : null))
      : "#qrDialog";
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
    }, OVERVIEW_POLL_MS);
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
    // closeQR 的 cancel 请求在 beforeunload 场景发不出去（无 keepalive）：
    // 本地收尾即可，服务端孤儿会话由 10 分钟自然过期兜底
    _qrOpenToken++;
    clearTimeout(qrTimer);
    qrTimer = null;
    qrTicket = null;
    $("#qrMask").hidden = true;
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
      // 版本只对已登录会话下发（公开路由不泄漏版本号）：空值时隐藏徽标而不是显示孤零零的「v」
      $("#chipVersion").innerHTML = meta.version ? '<span class="dot"></span>v' + esc(meta.version) : "";
      $("#chipVersion").hidden = !meta.version;
      if (!meta.authed) { showLogin(); return; }
      boot();
    } catch (e) {
      // 断连与未登录必须可区分。toast 要放在 showLogin 之后入队：showLogin 内部
      // 会 clearToasts()，先弹的会被自己清掉；toastBox(z:500) 在登录遮罩(z:200)
      // 之外且层级更高，登录框上能正常看到这条错误
      showLogin();
      // 顶栏状态芯片不能停在「加载中」：loadOverview 的 catch 有复位（其错误态
      // 渲染含复位置），init 失败走不到 loadOverview，这里手动复位为「状态未知」
      var mc = $("#chipMaster");
      if (mc) { mc.classList.remove("ok"); mc.innerHTML = '<span class="dot"></span>状态未知'; }
      toast("无法连接面板服务：" + e.message, "error");
    }
  }
  // unload 期间 fetch 基本发不出去：只停本地定时器/在途请求（closeQR 的 cancel
  // 请求卸载场景下无谓且发不出去，不调用）
  // pagehide 而非 beforeunload：后者触发后页面进 bfcache，恢复（pageshow）时
  // 定时器不会自动重建，面板会静默停更到手动刷新。pagehide 停、pageshow 按当前
  // 面板重建轮询（overviewTimer 的 interval 回调自带 hidden/登录态守卫）
  window.addEventListener("pagehide", stopAllPolls);
  window.addEventListener("pageshow", function (e) {
    // 只有 bfcache 恢复需要重建轮询/数据：首次加载 pageshow 同样会触发，此时
    // init() 的 /api/meta 还没回来，这里跑 loader 会与 boot() 的重入双发一次
    // 请求（登出态还必吃一次 401）。loginShown 守卫兜住「恢复时已登出」的形态
    if (!e.persisted || loginShown) return;
    // 无条件重启总览轮询：回调自带 hidden/登录态守卫。原先的
    // `!$("#loginMask").hidden` 判断的是「登录遮罩可见=已登出」——恰好写反，
    // 已登录（遮罩 hidden=true）反而不恢复，bfcache 恢复后总览静默停更
    startOverviewPoll();
    // 面板 loader 的在途请求不在 stopAllPolls 范围内（它只停搜索/解析/试听/QR
    // 与本地定时器）：重入当前面板拉一次最新数据，旧响应由各 loader 的代际守卫
    // 丢弃（logs 的轮询由 loadLogsPanel 内的 scheduleLogPoll 自行恢复）
    var fn = PANEL_LOADERS[curPanel];
    if (typeof fn === "function") Promise.resolve(fn()).catch(function () {});
  });
  // 漏网的 promise 异常兜底观测：只 console.warn 记录，不 preventDefault（不吞，
  // 控制台仍能看到原始 reason），避免个别路径的 rejection 静默消失无迹可查
  window.addEventListener("unhandledrejection", function (e) {
    console.warn("[MusicHub] unhandled rejection:", e && e.reason);
  });
  // 静态 HTML 里的两个原生 select（投递会话 / 凭证类型）也换成手写下拉框
  enhanceSelects(document);
  init();
})();
