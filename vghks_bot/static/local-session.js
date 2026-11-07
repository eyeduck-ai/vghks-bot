"use strict";

window.LocalSession = (() => {
  const key = "vghks.local-session";
  const resourcePath = /^\/api\/(?:analysis\/asset|accounts\/[a-f0-9]{32}\/(?:analysis\/asset|reviews\/diagnostics\/export))$/;
  let active;

  function valid(value) {
    return /^[A-Za-z0-9_-]{43}$/.test(value?.session || "") && /^[a-f0-9]{64}$/.test(value?.resource || "");
  }
  function read() {
    if (active) return active;
    try {
      const value = JSON.parse(window.sessionStorage.getItem(key));
      if (valid(value)) return value;
    } catch (_) { /* storage may be disabled; the launch tab still works */ }
    return null;
  }
  function accept(value) {
    if (!valid(value)) throw new Error("無法建立本機工作階段，請重新執行 EXE。");
    active = {session:value.session, resource:value.resource};
    try { window.sessionStorage.setItem(key, JSON.stringify(active)); } catch (_) {}
  }
  function headers() {
    const value = read();
    return value ? {"X-VGHKS-Session":value.session} : {};
  }
  function resourceURL(path) {
    const url = new URL(path, location.href);
    if (url.origin !== location.origin || !resourcePath.test(url.pathname)) {
      throw new Error("附件網址不正確。");
    }
    const value = read();
    if (value) url.searchParams.set("access", value.resource);
    return url.href;
  }
  return {accept, headers, resourceURL};
})();
