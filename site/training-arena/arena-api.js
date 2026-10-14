// Training Arena — the one place the console talks to meetrudi-arena-api (contract:
// services/training-arena/API.md). Loaded after config.js and before arena.js / the sign-in script.
// An empty API_BASE means PROTOTYPE mode: `live` is false and nothing here is ever called.
// The session token lives in sessionStorage only, so it dies with the tab.
(function () {
  "use strict";

  var KEY = "arena.session";

  function base() { return String((window.ARENA_CONFIG || {}).API_BASE || "").replace(/\/+$/, ""); }
  function session() {
    try { return JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch (e) { return null; }
  }
  function apiError(status, code, detail) {
    var e = new Error(detail || code);
    e.status = status; e.code = code; e.detail = detail;
    return e;
  }
  // Settles never: the page is navigating away, so callers must not toast or re-render.
  function away(href) {
    location.href = href;
    return new Promise(function () {});
  }

  // call("POST", "/batches", {...}) → Promise of the JSON answer. Rejects with an Error carrying
  // .status, .code and .detail (the API's plain-English sentence, safe to show as text).
  function call(method, path, body) {
    var s = session(), headers = {};
    if (s && s.token) headers["X-Arena-Token"] = s.token;
    var opts = { method: method, headers: headers };
    if (body !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(base() + path, opts).then(function (res) {
      return res.text().then(function (txt) {
        var data = {};
        try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = {}; }
        if (res.ok) return data;
        var code = data.error || "http_" + res.status;
        // 401 means the session ended — except a wrong password on /login or /account/password.
        if (res.status === 401 && path !== "/login" && code !== "invalid_credentials") {
          sessionStorage.removeItem(KEY);
          return away("enter_arena.html?expired=1");
        }
        if (res.status === 403 && code === "must_change_password") return away("enter_arena.html?change=1");
        throw apiError(res.status, code, data.detail || (res.status >= 500
          ? "The arena service had a problem (" + code + "). Try again in a moment."
          : "The request was refused (" + code + ")."));
      });
    }, function () {
      throw apiError(0, "network", "The arena service could not be reached. Check your connection and try again.");
    });
  }

  window.ArenaAPI = { live: Boolean(base()), call: call };
})();
