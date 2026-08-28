/* MEET_RUDI tester console — shared client helpers.
 *
 * Small on purpose. The pages hold markup and their own wiring; everything below is the part
 * that would otherwise be copy-pasted five times: the API call, the string lookup, the session
 * token, and the two DOM helpers.
 *
 * Security posture: this file ships to GitHub Pages and is world-readable. It performs NO
 * authorisation of its own — every check that matters (session validity, idle expiry, call
 * quota, quiet hours, admin role) is made by meetrudi-tester-api. Hiding a button here is a
 * courtesy to the tester, never a control.
 */
(function (global) {
  "use strict";

  var CFG = global.RUDI_CONFIG || {};
  var BASE = String(CFG.API_BASE || "").replace(/\/+$/, "");
  var TOKEN_KEY = "rudi.session";
  var LOCALE_KEY = "rudi.locale";

  // ---------------------------------------------------------------- i18n
  function locale() {
    var stored = global.localStorage && localStorage.getItem(LOCALE_KEY);
    if (stored && global.RUDI_I18N[stored]) return stored;
    var def = CFG.DEFAULT_LOCALE || "nl-BE";
    return global.RUDI_I18N[def] ? def : "en";
  }

  function setLocale(loc) {
    if (global.RUDI_I18N[loc]) {
      localStorage.setItem(LOCALE_KEY, loc);
      document.documentElement.lang = loc;
    }
  }

  /** t("reg.title") or t("ver.lead", {email: "..."}). Falls back to English, then to the key
   *  itself — a missing string shows up as `reg.title`, which is obvious in review rather than
   *  silently blank. */
  function t(key, vars) {
    var table = global.RUDI_I18N[locale()] || global.RUDI_I18N.en;
    var s = table[key];
    if (s === undefined) s = (global.RUDI_I18N.en || {})[key];
    if (s === undefined) return key;
    return s.replace(/\{(\w+)\}/g, function (m, name) {
      return vars && vars[name] !== undefined ? vars[name] : m;
    });
  }

  /** Fill every [data-t] element from the string table. Pages call this once on load. */
  function applyStrings(root) {
    (root || document).querySelectorAll("[data-t]").forEach(function (el) {
      el.textContent = t(el.getAttribute("data-t"));
    });
    (root || document).querySelectorAll("[data-t-ph]").forEach(function (el) {
      el.setAttribute("placeholder", t(el.getAttribute("data-t-ph")));
    });
    document.documentElement.lang = locale();
  }

  // ---------------------------------------------------------------- session
  // sessionStorage, not localStorage: the token dies with the tab. It survives a refresh, which
  // matters with a 10-minute idle window, but never outlives the browsing session.
  //
  // Scoped, because a member of the test team will have both the console and the admin pane open
  // in one browser. Sharing a single key means signing into one silently signs you out of the
  // other — and the symptom is a blank page, not an error. admin.html calls useScope("admin")
  // before its first request.
  var scopedKey = TOKEN_KEY;

  function useScope(name) { scopedKey = TOKEN_KEY + (name ? "." + name : ""); }
  function token() { return sessionStorage.getItem(scopedKey) || ""; }
  function setToken(v) { v ? sessionStorage.setItem(scopedKey, v) : sessionStorage.removeItem(scopedKey); }
  function clearToken() { sessionStorage.removeItem(scopedKey); }

  // ---------------------------------------------------------------- API
  /** api("POST", "/login", {...}) -> {ok, status, data}. Never throws; a network failure comes
   *  back as status 0 with an "network" error code so callers have one shape to handle. */
  function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    var tok = token();
    if (tok) opts.headers["X-Tester-Token"] = tok;
    return fetch(BASE + path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { ok: r.ok, status: r.status, data: data || {} };
      });
    }).catch(function () {
      return { ok: false, status: 0, data: { error: "network" } };
    });
  }

  /** Turn an API error payload into a sentence a tester can read. */
  function errorText(res) {
    var d = (res && res.data) || {};
    if (d.error === "invalid_credentials" && d.attempts_left !== undefined) {
      return t("err.invalid_credentials") + " " + t("err.attempts_left", { n: d.attempts_left });
    }
    var key = "err." + (d.error || "generic");
    var msg = t(key);
    return msg === key ? t("err.generic") : msg;
  }

  /** A 401 anywhere means the session is gone — bounce to login with a reason. */
  function guard(res) {
    if (res.status === 401) {
      clearToken();
      location.href = "index.html?expired=1";
      return true;
    }
    if (res.status === 403 && res.data && res.data.error === "revoked") {
      clearToken();
      location.href = "index.html?revoked=1";
      return true;
    }
    return false;
  }

  // ---------------------------------------------------------------- DOM
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function banner(el, kind, text) {
    if (!el) return;
    if (!text) { el.hidden = true; return; }
    el.className = "banner " + kind;
    el.textContent = text;
    el.hidden = false;
  }

  function param(name) {
    return new URLSearchParams(location.search).get(name) || "";
  }

  /** Build a 0–10 scale into a container. onPick fires with the chosen number. */
  function buildScale(el, onPick) {
    el.innerHTML = "";
    for (var i = 0; i <= 10; i++) {
      (function (n) {
        var b = document.createElement("button");
        b.type = "button";
        b.textContent = String(n);
        b.setAttribute("aria-label", n + " / 10");
        b.addEventListener("click", function () {
          $$("button", el).forEach(function (x) { x.classList.toggle("on", x === b); });
          if (onPick) onPick(n);
        });
        el.appendChild(b);
      })(i);
    }
  }

  function scaleValue(el) {
    var on = $("button.on", el);
    return on ? parseInt(on.textContent, 10) : null;
  }

  function setScale(el, value) {
    if (value === null || value === undefined) return;
    $$("button", el).forEach(function (b) {
      b.classList.toggle("on", parseInt(b.textContent, 10) === value);
    });
  }

  function hhmm(totalSeconds) {
    var s = Math.max(0, totalSeconds | 0);
    return String((s / 60) | 0).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  /** "2026-08-22T06:30:00+02:00" -> "06:30" in the reader's own locale. */
  function clockOf(iso) {
    try {
      return new Date(iso).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
    } catch (e) { return ""; }
  }

  global.Rudi = {
    BASE: BASE, CFG: CFG,
    t: t, locale: locale, setLocale: setLocale, applyStrings: applyStrings,
    token: token, setToken: setToken, clearToken: clearToken, useScope: useScope,
    api: api, errorText: errorText, guard: guard,
    $: $, $$: $$, banner: banner, param: param,
    buildScale: buildScale, scaleValue: scaleValue, setScale: setScale,
    hhmm: hhmm, clockOf: clockOf
  };
})(window);
