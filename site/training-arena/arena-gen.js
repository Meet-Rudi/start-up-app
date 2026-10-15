// Training Arena — the console's client for the LOCAL GENERATOR: services/training-arena/generator/
// generator.py, running on the super-admin's own computer at GENERATOR_URL (config.js; default
// http://127.0.0.1:47815 — or the address set in the connection window, kept in this browser).
// It is not the arena API and never sees the arena's session token: it has its own, from pairing
// with the six-character code the generator prints. That token lives in sessionStorage, so it
// dies with the tab.
(function () {
  "use strict";

  var KEY = "arena.gen.token", BASE_KEY = "arena.gen.base";

  function configured() { return String((window.ARENA_CONFIG || {}).GENERATOR_URL || "http://127.0.0.1:47815").replace(/\/+$/, ""); }
  function base() { var b = ""; try { b = localStorage.getItem(BASE_KEY) || ""; } catch (e) { b = ""; } return (b || configured()).replace(/\/+$/, ""); }
  // Only this computer: http://127.0.0.1:<port> or http://localhost:<port>. Empty resets to config.js.
  function setBase(url) {
    url = String(url || "").trim().replace(/\/+$/, "");
    if (url && !/^http:\/\/(127\.0\.0\.1|localhost):\d{2,5}$/.test(url)) return false;
    try { if (url && url !== configured()) localStorage.setItem(BASE_KEY, url); else localStorage.removeItem(BASE_KEY); } catch (e) { /* storage off */ }
    return true;
  }
  function token() { try { return sessionStorage.getItem(KEY) || ""; } catch (e) { return ""; } }
  function setToken(t) { try { if (t) sessionStorage.setItem(KEY, t); else sessionStorage.removeItem(KEY); } catch (e) { /* private window */ } }
  function err(code, detail, status) { var e = new Error(detail || code); e.code = code; e.detail = detail; e.status = status; return e; }

  // call("POST", "/plan", {...}) → Promise of the JSON answer; rejects with .code and .detail.
  function call(method, path, body) {
    var opts = { method: method, cache: "no-store", headers: { "X-Generator-Token": token() } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(base() + path, opts).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.ok) return data;
        if (res.status === 401 && data.error === "not_paired") setToken("");
        throw err(data.error || "http_" + res.status, data.detail || "The local generator answered " + res.status + ".", res.status);
      });
    }, function () {
      throw err("offline", "The local generator is not running on this computer (or the browser blocked the connection).");
    });
  }

  // A generation job: start it, then poll. onJob(job) runs on every update; the promise settles
  // with the finished job (state done, failed or cancelled). cancel() asks the generator to stop.
  function run(path, body, onJob) {
    var stopped = false, jobId = null, timer = null;
    var promise = call("POST", path, body).then(function (r) {
      jobId = r.job.id;
      if (onJob) onJob(r.job);
      return new Promise(function (resolve, reject) {
        (function poll() {
          if (stopped) return resolve({ id: jobId, state: "cancelled", steps: [] });
          call("GET", "/jobs/" + jobId).then(function (x) {
            if (onJob) onJob(x.job);
            if (x.job.state === "running") timer = setTimeout(poll, 700);
            else resolve(x.job);
          }, reject);
        })();
      });
    });
    return {
      promise: promise,
      cancel: function () {
        stopped = true; clearTimeout(timer);
        return jobId ? call("POST", "/jobs/" + jobId + "/cancel").catch(function () { return null; }) : Promise.resolve(null);
      }
    };
  }

  // The start link: Windows starts the generator without a window (registered once by
  // Install-Generator.cmd) and the link's one-time code pairs this tab with it. If one already runs,
  // the new start hands the code over to it. Another web site could open the same link, but the
  // generator refuses every request that is not from the arena console. Resolves with /health once
  // paired; rejects with code "not_started" after `waitMs`.
  function nonce() {
    var b = new Uint8Array(18);
    (window.crypto || window.msCrypto).getRandomValues(b);
    return btoa(String.fromCharCode.apply(null, b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function start(waitMs) {
    var code = nonce(), t0 = Date.now(), a = document.createElement("a");
    a.href = "meetrudi-generator://start?pair=" + code;
    document.body.appendChild(a); a.click(); a.remove();
    return new Promise(function (resolve, reject) {
      (function poll() {
        call("GET", "/health").then(function (h) {
          if (h.service !== "meetrudi-arena-generator") throw err("foreign", "");
          return call("POST", "/pair", { code: code }).then(function (r) { setToken(r.token); resolve(h); });
        }).catch(function () {
          if (Date.now() - t0 < (waitMs || 30000)) return setTimeout(poll, 1000);
          reject(err("not_started", "The generator did not answer within " + Math.round((waitMs || 30000) / 1000) + " seconds."));
        });
      })();
    });
  }
  // Where the local generator stands for this tab: offline · foreign · unpaired · ready (+ /health).
  function status() {
    return call("GET", "/health").then(function (h) {
      if (h.service !== "meetrudi-arena-generator") return { state: "foreign", health: h };
      if (!h.paired) setToken("");
      return { state: h.paired ? "ready" : "unpaired", health: h };
    }, function () { return { state: "offline", health: null }; });
  }

  window.ArenaGen = {
    base: base,
    start: start,
    status: status,
    setBase: setBase,
    paired: function () { return !!token(); },
    health: function () { return call("GET", "/health"); },
    pair: function (code) { return call("POST", "/pair", { code: code }).then(function (r) { setToken(r.token); return r; }); },
    forget: function () { setToken(""); },
    call: call,
    run: run
  };
})();
