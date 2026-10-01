/* 主题预置：在样式表加载前同步解析主题，防 FOUC（CSP 禁止内联脚本，故外置同源文件） */
var m = "auto";
try {
  var s = localStorage.getItem("mh-theme");
  if (s === "dark" || s === "light" || s === "auto") m = s;
} catch (e) {}
var dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
var d = document.documentElement;
d.dataset.themeMode = m;
d.dataset.theme = m === "auto" ? (dark ? "dark" : "light") : m;
