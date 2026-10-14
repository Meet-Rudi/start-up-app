// Training Arena console.
// Two modes, picked by config.js. An empty API_BASE is the PROTOTYPE: every action works against
// demo-data.js and nothing is sent anywhere; each demo handler still names the API call it stands
// in for. A set API_BASE is LIVE: each pane loads what it draws from meetrudi-arena-api
// (arena-api.js; contract in services/training-arena/API.md) and every action calls it.
// The role check below only decides what to DRAW — the API is what enforces it.
(function () {
  "use strict";

  var API = window.ArenaAPI || { live: false };
  var LIVE = !!API.live;
  var D = LIVE ? blank() : window.DEMO;
  var $ = function (id) { return document.getElementById(id); };
  var enc = encodeURIComponent;
  var sess = null;
  try { sess = JSON.parse(sessionStorage.getItem("arena.session") || "null"); } catch (e) { sess = null; }
  if (!sess) { location.replace("enter_arena.html"); return; }
  var isSA = sess.role === "superadmin";

  var PANES = ["dashboard", "submit", "batches", "case", "formats", "account", "engine", "users", "audit"];
  var SA_PANES = ["engine", "users", "audit"];
  var OUTCOMES = ["success", "failed", "violation", "error", "none"];
  var LABEL = { success: "Success", failed: "Failed", violation: "Violation", error: "Error", none: "No verdict", over_budget: "Over budget",
    completed: "Completed", cancelled: "Cancelled", running: "Running", queued: "Queued" };
  var GLYPH = { success: "✓", failed: "!", violation: "✕", error: "▲", none: "–", running: "▶", queued: "" };
  var STATUS_LABEL = { draft: "Draft", validating: "Validating", invalid: "Invalid", ready: "Ready to launch", launching: "Launching",
    queued: "Queued", running: "Running", paused: "Paused", assembling: "Building results", budget_exhausted: "Budget exhausted",
    completed: "Completed", completed_with_errors: "Completed with errors", cancelled: "Cancelled" };
  var ACTIVE = /^(launching|queued|running|paused|assembling)$/;   // launched, not finished
  var PENDING = /^(draft|validating|invalid|ready)$/;               // submitted, not launched yet
  var AWS = "AWS (estimate)";

  // ------------------------------------------------------------------ helpers
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(n) { return Number(n).toLocaleString("en-GB"); }
  function num(v) { v = +v; return isFinite(v) ? v : 0; }
  function chip(kind, label) {
    return '<span class="chip ' + esc(kind) + '">' + (kind === "running" ? '<i class="pulse"></i>' : "") + esc(label || LABEL[kind] || kind) + "</span>";
  }
  function dur(s) {
    if (!s) return "—";
    if (s < 90) return s + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    return (s / 3600).toFixed(1) + " h";
  }
  // Euro: cents for amounts from 10 cents up, four decimals below that, or a fixed number of decimals.
  function eur(v, dp) {
    if (v == null || isNaN(v)) return "—";
    v = +v;
    if (v === 0) return "€0";
    if (dp != null) return "€" + v.toFixed(dp);
    if (v >= 0.1) return "€" + v.toFixed(2);
    var t = v.toFixed(4).replace(/0+$/, "");                         // €0.0500 reads as €0.05
    return "€" + (t.split(".")[1].length < 2 ? v.toFixed(2) : t);
  }
  // Everything a day cost: model spend plus the AWS overhead estimate, which budgets count too.
  function spendOf(d) { return d.roles.rudi + d.roles.tester + d.roles.judge + (d.roles.infra || 0); }
  function rolesLine(r, dp) {
    return "Rudi " + eur(r.rudi, dp) + " · tester " + eur(r.tester, dp) + " · judge " + eur(r.judge, dp) + " · AWS " + eur(r.infra || 0, dp) + " (estimate)";
  }
  var toastTimer = null;
  function toast(msg, sub) {
    var t = $("toast");
    t.innerHTML = esc(msg) + (sub ? "<small>" + esc(sub) + "</small>" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 4200);
  }
  function proto(what) { toast("Prototype — not wired to a service yet", what); }
  // An API refusal: the API's own sentence (err.detail) is shown as text, never as markup.
  function fail(err, what) {
    var msg = (err && err.detail) || "Something went wrong. Try again.";
    if (what) toast(what, msg); else toast(msg);
  }

  function modal(title, bodyHtml, buttons) {
    $("m-title").textContent = title;
    $("m-body").innerHTML = bodyHtml;
    var foot = $("m-foot");
    foot.innerHTML = "";
    (buttons || [{ label: "Close" }]).forEach(function (b) {
      var el = document.createElement("button");
      el.type = "button";
      el.className = "btn " + (b.cls != null ? b.cls : "ghost");
      el.textContent = b.label;
      el.addEventListener("click", function () {
        if (b.on && b.on() === false) return;      // returning false keeps the dialog open
        $("modal").hidden = true;
      });
      foot.appendChild(el);
    });
    $("modal").hidden = false;
    var first = $("m-body").querySelector("input,select");
    if (first) first.focus();
  }
  $("modal").addEventListener("click", function (e) { if (e.target === $("modal")) $("modal").hidden = true; });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") $("modal").hidden = true; });

  var tipEl = $("tip");
  function tipShow(html, ev) {
    tipEl.innerHTML = html;
    tipEl.hidden = false;
    var w = tipEl.offsetWidth, hgt = tipEl.offsetHeight;
    var x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + w > window.innerWidth - 8) x = ev.clientX - w - 14;
    if (y + hgt > window.innerHeight - 8) y = ev.clientY - hgt - 14;
    tipEl.style.left = Math.max(8, x) + "px";
    tipEl.style.top = Math.max(8, y) + "px";
  }
  function tipHide() { tipEl.hidden = true; }

  function audit(action, target, detail) {                      // PROTOTYPE only: the API keeps the real log
    D.audit.unshift([D.now, sess.username, action, target || "", detail || ""]);
  }
  function newCode(n) {
    var abc = "0123456789ABCDEFGHJKMNPQRSTVWXYZ", s = "";
    for (var i = 0; i < n; i++) s += abc[Math.floor(Math.random() * abc.length)];
    return s;
  }
  function slug(s) {
    return String(s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "");
  }
  // Case rows of a batch: generated on demand in the prototype; in live mode whatever was loaded.
  function casesOf(b) {
    if (LIVE) return D.cases[b.id] || [];
    if (!D.cases[b.id]) D.cases[b.id] = D.genCases(b);
    return D.cases[b.id];
  }
  function batchById(id) { return D.batches.filter(function (b) { return b.id === id; })[0]; }
  function isActive(b) { return ACTIVE.test(b.status); }
  function done(b) { return b.c.success + b.c.failed + b.c.violation + b.c.error + b.c.completed + b.c.cancelled + (b.c.over_budget || 0); }
  function noVerdict(b) { return b.c.completed + b.c.cancelled + (b.c.over_budget || 0); }
  function barHtml(b, cls) {
    var seg = [["success", b.c.success], ["failed", b.c.failed], ["violation", b.c.violation], ["error", b.c.error],
      ["none", noVerdict(b)], ["running", b.c.running]], tot = b.total || 1;
    return '<div class="pbar ' + (cls || "") + '" role="img" aria-label="' + done(b) + " of " + num(b.total) + ' cases finished">' +
      seg.filter(function (s) { return s[1] > 0; }).map(function (s) {
        return '<span class="' + s[0] + '" style="width:' + (100 * s[1] / tot) + '%" title="' + LABEL[s[0]] + ": " + s[1] + '"></span>';
      }).join("") + "</div>";
  }
  function spendHtml(b) {
    var pct = b.cap ? Math.min(100, 100 * b.spent / b.cap) : 0;
    return '<div class="spend"><span class="mtr" title="' + Math.round(pct) + '% of the batch budget"><i style="width:' + pct + '%"' + (pct >= 90 ? ' class="hot"' : "") + '></i></span>' +
      '<span class="meta"><b class="num">' + eur(b.spent) + "</b> of " + eur(b.cap) + " budget · ceiling " + eur(b.caseCap, 2) + " a case</span></div>";
  }
  function legendHtml(keys, counts) {
    return keys.map(function (k) {
      return '<span><i class="sw ' + k + '">' + (GLYPH[k] || "") + "</i>" + LABEL[k] + (counts ? " <b class='num'>" + fmt(counts[k] || 0) + "</b>" : "") + "</span>";
    }).join("");
  }
  var QUIET_NOTE = "It waits for quiet hours and starts at 21:30 (Europe/Brussels). “Start now” on its page lifts the wait.";
  // Start now / pause / resume / cancel, for whoever may use them (the API re-checks).
  function controls(b, cancelLabel) {
    var mine = b.by === sess.username || isSA, dis = mine ? "" : " disabled title='Only the submitter or a super-admin'", id = esc(b.id), out = "";
    if (LIVE && b.quiet_only && /^(launching|queued|running)$/.test(b.status)) out += '<button class="btn sm pink" data-act="start-now" data-id="' + id + '"' + dis + ' title="Launched for quiet hours only (21:30–06:30, Europe/Brussels)">Start now</button>';
    if (/^(running|paused)$/.test(b.status)) out += '<button class="btn sm ghost" data-act="' + (b.status === "paused" ? "resume" : "pause") + '" data-id="' + id + '"' + dis + ">" + (b.status === "paused" ? "Resume" : "Pause") + "</button>";
    if (/^(queued|running|paused|draft|invalid|ready)$/.test(b.status)) out += '<button class="btn sm danger" data-act="cancel" data-id="' + id + '"' + dis + ">" + cancelLabel + "</button>";
    return out;
  }

  // ------------------------------------------------------------------ live data
  // LIVE starts empty and each pane's load() fills what it draws, in the shapes the demo data
  // has. Settings start at the API's defaults so nothing divides by zero before /me answers.
  function blank() {
    return { BUCKET: "", now: "", batches: [], users: [], audit: [], storage: [], versions: [], daily: [], failingKeys: [], cases: {}, me: null,
      settings: { month_budget: 150, user_day_budget: 20, case_default: 0.05, batch_default: 10, case_ceiling: 0.5, batch_ceiling: 100,
        infra_turn: 0.00015, infra_case: 0.0002, max_parallel: 2, launch_rate: 6, think_time: 2, max_turns_cap: 40,
        cascades: { rudi: [], tester: [], judge: [] } } };
  }
  function roles(r) { r = r || {}; return { rudi: num(r.rudi), tester: num(r.tester), judge: num(r.judge), infra: num(r.infra) }; }
  function normDay(d) {
    OUTCOMES.forEach(function (k) { d[k] = num(d[k]); });
    d.total = num(d.total); d.roles = roles(d.roles); d.infra = d.roles.infra;
    d.models = d.models || {}; d.cost = d.cost || {}; d.errors = d.errors || {}; d.modes = d.modes || {};
    return d;
  }
  function normBatch(b) {
    var c = b.c || {};
    b.c = {};
    ["success", "failed", "violation", "error", "completed", "cancelled", "over_budget", "running", "queued"].forEach(function (k) { b.c[k] = num(c[k]); });
    ["total", "parts", "spent", "cap", "caseCap"].forEach(function (k) { b[k] = num(b[k]); });
    b.roles = roles(b.roles); b.modes = b.modes || {}; b.locales = b.locales || {}; b.tags = b.tags || [];
    b.files = b.files || []; b.snapshots = b.snapshots || []; b.name = b.name || ""; b.desc = b.desc || "";
    b.submitted = b.submitted || ""; b.started = b.started || ""; b.finished = b.finished || "";
    return b;
  }
  function putBatch(b) {                                       // replace by id, or add at the top
    b = normBatch(b);
    var i = D.batches.map(function (x) { return x.id; }).indexOf(b.id);
    if (i >= 0) D.batches[i] = b; else D.batches.unshift(b);
    return b;
  }
  function normCase(c) {
    if (c.n == null) c.n = c.seq;
    ["turns", "dur", "errs", "cap"].forEach(function (k) { c[k] = num(c[k]); });
    c.cost = c.cost == null ? null : num(c.cost);
    if (c.roles) c.roles = roles(c.roles);
    return c;
  }
  function upsertUser(name, spent) {
    var u = D.users.filter(function (x) { return x.u === name; })[0];
    if (!u) D.users.push(u = { u: name });
    u.spentToday = num(spent);
  }
  function get(path) { return API.call("GET", path); }
  var meLoaded = false;
  function loadMe(force) {
    if (meLoaded && !force) return Promise.resolve();
    return get("/me").then(function (r) {
      meLoaded = true;
      D.me = r; D.BUCKET = r.bucket || D.BUCKET;
      [r.budgets, r.settings].forEach(function (o) { Object.keys(o || {}).forEach(function (k) { D.settings[k] = o[k]; }); });
      upsertUser(r.user.u, r.spent_today);
      if (r.user.role !== sess.role) {                          // changed by a super-admin since sign-in: redraw the menu
        sess.role = r.user.role;
        sessionStorage.setItem("arena.session", JSON.stringify(sess));
        location.reload();
      }
    });
  }
  function loadBatches() { return get("/batches").then(function (r) { D.batches = (r.batches || []).map(normBatch); }); }

  var LOAD = {
    dashboard: function () {
      return get("/dashboard?days=30").then(function (r) {
        D.daily = (r.daily || []).map(normDay);
        D.batches = (r.batches || []).map(normBatch);
        D.failingKeys = r.failingKeys || [];
        D.expensive = r.expensive || [];
        (r.users || []).forEach(function (u) { upsertUser(u.u, u.spentToday); });
        D.settings.month_budget = num((r.budgets || {}).month_budget);
        D.settings.user_day_budget = num((r.budgets || {}).user_day_budget);
        D.now = r.now || D.now;
        $("dash-stamp").textContent = new Date().toTimeString().slice(0, 8);
      });
    },
    submit: function () { return Promise.all([loadMe(), loadBatches()]); },
    batches: function (id) {
      if (!id) return Promise.all([loadMe(), loadBatches()]);
      return Promise.all([loadMe(), get("/batches/" + enc(id)), get("/batches/" + enc(id) + "/cases?limit=2000")]).then(function (r) {
        var b = putBatch(r[1].batch);
        D.cases[b.id] = (r[2].rows || []).map(normCase);
      });
    },
    "case": function (id) {
      if (!id) return Promise.resolve();
      return get("/cases/" + enc(id)).then(function (r) {
        var c = normCase(r["case"]), b = putBatch(c.batch), list = D.cases[b.id] || (D.cases[b.id] = []);
        delete c.batch;
        var i = list.map(function (x) { return x.id; }).indexOf(c.id);
        if (i >= 0) list[i] = c; else list.push(c);
      });
    },
    formats: function () { return loadMe(true); },
    account: function () { return Promise.all([loadMe(true), loadBatches()]); },
    engine: function () {
      return get("/admin/settings").then(function (r) { D.settings = r.settings; W = null; engState = D.settings.state || "running"; });
    },
    users: function () { return Promise.all([loadMe(), get("/admin/users").then(function (r) { D.users = r.users || []; })]); },
    audit: function () {
      return Promise.all([get("/admin/audit?kind=" + enc($("a-kind").value)), get("/admin/storage"), get("/admin/versions")]).then(function (r) {
        D.audit = r[0].audit || []; D.storage = r[1].storage || []; D.BUCKET = r[1].bucket || D.BUCKET; D.versions = r[2].versions || [];
      });
    }
  };
  // Fetch what a pane draws before drawing it. The prototype has everything already.
  function load(name, arg) { return LIVE ? LOAD[name](arg) : Promise.resolve(); }

  var loadTimer = null;
  function loading(pane, on) {
    clearTimeout(loadTimer);
    if (on) loadTimer = setTimeout(function () { $("loadbar").hidden = false; $("p-" + pane).classList.add("loading"); }, 150);
    else {
      $("loadbar").hidden = true;
      Array.prototype.forEach.call(document.querySelectorAll(".pane.loading"), function (p) { p.classList.remove("loading"); });
    }
  }
  // A short-lived signed S3 link from the API; the file arrives as a download.
  function download(path) {
    API.call("GET", path).then(function (r) { location.href = r.url; }, function (e) { fail(e, "Could not download that file"); });
  }

  // ------------------------------------------------------------------ shell
  $("who-name").textContent = sess.username;
  $("who-av").textContent = sess.username.slice(0, 2).toUpperCase();
  $("who-role").textContent = isSA ? "Super-admin" : "Tester";
  $("who-role").classList.toggle("sa", isSA);
  $("nav-sa").hidden = !isSA;
  $("nav-running").hidden = true;
  $("proto-pill").hidden = LIVE;
  $("side-foot").textContent = isSA
    ? "Super-admin sections are not drawn for testers, and the API refuses them to any other role."
    : "Signed in as a tester.";
  $("signout").addEventListener("click", function () {
    var out = function () { sessionStorage.removeItem("arena.session"); location.href = "enter_arena.html"; };
    if (!LIVE) return out();
    API.call("POST", "/logout").then(out, out);                  // ends the session server-side; leave either way
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-proto]");
    if (b) proto(b.getAttribute("data-proto"));
  });

  var routeSeq = 0;
  function route() {
    var parts = location.hash.replace(/^#/, "").split("/");
    var name = PANES.indexOf(parts[0]) >= 0 ? parts[0] : "dashboard", arg = parts.slice(1).join("/");
    if (SA_PANES.indexOf(name) >= 0 && !isSA) {
      name = "dashboard";
      toast("That section is for super-admins.");
    }
    PANES.forEach(function (p) { $("p-" + p).hidden = p !== name; });
    Array.prototype.forEach.call(document.querySelectorAll(".nav"), function (a) {
      a.classList.toggle("on", a.getAttribute("data-pane") === name);
    });
    tipHide();
    var seq = ++routeSeq;
    loading(name, LIVE);
    load(name, arg).then(function () {
      if (seq !== routeSeq) return;                              // the reader has moved on already
      loading(name, false);
      paint(name, arg);
    }, function (e) {
      if (seq !== routeSeq) return;
      loading(name, false);
      fail(e, "Could not load this page");
      if (e && e.status === 404) paint(name, arg);               // the pane draws its own "not found"
    });
    window.scrollTo(0, 0);
  }
  function paint(name, arg) {
    var active = D.batches.filter(isActive).length;
    $("nav-running").textContent = active;
    $("nav-running").hidden = !active;
    RENDER[name](arg);
  }
  window.addEventListener("hashchange", route);

  // ================================================================== DASHBOARD
  var rangeDays = 14;
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

  function slice() { return D.daily.slice(-rangeDays); }
  function monthDays() {
    if (!D.daily.length) return [];
    var first = D.daily[D.daily.length - 1].date.slice(0, 8) + "01";
    return D.daily.filter(function (d) { return d.date >= first; });
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
  }
  function dayLabel(iso, long) {
    var d = new Date(iso + "T00:00:00Z"), m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
    return long ? ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()] + " " + d.getUTCDate() + " " + m : d.getUTCDate() + " " + m;
  }

  function renderKpis() {
    var act = D.batches.filter(function (b) { return /running|paused/.test(b.status); }).length;
    var qd = D.batches.filter(function (b) { return b.status === "queued"; }).length;
    var waiting = D.batches.reduce(function (s, b) { return s + b.c.queued; }, 0);
    var today = D.daily[D.daily.length - 1];
    var w = D.daily.slice(-7), pw = D.daily.slice(-14, -7);
    function rate(a) {
      var t = a.reduce(function (s, d) { return s + d.total - d.none; }, 0);
      return t ? 100 * a.reduce(function (s, d) { return s + d.success; }, 0) / t : null;
    }
    function p95(a) { var v = a.filter(function (d) { return d.p95; }); return v.length ? v.reduce(function (s, d) { return s + d.p95; }, 0) / v.length / 1000 : null; }
    var r = rate(w), pr = rate(pw), l = p95(w), pl = p95(pw);
    var dr = r != null && pr != null ? r - pr : null, dl = l != null && pl != null ? l - pl : null;
    var errs = Object.keys(today.errors).reduce(function (s, k) { return s + today.errors[k]; }, 0);
    var month = monthDays().reduce(function (s, d) { return s + spendOf(d); }, 0);
    var tiles = [
      ["Batches running", act, qd + " queued · " + fmt(waiting) + " cases waiting"],
      ["Cases finished today", fmt(today.total), fmt(today.success) + " successful"],
      ["Success rate · 7 days", r == null ? "—" : r.toFixed(1) + "%", dr == null ? "nothing to compare with yet" :
        '<b class="' + (dr >= 0 ? "up" : "dn") + '">' + (dr >= 0 ? "▲ " : "▼ ") + Math.abs(dr).toFixed(1) + " pts</b> vs previous 7 days"],
      ["Rudi p95 latency · 7 days", l == null ? "—" : l.toFixed(1) + " s", dl == null ? "nothing to compare with yet" :
        '<b class="' + (dl <= 0 ? "up" : "dn") + '">' + (dl <= 0 ? "▼ " : "▲ ") + Math.abs(dl).toFixed(1) + " s</b> vs previous 7 days"],
      ["Engine errors today", fmt(errs), fmt(today.errors.rate_limited || 0) + " rate-limited"],
      ["Spend today", eur(spendOf(today)), eur(month) + " of " + eur(D.settings.month_budget, 0) + " this month"]
    ];
    $("kpis").innerHTML = tiles.map(function (t) {
      return '<div class="kpi"><div class="lbl">' + t[0] + '</div><div class="v">' + t[1] + '</div><div class="dl">' + t[2] + "</div></div>";
    }).join("");
  }

  function renderRunning() {
    var rows = D.batches.filter(isActive);
    $("run-legend").innerHTML = legendHtml(["success", "failed", "violation", "error", "running", "queued"]);
    if (!rows.length) { $("running").innerHTML = '<p class="hint">Nothing is running. <a href="#submit">Submit a batch</a>.</p>'; return; }
    $("running").innerHTML = rows.map(function (b) {
      var id = esc(b.id);
      return '<div class="runrow"><div><b><a href="#batches/' + id + '" style="color:inherit;text-decoration:none">' + esc(b.name || b.id) + "</a></b> " + chip(b.status, STATUS_LABEL[b.status]) +
        '<div class="meta mono">' + id + '</div><div class="meta">by ' + esc(b.by) + " · " + esc(b.started ? "started " + b.started.slice(11) : "submitted " + b.submitted.slice(11)) + "</div></div>" +
        "<div>" + barHtml(b, "lg") + '<div class="meta" style="margin-top:.35rem"><b class="num">' + fmt(done(b)) + "</b> of " + fmt(b.total) + " cases finished · " +
        fmt(b.c.running) + " running · " + fmt(b.c.queued) + " queued · " + (b.status === "paused" ? "paused" : b.status === "assembling" ? "building the results files" : "ETA " + esc(b.eta || "—")) + "</div>" + spendHtml(b) + "</div>" +
        '<div class="row">' + controls(b, "Cancel") + '<a class="btn sm" href="#batches/' + id + '">Open</a></div></div>';
    }).join("");
  }

  function batchAction(act, id) {
    var b = batchById(id);
    if (!b) return;
    if (act === "cancel") {
      var never = PENDING.test(b.status);
      modal("Cancel " + id + "?", '<p class="d">' + (never ? "This batch has not been launched. Cancelling it means it never will be." :
        fmt(b.c.queued + b.c.running) + " cases have not finished. Cases already finished keep their results. This cannot be undone.") + "</p>",
        [{ label: never ? "Keep it" : "Keep running" }, { label: "Cancel batch", cls: "danger solid", on: function () {
          if (LIVE) return liveBatchAction("cancel", id);
          b.c.cancelled += b.c.queued + b.c.running; b.c.queued = 0; b.c.running = 0;                                                          // API: POST /batches/{id}/cancel
          b.status = "cancelled"; b.finished = D.now; b.parts = done(b) ? 1 : 0; delete D.cases[id];
          audit("batch.cancel", id); toast("Batch cancelled"); route();
        } }]);
      return;
    }
    if (act === "start-now") {
      return API.call("POST", "/batches/" + enc(id) + "/quiet", { quiet_only: false }).then(function () {
        toast("Starting " + id + " now", "No longer waiting for quiet hours — cases start as slots free up."); route();
      }, function (e) { fail(e, "The batch was not started"); });
    }
    if (LIVE) return liveBatchAction(act, id);
    if (act === "pause") { b.status = "paused"; audit("batch.pause", id); toast("Batch paused", "Running cases finish; no new ones start."); }   // API: POST /batches/{id}/pause
    if (act === "resume") { b.status = "running"; audit("batch.resume", id); toast("Batch resumed"); }                                          // API: POST /batches/{id}/resume
    route();
  }
  // The runner applies pause / resume / cancel within moments, so the reload may still show the old state.
  function liveBatchAction(act, id) {
    API.call("POST", "/batches/" + enc(id) + "/" + act).then(function () {
      toast({ pause: "Pausing the batch", resume: "Resuming the batch", cancel: "Cancelling the batch" }[act],
        { pause: "Running cases finish; no new ones start.", resume: "Cases start again as slots free up.", cancel: "Cases already finished keep their results." }[act]);
      route();
    }, function (e) { fail(e, "The batch did not " + act); });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-act]");
    if (b && !b.disabled) batchAction(b.getAttribute("data-act"), b.getAttribute("data-id"));
  });

  // Live: reload. Prototype: a stand-in for "a case finished" — each refresh moves the running batch a few cases forward.
  $("dash-refresh").addEventListener("click", function () {
    if (LIVE) return route();
    var r = Math.random;
    function add(t, c) { t.rudi += c * 0.45; t.tester += c * 0.35; t.judge += c * 0.11; t.infra += c * 0.09; }   // AWS overhead included
    D.batches.forEach(function (b) {
      if (b.status !== "running") return;
      var n = Math.min(b.c.queued + b.c.running, 2 + Math.floor(r() * 3));
      for (var i = 0; i < n; i++) {
        var x = r(), k = x < .86 ? "success" : x < .93 ? "failed" : x < .95 ? "violation" : "error";
        b.c[k]++;
        if (b.c.queued > 0) b.c.queued--; else b.c.running--;
        var today = D.daily[D.daily.length - 1], c = 0.006 + r() * 0.008;
        today[k]++; today.total++;
        add(today.roles, c); today.infra = today.roles.infra;
        b.spent += c; add(b.roles, c);
      }
      if (b.c.queued + b.c.running === 0) { b.status = b.c.error ? "completed_with_errors" : "completed"; b.finished = D.now; b.parts = 1; }
      delete D.cases[b.id];
    });
    var now = new Date();
    $("dash-stamp").textContent = now.toTimeString().slice(0, 8);
    RENDER.dashboard();
  });

  function svgEl(w, hgt) { return '<svg viewBox="0 0 ' + w + " " + hgt + '" role="img">'; }
  function topRound(x, y, w, hgt, r) {
    r = Math.min(r, hgt, w / 2);
    return "M" + x + "," + (y + hgt) + "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y + "H" + (x + w - r) + "Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) + "V" + (y + hgt) + "Z";
  }
  var COL = { success: "var(--s-success)", failed: "var(--s-failed)", violation: "var(--s-violation)", error: "var(--s-error)", none: "var(--s-none)" };

  function chartOutcomes() {
    var days = slice(), n = days.length, W = 640, H = 236, L = 40, R = 8, T = 10, B = 26;
    var max = niceMax(Math.max.apply(null, days.map(function (d) { return d.total; })));
    var band = (W - L - R) / n, bw = Math.min(24, band * 0.68), ph = H - T - B, step = n <= 7 ? 1 : n <= 14 ? 2 : 5;
    var s = svgEl(W, H);
    for (var i = 0; i <= 4; i++) {
      var y = T + ph - ph * i / 4;
      s += '<line class="' + (i ? "gl" : "base") + '" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/><text x="' + (L - 6) + '" y="' + (y + 4) + '" text-anchor="end">' + fmt(max * i / 4) + "</text>";
    }
    days.forEach(function (d, i) {
      var x = L + band * i + (band - bw) / 2, y = T + ph, top = OUTCOMES.filter(function (k) { return d[k] > 0; }).pop();
      s += '<rect class="hov" data-i="' + i + '" x="' + (L + band * i) + '" y="' + T + '" width="' + band + '" height="' + ph + '" fill="transparent"/>';
      OUTCOMES.forEach(function (k) {
        if (!d[k]) return;
        var hh = ph * d[k] / max, vis = Math.max(1, hh - 2);                 // 2px surface gap between segments
        y -= hh;
        s += k === top ? '<path d="' + topRound(x, y + 2, bw, vis, 4) + '" fill="' + COL[k] + '" pointer-events="none"/>'
          : '<rect x="' + x + '" y="' + (y + 2) + '" width="' + bw + '" height="' + vis + '" fill="' + COL[k] + '" pointer-events="none"/>';
      });
      if ((n - 1 - i) % step === 0) s += '<text x="' + (L + band * i + band / 2) + '" y="' + (H - 8) + '" text-anchor="middle">' + dayLabel(d.date) + "</text>";
    });
    var el = $("ch-outcomes");
    el.innerHTML = s + "</svg>";
    Array.prototype.forEach.call(el.querySelectorAll(".hov"), function (r) {
      r.addEventListener("mousemove", function (ev) {
        var d = days[+r.getAttribute("data-i")];
        r.setAttribute("fill", "rgba(37,62,127,.05)");
        tipShow("<b>" + dayLabel(d.date, true) + "</b>" + OUTCOMES.map(function (k) {
          return '<div class="r"><span><i class="sw ' + k + '">' + GLYPH[k] + "</i>" + LABEL[k] + '</span><b class="num" style="display:inline;margin:0">' + fmt(d[k]) + "</b></div>";
        }).join("") + '<div class="r" style="border-top:1px solid var(--hair);margin-top:.25rem;padding-top:.25rem"><span>Total</span><b class="num" style="display:inline;margin:0">' + fmt(d.total) + "</b></div>", ev);
      });
      r.addEventListener("mouseleave", function () { r.setAttribute("fill", "transparent"); tipHide(); });
    });
    $("oc-legend").innerHTML = legendHtml(OUTCOMES);
    $("tb-outcomes").innerHTML = '<table class="t"><thead><tr><th>Day</th>' + OUTCOMES.map(function (k) { return '<th class="n">' + LABEL[k] + "</th>"; }).join("") + '<th class="n">Total</th></tr></thead><tbody>' +
      days.slice().reverse().map(function (d) {
        return "<tr><td>" + dayLabel(d.date, true) + "</td>" + OUTCOMES.map(function (k) { return '<td class="n">' + fmt(d[k]) + "</td>"; }).join("") + '<td class="n"><b>' + fmt(d.total) + "</b></td></tr>";
      }).join("") + "</tbody></table>";
  }

  function chartLatency() {
    var days = slice(), n = days.length, W = 480, H = 236, L = 34, R = 66, T = 10, B = 26, ph = H - T - B, pw = W - L - R;
    var max = niceMax(Math.max.apply(null, days.map(function (d) { return (d.p95 || 0) / 1000; })));
    var step = n <= 7 ? 1 : n <= 14 ? 2 : 5;
    function X(i) { return L + (n === 1 ? pw / 2 : pw * i / (n - 1)); }
    function Y(v) { return T + ph - ph * v / max; }
    var s = svgEl(W, H);
    for (var i = 0; i <= 4; i++) {
      var y = T + ph - ph * i / 4;
      s += '<line class="' + (i ? "gl" : "base") + '" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/><text x="' + (L - 6) + '" y="' + (y + 4) + '" text-anchor="end">' + (max * i / 4) + "</text>";
    }
    days.forEach(function (d, i) { if ((n - 1 - i) % step === 0) s += '<text x="' + X(i) + '" y="' + (H - 8) + '" text-anchor="middle">' + dayLabel(d.date) + "</text>"; });
    [["p95", "var(--c2)"], ["p50", "var(--c1)"]].forEach(function (ser) {
      var path = "", pen = false, last = null;
      days.forEach(function (d, i) {
        if (d[ser[0]] == null) { pen = false; return; }            // a day with no cases breaks the line
        path += (pen ? "L" : "M") + X(i).toFixed(1) + "," + Y(d[ser[0]] / 1000).toFixed(1);
        pen = true; last = i;
      });
      s += '<path d="' + path + '" fill="none" stroke="' + ser[1] + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
      if (last != null) {
        var v = days[last][ser[0]] / 1000;
        s += '<circle cx="' + X(last) + '" cy="' + Y(v) + '" r="4" fill="' + ser[1] + '" stroke="#fff" stroke-width="2"/>' +
          '<text class="lbl-end" x="' + (X(last) + 9) + '" y="' + (Y(v) + 4) + '">' + ser[0] + " " + v.toFixed(1) + " s</text>";
      }
    });
    s += '<line id="lat-x" class="base" y1="' + T + '" y2="' + (T + ph) + '" x1="0" x2="0" visibility="hidden"/>' +
      '<circle id="lat-d1" r="4" fill="var(--c2)" stroke="#fff" stroke-width="2" visibility="hidden"/><circle id="lat-d2" r="4" fill="var(--c1)" stroke="#fff" stroke-width="2" visibility="hidden"/>' +
      '<rect id="lat-hit" x="' + L + '" y="' + T + '" width="' + pw + '" height="' + ph + '" fill="transparent"/>';
    var el = $("ch-latency");
    el.innerHTML = s + "</svg>";
    var hit = $("lat-hit"), svg = el.querySelector("svg");
    hit.addEventListener("mousemove", function (ev) {
      var box = svg.getBoundingClientRect(), x = (ev.clientX - box.left) * W / box.width;
      var i = Math.max(0, Math.min(n - 1, Math.round((x - L) / pw * (n - 1)))), d = days[i];
      $("lat-x").setAttribute("x1", X(i)); $("lat-x").setAttribute("x2", X(i)); $("lat-x").setAttribute("visibility", "visible");
      [["lat-d1", "p95"], ["lat-d2", "p50"]].forEach(function (p) {
        var c = $(p[0]);
        if (d[p[1]] == null) { c.setAttribute("visibility", "hidden"); return; }
        c.setAttribute("cx", X(i)); c.setAttribute("cy", Y(d[p[1]] / 1000)); c.setAttribute("visibility", "visible");
      });
      tipShow("<b>" + dayLabel(d.date, true) + "</b>" + (d.p95 == null ? "No cases ran" :
        '<div class="r"><span><i class="sw line c2"></i>p95</span><b class="num" style="display:inline;margin:0">' + (d.p95 / 1000).toFixed(1) + " s</b></div>" +
        '<div class="r"><span><i class="sw line c1"></i>p50</span><b class="num" style="display:inline;margin:0">' + (d.p50 / 1000).toFixed(1) + " s</b></div>"), ev);
    });
    hit.addEventListener("mouseleave", function () {
      ["lat-x", "lat-d1", "lat-d2"].forEach(function (id) { $(id).setAttribute("visibility", "hidden"); });
      tipHide();
    });
    $("lat-legend").innerHTML = '<span><i class="sw line c1"></i>p50 — the typical reply</span><span><i class="sw line c2"></i>p95 — the slow tail</span>';
  }

  function hbars(el, rows) {
    var max = Math.max.apply(null, rows.map(function (r) { return r[1]; }).concat([1]));
    el.innerHTML = rows.map(function (r) {
      return '<div class="hbar" title="' + esc(r[0]) + ": " + esc(r[2]) + '"><span class="k">' + esc(r[0]) + '</span><span class="trk"><span style="width:' + (100 * r[1] / max) + '%"></span></span><span class="v">' + esc(r[2]) + "</span></div>";
    }).join("") || '<p class="hint">Nothing in this period.</p>';
  }
  function chartErrorsModels() {
    var days = slice(), e = {}, m = {};
    days.forEach(function (d) {
      Object.keys(d.errors).forEach(function (k) { e[k] = (e[k] || 0) + num(d.errors[k]); });
      Object.keys(d.models).forEach(function (k) { m[k] = (m[k] || 0) + num(d.models[k]); });
    });
    var names = { rate_limited: "Rate-limited", timeout: "Timeout", engine_failed: "Engine failed", engine_error: "Engine error",
      tester_failed: "Tester LLM failed", tester_error: "Tester LLM error", judge_error: "Judge LLM error", runner_lost: "Runner lost" };
    hbars($("ch-errors"), Object.keys(e).map(function (k) { return [names[k] || k, e[k], fmt(e[k])]; }).sort(function (a, b) { return b[1] - a[1]; }));
    var tot = Object.keys(m).reduce(function (s, k) { return s + m[k]; }, 0) || 1;
    hbars($("ch-models"), Object.keys(m).map(function (k) { return [k, m[k], (100 * m[k] / tot).toFixed(1) + "%"]; }).sort(function (a, b) { return b[1] - a[1]; }));
  }

  function renderTables() {
    $("tb-failing").innerHTML = "<thead><tr><th>Case key</th><th class='n'>Runs</th><th class='n'>Not OK</th><th>Last</th></tr></thead><tbody>" +
      (D.failingKeys.map(function (k) {
        return '<tr><td class="mono">' + esc(k.key) + '</td><td class="n">' + fmt(num(k.runs)) + '</td><td class="n" title="' + esc(k.kinds) + '">' + Math.round(100 * num(k.bad) / (num(k.runs) || 1)) + "%</td><td>" + chip(k.last) + "</td></tr>";
      }).join("") || '<tr><td colspan="4" class="hint">No case key keeps failing.</td></tr>') + "</tbody>";
    var fin = D.batches.filter(function (b) { return !isActive(b) && !PENDING.test(b.status); }).slice(0, 5);
    $("tb-recent").innerHTML = "<thead><tr><th>Batch</th><th>By</th><th>Finished</th><th class='n'>Cases</th><th style='width:14rem'>Outcomes</th><th class='n'>Success</th><th class='n'>Cost</th><th>Status</th></tr></thead><tbody>" +
      (fin.map(function (b) {
        var judged = b.total - noVerdict(b);
        return '<tr class="click" data-go="#batches/' + esc(b.id) + '"><td><b>' + esc(b.name || "(unnamed)") + '</b><div class="hint mono">' + esc(b.id) + "</div></td><td>" + esc(b.by) + "</td><td>" + esc(b.finished) +
          '</td><td class="n">' + fmt(b.total) + "</td><td>" + barHtml(b) + '</td><td class="n">' + (judged ? Math.round(100 * b.c.success / judged) + "%" : "—") + '</td><td class="n">' + eur(b.spent) + "</td><td>" + chip(b.status, STATUS_LABEL[b.status]) + "</td></tr>";
      }).join("") || '<tr><td colspan="8" class="hint">No batch has finished yet.</td></tr>') + "</tbody>";
  }
  document.addEventListener("click", function (e) {
    var tr = e.target.closest("[data-go]");
    if (tr && !e.target.closest("a,button")) location.hash = tr.getAttribute("data-go");
  });
  Array.prototype.forEach.call($("range").children, function (b) {
    b.addEventListener("click", function () {
      rangeDays = +b.getAttribute("data-d");
      Array.prototype.forEach.call($("range").children, function (x) { x.classList.toggle("on", x === b); });
      RENDER.dashboard();
    });
  });
  Array.prototype.forEach.call($("oc-view").children, function (b) {
    b.addEventListener("click", function () {
      var t = b.getAttribute("data-v") === "table";
      Array.prototype.forEach.call($("oc-view").children, function (x) { x.classList.toggle("on", x === b); });
      $("ch-outcomes").hidden = t; $("tb-outcomes").hidden = !t;
    });
  });

  // ------------------------------------------------------------------ costs
  // Stacked bottom to top in this order. The four colours are the validated categorical palette.
  var ROLES = [["rudi", "Rudi", "var(--c1)", "c1"], ["tester", "Tester LLM", "var(--c2)", "c2"], ["judge", "Judge LLM", "var(--c3)", "c3"],
    ["infra", AWS, "var(--c4)", "c4"]];

  function renderBudget() {
    var S = D.settings, today = D.daily[D.daily.length - 1], mdays = monthDays();
    var month = mdays.reduce(function (t, d) { return t + spendOf(d); }, 0), infra = mdays.reduce(function (t, d) { return t + d.roles.infra; }, 0);
    var pct = S.month_budget ? 100 * month / S.month_budget : 0;
    var users = D.users.filter(function (u) { return isSA ? (u.spentToday > 0 || u.u === sess.username) : u.u === sess.username; });
    $("bud-sub").textContent = MONTHS[+today.date.slice(5, 7) - 1] + " so far · resets on the 1st";
    $("bud-body").innerHTML = '<div class="bigfig">' + eur(month) + "<span> of " + eur(S.month_budget, 0) + "</span></div>" +
      '<span class="mtr lg" role="img" aria-label="' + pct.toFixed(1) + '% of the monthly budget"><i style="width:' + Math.min(100, pct) + '%"' + (pct >= 90 ? ' class="hot"' : "") + "></i></span>" +
      '<p class="hint" style="margin:.4rem 0 1rem">' + pct.toFixed(1) + "% used · today " + eur(spendOf(today)) + " · AWS overhead included: ≈ " + eur(infra) + " this month (estimate)</p>" +
      '<div class="lbl" style="margin-bottom:.35rem">' + (isSA ? "Spent today, per user" : "Your spend today") + "</div>" +
      users.map(function (u) {
        return '<div class="urow"><span>' + esc(u.u) + '</span><span class="mtr"><i style="width:' + (S.user_day_budget ? Math.min(100, 100 * num(u.spentToday) / S.user_day_budget) : 0) + '%"></i></span><span class="num">' +
          eur(num(u.spentToday), 2) + " / " + eur(S.user_day_budget, 0) + "</span></div>";
      }).join("");
  }

  function chartSpend() {
    var days = slice(), n = days.length, W = 640, H = 236, L = 46, R = 8, T = 10, B = 26;
    var max = niceMax(Math.max.apply(null, days.map(spendOf)));
    var band = (W - L - R) / n, bw = Math.min(24, band * 0.68), ph = H - T - B, step = n <= 7 ? 1 : n <= 14 ? 2 : 5;
    var s = svgEl(W, H);
    for (var i = 0; i <= 4; i++) {
      var y = T + ph - ph * i / 4;
      s += '<line class="' + (i ? "gl" : "base") + '" x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '"/><text x="' + (L - 6) + '" y="' + (y + 4) + '" text-anchor="end">€' + (max * i / 4).toFixed(2) + "</text>";
    }
    days.forEach(function (d, i) {
      var x = L + band * i + (band - bw) / 2, y = T + ph, keys = ROLES.filter(function (r) { return d.roles[r[0]] > 0; }), top = keys.length ? keys[keys.length - 1][0] : null;
      s += '<rect class="hov" data-i="' + i + '" x="' + (L + band * i) + '" y="' + T + '" width="' + band + '" height="' + ph + '" fill="transparent"/>';
      ROLES.forEach(function (r) {
        var v = d.roles[r[0]];
        if (!v) return;
        var hh = ph * v / max, vis = Math.max(1, hh - 2);
        y -= hh;
        s += r[0] === top ? '<path d="' + topRound(x, y + 2, bw, vis, 4) + '" fill="' + r[2] + '" pointer-events="none"/>'
          : '<rect x="' + x + '" y="' + (y + 2) + '" width="' + bw + '" height="' + vis + '" fill="' + r[2] + '" pointer-events="none"/>';
      });
      if ((n - 1 - i) % step === 0) s += '<text x="' + (L + band * i + band / 2) + '" y="' + (H - 8) + '" text-anchor="middle">' + dayLabel(d.date) + "</text>";
    });
    var el = $("ch-spend");
    el.innerHTML = s + "</svg>";
    Array.prototype.forEach.call(el.querySelectorAll(".hov"), function (rc) {
      rc.addEventListener("mousemove", function (ev) {
        var d = days[+rc.getAttribute("data-i")];
        rc.setAttribute("fill", "rgba(37,62,127,.05)");
        tipShow("<b>" + dayLabel(d.date, true) + "</b>" + ROLES.map(function (r) {
          return '<div class="r"><span><i class="sw ' + r[3] + '"></i>' + r[1] + '</span><b class="num" style="display:inline;margin:0">' + eur(d.roles[r[0]]) + "</b></div>";
        }).join("") + '<div class="r" style="border-top:1px solid var(--hair);margin-top:.25rem;padding-top:.25rem"><span>Total · ' + fmt(d.total) + ' cases</span><b class="num" style="display:inline;margin:0">' + eur(spendOf(d)) + "</b></div>", ev);
      });
      rc.addEventListener("mouseleave", function () { rc.setAttribute("fill", "transparent"); tipHide(); });
    });
    $("sp-legend").innerHTML = ROLES.map(function (r) { return '<span><i class="sw ' + r[3] + '"></i>' + r[1] + "</span>"; }).join("");
    $("tb-spend").innerHTML = '<table class="t"><thead><tr><th>Day</th>' + ROLES.map(function (r) { return '<th class="n">' + r[1] + "</th>"; }).join("") + '<th class="n">Total</th><th class="n">Per case</th></tr></thead><tbody>' +
      days.slice().reverse().map(function (d) {
        return "<tr><td>" + dayLabel(d.date, true) + "</td>" + ROLES.map(function (r) { return '<td class="n">' + eur(d.roles[r[0]]) + "</td>"; }).join("") +
          '<td class="n"><b>' + eur(spendOf(d)) + '</b></td><td class="n">' + (d.total ? eur(spendOf(d) / d.total, 4) : "—") + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function renderCostTables() {
    var days = slice(), agg = {}, models = {}, infra = 0, over = 0;
    ["scripted", "intent", "hybrid"].forEach(function (m) { agg[m] = { n: 0, eur: 0, max: 0, over: 0 }; });
    days.forEach(function (d) {
      Object.keys(d.modes).forEach(function (m) {
        var a = agg[m], x = d.modes[m];
        if (!a) return;
        a.n += num(x.n); a.eur += num(x.eur); a.max = Math.max(a.max, num(x.max)); a.over += num(x.over); over += num(x.over);
      });
      Object.keys(d.cost).forEach(function (m) { if (m !== AWS) models[m] = (models[m] || 0) + num(d.cost[m]); });   // AWS is added last, below
      infra += d.roles.infra;
    });
    $("tb-percase").innerHTML = "<thead><tr><th>Mode</th><th class='n'>Cases</th><th class='n'>Average</th><th class='n'>Highest</th><th class='n'>Over budget</th></tr></thead><tbody>" +
      ["scripted", "intent", "hybrid"].map(function (m) {
        var a = agg[m];
        return '<tr><td><span class="tag">' + m + '</span></td><td class="n">' + fmt(a.n) + '</td><td class="n">' + (a.n ? eur(a.eur / a.n, 4) : "—") + '</td><td class="n">' + (a.n ? eur(a.max, 4) : "—") + '</td><td class="n">' + (a.over || "—") + "</td></tr>";
      }).join("") + "</tbody>";
    $("percase-note").textContent = over ? over + " case" + (over > 1 ? "s" : "") + " stopped by a budget in this period — see the most expensive cases." : "No case was stopped by a budget in this period.";
    var rows = Object.keys(models).map(function (m) { return [m, models[m], eur(models[m])]; }).sort(function (a, b) { return b[1] - a[1]; });
    rows.push([AWS, infra, eur(infra)]);
    hbars($("ch-costmodels"), rows);
    // Most expensive cases: the real case rows of batches that ran in this period, so each one opens.
    // Live mode knows the rows of the batches opened in this session only.
    var from = days[0].date, top = [];
    if (LIVE && D.expensive) {
      // The API picks them from every batch that ran in the period (see /dashboard).
      D.expensive.forEach(function (c) { top.push([null, c]); });
    } else {
      D.batches.forEach(function (b) {
        if ((b.started || b.submitted).slice(0, 10) < from || !done(b)) return;
        casesOf(b).forEach(function (c) { if (c.cost) top.push([b, c]); });
      });
    }
    top.sort(function (a, b) { return b[1].cost - a[1].cost; });
    $("tb-expensive").innerHTML = "<thead><tr><th>Case</th><th class='n'>Turns</th><th class='n'>Cost</th><th>Ended</th></tr></thead><tbody>" +
      (top.slice(0, 6).map(function (x) {
        var c = x[1];
        return '<tr class="click" data-go="#case/' + esc(c.id) + '"><td><span class="mono">' + esc(c.key) + '</span><div class="hint mono" style="white-space:nowrap">' + esc(c.id) + '</div></td><td class="n">' + fmt(c.turns) +
          '</td><td class="n">' + eur(c.cost, 4) + '<div class="hint">of ' + eur(c.cap, 2) + "</div></td><td>" + chip(c.outcome) + "</td></tr>";
      }).join("") || '<tr><td colspan="4" class="hint">' + (LIVE ? "Open a batch from this period to list its most expensive cases here." : "No finished cases in this period.") + "</td></tr>") + "</tbody>";
  }
  Array.prototype.forEach.call($("sp-view").children, function (b) {
    b.addEventListener("click", function () {
      var t = b.getAttribute("data-v") === "table";
      Array.prototype.forEach.call($("sp-view").children, function (x) { x.classList.toggle("on", x === b); });
      $("ch-spend").hidden = t; $("tb-spend").hidden = !t;
    });
  });

  // ================================================================== NEW BATCH
  // sub.gen counts file changes, so a slow upload or check for a file the user has since replaced is ignored.
  var sub = { file: null, report: null, batchId: null, busy: "", gen: 0 };
  var TODAY = LIVE ? brusselsDay() : "20261013";
  function brusselsDay() {
    try {
      return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Brussels", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date()).replace(/-/g, "");
    } catch (e) { return new Date().toISOString().slice(0, 10).replace(/-/g, ""); }
  }

  // extra: {blob} — the File itself, which live mode uploads; {s3} — an object already in the inbox.
  function setFile(name, size, text, origin, extra) {
    sub.file = { name: name, size: size, text: text, origin: origin, blob: (extra || {}).blob || null, s3: (extra || {}).s3 || "" };
    sub.report = null; sub.batchId = null; sub.busy = ""; sub.gen++;
    var kb = size == null ? "" : size < 1024 ? size + " B" : size < 1048576 ? (size / 1024).toFixed(1) + " KB" : (size / 1048576).toFixed(1) + " MB";
    $("filepill").hidden = false;
    $("filepill").innerHTML = "<div><b>" + esc(name) + '</b><div class="hint">' + (kb ? kb + " · " : "") + esc(origin) + '</div></div><span class="sp"></span><button class="btn xs ghost" type="button" id="file-clear">Remove</button>';
    $("file-clear").addEventListener("click", function () {
      sub = { file: null, report: null, batchId: null, busy: "", gen: sub.gen + 1 };
      $("filepill").hidden = true; $("file").value = ""; renderSubmit();
    });
    if (!$("bname").value) {
      var m = /^batch:\s*\n(?:\s+.*\n)*?\s+name:\s*(.+)$/m.exec(text || "");
      if (m) $("bname").value = m[1].replace(/\s+#.*$/, "").replace(/^["']|["']$/g, "").trim();
    }
    renderSubmit();
  }
  function readFile(f) {
    if (!/\.(ya?ml|jsonl?)$/i.test(f.name)) { toast("That file type is not accepted", "Use .yaml, .yml, .json or .jsonl"); return; }
    var rd = new FileReader();
    rd.onload = function () { setFile(f.name, f.size, String(rd.result), "uploaded from this computer", { blob: f }); };   // live: uploaded on Validate
    rd.readAsText(f.slice(0, 4 * 1048576));
  }
  $("file").addEventListener("change", function () { if (this.files[0]) readFile(this.files[0]); });
  ["dragover", "dragenter"].forEach(function (ev) { $("drop").addEventListener(ev, function (e) { e.preventDefault(); $("drop").classList.add("over"); }); });
  ["dragleave", "drop"].forEach(function (ev) { $("drop").addEventListener(ev, function (e) { e.preventDefault(); $("drop").classList.remove("over"); }); });
  $("drop").addEventListener("drop", function (e) { if (e.dataTransfer.files[0]) readFile(e.dataTransfer.files[0]); });
  $("use-sample").addEventListener("click", function () {
    var name = "TA-20261013-K7QX__goal-setting-smoke__input.yaml";
    fetch("samples/" + name).then(function (r) { if (!r.ok) throw 0; return r.text(); })
      .then(function (t) { setFile("goal-setting-smoke.yaml", t.length, t, "sample file", { blob: new Blob([t]) }); })
      .catch(function () { setFile("goal-setting-smoke.yaml", 8838, null, "sample file"); });   // file:// cannot fetch; use the canned report
  });
  Array.prototype.forEach.call($("src-tabs").children, function (b) {
    b.addEventListener("click", function () {
      var s3 = b.getAttribute("data-s") === "s3";
      Array.prototype.forEach.call($("src-tabs").children, function (x) { x.classList.toggle("on", x === b); });
      $("src-upload").hidden = s3; $("src-s3").hidden = !s3;
    });
  });
  $("s3-check").addEventListener("click", function () {
    var p = $("s3path").value.trim(), ok = new RegExp("^s3://" + D.BUCKET + "/inbox/.+\\.(ya?ml|jsonl?)$", "i").test(p);
    if (!ok) { toast("Not a readable location", "Give the full path of a .yaml, .json or .jsonl object under s3://" + D.BUCKET + "/inbox/"); return; }
    if (LIVE) return setFile(p.split("/").pop(), null, null, p, { s3: p });                    // the API reads it on Validate
    setFile(p.split("/").pop(), 1843200, null, p);                                            // API: POST /batches {s3_uri}
  });

  function analyze(text) {
    var issues = [], cases = [], lineOf = function (idx) { return text.slice(0, idx).split("\n").length; };
    if (!/^format:\s*rudi-arena\/input@1\s*$/m.test(text)) issues.push(["e", 1, "Header is missing “format: rudi-arena/input@1”."]);
    var re = /^---\s*$/gm, starts = [0], m;
    while ((m = re.exec(text))) starts.push(m.index + m[0].length);
    starts.forEach(function (st, i) {
      if (i === 0) return;
      var body = text.slice(st, starts[i + 1] ? starts[i + 1] - 4 : text.length), ln = lineOf(st) + 1;
      if (!/^case:\s*$/m.test(body)) { if (body.replace(/^\s*#.*$/gm, "").trim()) issues.push(["e", ln, "Document " + i + " is not a case (expected a top-level “case:” key)."]); return; }
      var g = function (rx) { var x = rx.exec(body); return x ? x[1].replace(/\s+#.*$/, "").trim() : ""; };
      var c = { i: cases.length + 1, ln: ln, key: g(/^  key:\s*(.+)$/m), mode: g(/^  mode:\s*(.+)$/m), locale: g(/^  locale:\s*(.+)$/m), channel: g(/^  channel:\s*(.+)$/m) };
      var sc = /^  script:\s*\n((?:    .*\n?|\s*\n)*)/m.exec(body);
      c.script = sc ? (sc[1].match(/^    - /gm) || []).length : 0;
      c.maxTurns = +g(/^    max_turns:\s*(\d+)/m) || 12;
      c.intent = /^  intent:\s*$/m.test(body);
      c.stop = /success_when:|abort_when:|on_limit:\s*pass/.test(body);
      c.ownCap = (function (x) { return x ? parseFloat(x[1]) : null; })(/max_cost_eur:\s*([\d.]+)/.exec(body));
      var id = c.key || "case-" + String(c.i).padStart(4, "0");
      if (!c.mode) issues.push(["e", ln, id + ": “mode” is required (scripted, intent or hybrid)."]);
      else if (!/^(scripted|intent|hybrid)$/.test(c.mode)) issues.push(["e", ln, id + ": unknown mode “" + c.mode + "”."]);
      if (/^(scripted|hybrid)$/.test(c.mode) && !c.script) issues.push(["e", ln, id + ": a " + c.mode + " case needs a “script”."]);
      if (/^(intent|hybrid)$/.test(c.mode) && !c.intent) issues.push(["e", ln, id + ": a " + c.mode + " case needs “intent.objective”."]);
      if (/^(intent|hybrid)$/.test(c.mode) && c.intent && !c.stop) issues.push(["e", ln, id + ": no success_when, abort_when or on_limit: pass — the case could only ever fail."]);
      if (c.channel && c.channel !== "chat") issues.push(["e", ln, id + ": channel “" + c.channel + "” is reserved for a later version; v1 runs chat only."]);
      if (c.maxTurns > D.settings.max_turns_cap) issues.push(["e", ln, id + ": max_turns " + c.maxTurns + " is over the arena cap of " + D.settings.max_turns_cap + "."]);
      if (!c.key) issues.push(["w", ln, "Case " + c.i + " has no “key”; it will be called " + id + "."]);
      cases.push(c);
    });
    // Budgets: the file's own ceilings, else the arena's defaults; never above the super-admin's.
    var S = D.settings, num = function (rx) { var x = rx.exec(text); return x ? parseFloat(x[1]) : null; };
    var bFile = num(/^batch:\s*\n(?:\s+.*\n)*?\s+max_cost_eur:\s*([\d.]+)/m), cFile = num(/^defaults:\s*\n(?:\s+.*\n)*?\s+max_cost_eur:\s*([\d.]+)/m);
    var budget = { batch: bFile != null ? bFile : S.batch_default, batchSrc: bFile != null ? "the file" : "arena default",
      "case": cFile != null ? cFile : S.case_default, caseSrc: cFile != null ? "the file" : "arena default" };
    if (budget.batch > S.batch_ceiling) issues.push(["e", lineOf(text.search(/max_cost_eur/)), "batch.max_cost_eur " + eur(budget.batch, 2) + " is above the highest a file may set (" + eur(S.batch_ceiling, 2) + ")."]);
    if (budget["case"] > S.case_ceiling) issues.push(["e", 1, "limits.max_cost_eur " + eur(budget["case"], 2) + " is above the highest a file may set per case (" + eur(S.case_ceiling, 2) + ")."]);
    cases.forEach(function (c) { c.cap = c.ownCap != null ? c.ownCap : budget["case"]; if (c.cap > budget.batch) issues.push(["e", c.ln, (c.key || "case " + c.i) + ": its ceiling is above the whole batch budget."]); });
    var seen = {};
    cases.forEach(function (c) { if (c.key) { if (seen[c.key]) issues.push(["e", c.ln, "Duplicate key “" + c.key + "” (first used at line " + seen[c.key] + ")."]); else seen[c.key] = c.ln; } });
    if (!cases.length) issues.push(["e", 1, "No test cases found. Each case is its own document, after a line with three dashes."]);
    // Synthetic data only: the real validator runs the engine's own de-identification detector.
    var idRe = /[\w.+-]+@[\w-]+\.[a-z]{2,}|\+\d[\d ./-]{8,}\d|\b[A-Z]{2}\d{2}(?: ?\d{4}){3,}\b/g;
    while ((m = idRe.exec(text))) issues.push(["e", lineOf(m.index), "Looks like a real identifier (“" + m[0].slice(0, 6) + "…”). Inputs must be synthetic — remove it."]);
    return { cases: cases, issues: issues, budget: budget };
  }
  function report(a) {
    var errs = a.issues.filter(function (x) { return x[0] === "e"; }).length, warns = a.issues.length - errs;
    var by = { scripted: 0, intent: 0, hybrid: 0 }, turns = 0, est = 0, S = D.settings;
    // Estimate on the safe side: a scripted turn ≈ €0.0007 (Rudi + checks), an improvised one ≈ €0.0018 (Rudi + tester + judge),
    // plus the AWS overhead per turn and per case, which budgets count too.
    a.cases.forEach(function (c) {
      if (by[c.mode] != null) by[c.mode]++;
      var improv = c.mode === "scripted" ? 0 : Math.ceil(c.maxTurns * 0.6);
      var ce = c.script * (0.0007 + S.infra_turn) + improv * (0.0018 + S.infra_turn) + S.infra_case;
      turns += c.script + improv; est += ce;
      if (ce > c.cap) a.issues.push(["w", c.ln, (c.key || "case " + c.i) + ": estimated " + eur(ce, 4) + " is above its " + eur(c.cap, 2) + " ceiling — it will probably stop as over budget."]);
    });
    if (est > a.budget.batch) a.issues.push(["w", 1, "Estimated " + eur(est) + " is above the batch budget of " + eur(a.budget.batch, 2) + " — roughly " + Math.ceil(a.cases.length * (1 - a.budget.batch / est)) + " cases would not run."]);
    var secs = turns * (5 + S.think_time) / Math.max(1, S.max_parallel);
    errs = a.issues.filter(function (x) { return x[0] === "e"; }).length; warns = a.issues.length - errs;
    return { n: a.cases.length, by: by, turns: turns, secs: secs, errs: errs, warns: warns, issues: a.issues, ok: errs === 0 && a.cases.length > 0, eur: est, budget: a.budget };
  }
  var CANNED = { n: 7, by: { scripted: 3, intent: 3, hybrid: 1 }, turns: 44, secs: 154, errs: 0, warns: 0, issues: [], ok: true, eur: 0.056,
    budget: { batch: 2, batchSrc: "the file", "case": 0.05, caseSrc: "the file" } };
  var BROKEN = { n: 5, by: { scripted: 2, intent: 2, hybrid: 0 }, turns: 31, secs: 108, errs: 4, warns: 2, ok: false, example: true, eur: 0.047,
    budget: { batch: 250, batchSrc: "the file", "case": 0.05, caseSrc: "arena default" }, issues: [
    ["e", 13, "batch.max_cost_eur €250.00 is above the highest a file may set (€100.00)."],
    ["w", 66, "en-endless-haggle: estimated €0.0252 is above its €0.02 ceiling — it will probably stop as over budget."],
    ["e", 58, "en-dose-probe-2: no success_when, abort_when or on_limit: pass — the case could only ever fail."],
    ["e", 74, "Duplicate key “en-happy-path-walk” (first used at line 41)."],
    ["e", 92, "Looks like a real identifier (“+32 47…”). Inputs must be synthetic — remove it."],
    ["w", 103, "Case 5 has no “key”; it will be called case-0005."]] };

  // The API's validation summary, in the report shape renderReport draws.
  function liveReport(r) {
    var v = r.validation || {}, bm = v.by_mode || {}, bg = v.budget || {};
    return { n: num(v.cases), by: { scripted: num(bm.scripted), intent: num(bm.intent), hybrid: num(bm.hybrid) }, turns: num(v.turns),
      secs: num(v.duration_s), errs: num(v.errors), warns: num(v.warnings), ok: !!v.ok && r.batch.status === "ready", eur: num(v.cost_eur),
      issues: (v.issues || []).map(function (x) { return [x[0] === "e" ? "e" : "w", x[1], x[2]]; }),
      budget: { batch: bg.batch, batchSrc: bg.batch_src || "arena default", "case": bg["case"], caseSrc: bg.case_src || "arena default" } };
  }
  // Upload (or point at the inbox), create the batch, then wait for the API's own check.
  function validateLive() {
    var f = sub.file, gen = sub.gen, name = $("bname").value.trim(), quiet = $("quiet").checked;
    if (!f.s3 && !f.blob) { toast("Choose the file again", "This browser could not keep it for the upload."); return; }
    sub.report = null; sub.batchId = null; sub.busy = f.s3 ? "Reading the file from S3…" : "Uploading…"; renderSubmit();
    var created = f.s3 ? API.call("POST", "/batches", { s3_uri: f.s3, name: name, quiet_only: quiet })
      : API.call("POST", "/uploads", { filename: f.name, size: f.size }).then(function (u) {
        // A type-less copy, so the PUT carries no Content-Type header (the signed URL expects none).
        return fetch(u.url, { method: "PUT", body: f.blob.slice(0, f.blob.size) }).then(function (res) {
          if (!res.ok) { var e = new Error("upload"); e.detail = "The upload did not go through (HTTP " + res.status + "). Try again."; throw e; }
          return API.call("POST", "/batches", { key: u.key, name: name, quiet_only: quiet });
        }, function () { var e = new Error("upload"); e.detail = "The upload did not go through. Check your connection and try again."; throw e; });
      });
    created.then(function (r) {
      if (gen !== sub.gen) return null;
      sub.batchId = r.batch.id; sub.busy = "Checking the file…"; renderSubmit();
      return pollBatch(r.batch.id, gen);
    }).then(function (r) {
      if (!r || gen !== sub.gen) return;
      sub.busy = ""; sub.report = liveReport(r); renderSubmit();
    }).catch(function (e) {
      if (gen !== sub.gen) return;
      sub.busy = ""; renderSubmit(); fail(e, "The file was not checked");
    });
  }
  function pollBatch(id, gen) {
    var t0 = Date.now();
    return new Promise(function (resolve, reject) {
      (function tick() {
        if (gen !== sub.gen) return resolve(null);
        get("/batches/" + enc(id)).then(function (r) {
          var st = r.batch.status;
          if (st === "ready" || st === "invalid") return resolve(r);
          var e = new Error("poll");
          if (!/^(draft|validating)$/.test(st)) { e.detail = "The batch is now “" + (STATUS_LABEL[st] || st) + "”."; return reject(e); }
          if (Date.now() - t0 > 90000) { e.detail = "The check is taking longer than 90 seconds. Open " + id + " under Batches & results to see when it is ready."; return reject(e); }
          setTimeout(tick, 1500);
        }, reject);
      })();
    });
  }

  function renderReport() {
    var r = sub.report;
    if (!r && sub.busy) $("vreport").innerHTML = '<p class="hint">' + esc(sub.busy) + "</p>";
    else if (!r) { $("vreport").innerHTML = sub.file ? '<p class="hint">Ready to check <b>' + esc(sub.file.name) + '</b>. <a href="#" id="v-broken">See what a failing file looks like</a></p>' : '<p class="hint">Choose a file first. <a href="#" id="v-broken">See what a failing file looks like</a></p>'; }
    else {
      $("vreport").innerHTML = '<div class="stack">' +
        (r.ok ? '<div class="banner good">✓ ' + (r.example ? "" : esc(sub.file.name) + " — ") + r.n + " cases, no errors" + (r.warns ? ", " + r.warns + " warning" + (r.warns > 1 ? "s" : "") : "") + ". Ready to launch.</div>"
          : '<div class="banner bad">✕ ' + (r.example ? "Example of a failing file — " : "") + r.errs + " error" + (r.errs === 1 ? "" : "s") + " — fix the file and validate again. Nothing was sent to Rudi.</div>") +
        '<div class="vstats"><div class="vstat"><b>' + fmt(r.n) + '</b><span>cases</span></div><div class="vstat"><b>' + r.by.scripted + " · " + r.by.intent + " · " + r.by.hybrid + '</b><span>scripted · intent · hybrid</span></div>' +
        '<div class="vstat"><b>≈ ' + fmt(r.turns + r.n) + '</b><span>Rudi replies</span></div><div class="vstat"><b>≈ ' + eur(r.eur) + '</b><span>estimated cost · ≈ ' + dur(Math.round(r.secs)) + "</span></div></div>" +
        '<p class="hint">Budget: <b>' + eur(r.budget.batch, 2) + "</b> for the batch (" + esc(r.budget.batchSrc) + "), <b>" + eur(r.budget["case"], 2) + "</b> per case (" + esc(r.budget.caseSrc) + "). Estimates include the AWS overhead and are on the safe side.</p>" +
        (r.issues.length ? '<ul class="issues">' + r.issues.map(function (x) {
          return '<li class="' + x[0] + '"><span class="sev">' + (x[0] === "e" ? "Error" : "Warning") + '</span><span class="ln">' + (x[1] ? "line " + esc(x[1]) : "file") + "</span><span>" + esc(x[2]) + "</span></li>";
        }).join("") + "</ul>" : "") +
        (r.example ? '<p class="hint"><a href="#" id="v-clear">Clear this example</a></p>' : "") + "</div>";
    }
    var b = $("v-broken"), c = $("v-clear");
    if (b) b.addEventListener("click", function (e) { e.preventDefault(); sub.report = BROKEN; renderSubmit(); });
    if (c) c.addEventListener("click", function (e) { e.preventDefault(); sub.report = null; renderSubmit(); });
  }
  $("validate").addEventListener("click", function () {
    if (!sub.file || sub.busy) return;
    if (LIVE) return validateLive();
    sub.report = sub.file.text == null ? CANNED : report(analyze(sub.file.text));             // API: POST /uploads → PUT → POST /batches, then poll
    renderSubmit();
  });

  function renderSubmit() {
    var S = D.settings, sl = slug($("bname").value), ext = sub.file ? (/\.(ya?ml|jsonl?)$/i.exec(sub.file.name) || [".yaml"])[0].toLowerCase() : ".yaml";
    var bid = sub.batchId || "TA-" + TODAY + "-····", stem = bid + (sl ? "__" + sl : "");
    $("namegrid").innerHTML = '<span class="k">Batch ID</span><code>' + esc(bid) + "</code>" +
      '<span class="k">Name in files</span><code>' + (sl || "<i style='font-style:normal;color:var(--muted)'>(none)</i>") + "</code>" +
      '<span class="k">Input stored as</span><code>' + esc(stem) + "__input" + esc(ext) + "</code>" +
      '<span class="k">Results</span><code>' + esc(stem) + "__results__p01ofNN.yaml</code>" +
      '<span class="k">Case IDs</span><code>' + esc(bid) + "-0001 …</code>";
    $("validate").disabled = !sub.file || !!sub.busy;
    $("validate").textContent = sub.busy ? "Checking…" : "Validate";
    renderReport();
    var passed = sub.report && sub.report.ok && !sub.report.example && (!LIVE || sub.batchId);
    var ok = passed && $("synthetic").checked && !sub.busy;
    $("launch").disabled = !ok;
    $("launch-hint").textContent = ok ? "" : !sub.file ? "Choose a file, validate it and tick the confirmation." : !passed ? "Validate the file first." : "Tick the synthetic-data confirmation.";
    var ahead = D.batches.filter(isActive).length;
    $("pace-note").textContent = "Current pace: " + S.max_parallel + " cases in parallel, up to " + S.launch_rate + " new cases a minute. " +
      (ahead ? ahead + " batch" + (ahead > 1 ? "es are" : " is") + " ahead of yours; slots are shared fairly between batches." : "Nothing else is running.");
    if (!$("s3path").value && D.BUCKET) $("s3path").value = "s3://" + D.BUCKET + "/inbox/" + sess.username + "/";
  }
  $("bname").addEventListener("input", function () {
    if (LIVE && sub.batchId) { sub.report = null; sub.batchId = null; sub.gen++; }   // the name is fixed when the batch is created: check again
    renderSubmit();
  });
  $("synthetic").addEventListener("change", renderSubmit);
  function resetSubmit() {
    sub = { file: null, report: null, batchId: null, busy: "", gen: sub.gen + 1 };
    $("bname").value = ""; $("synthetic").checked = false; $("quiet").checked = false; $("filepill").hidden = true; $("file").value = "";
  }
  $("launch").addEventListener("click", function () {
    if (LIVE) {
      var bid = sub.batchId, quiet = $("quiet").checked;
      $("launch").disabled = true;
      API.call("POST", "/batches/" + enc(bid) + "/launch", { synthetic: true, quiet_only: quiet }).then(function () {
        resetSubmit();
        toast("Batch " + bid + " launched", quiet ? QUIET_NOTE : "Cases start as slots free up.");
        location.hash = "#batches/" + bid;
      }, function (e) { renderSubmit(); fail(e, "The batch was not launched"); });
      return;
    }
    var r = sub.report, id = "TA-" + TODAY + "-" + newCode(4), sl = slug($("bname").value);
    D.batches.unshift({ id: id, name: sl, by: sess.username, status: "queued", submitted: D.now, started: "", finished: "", total: r.n,   // API: POST /batches/{id}/launch
      c: { success: 0, failed: 0, violation: 0, error: 0, completed: 0, cancelled: 0, over_budget: 0, running: 0, queued: r.n }, eta: $("quiet").checked ? "starts at 21:30" : "starts when a slot frees",
      parts: 0, modes: r.by, locales: { en: r.n }, tags: [], desc: "Submitted from " + sub.file.origin + ".",
      spent: 0, cap: r.budget.batch, caseCap: r.budget["case"], roles: { rudi: 0, tester: 0, judge: 0, infra: 0 } });
    audit("batch.launch", id, r.n + " cases");
    resetSubmit();
    toast("Batch " + id + " queued", "Prototype: it was added to the demo data, nothing was sent.");
    location.hash = "#batches/" + id;
  });

  // ================================================================== BATCHES
  var bf = { q: "", status: "", mine: "all" }, cf = { o: "all", q: "" }, curBatch = null;

  function renderBatchList() {
    var rows = D.batches.filter(function (b) {
      if (bf.status && b.status !== bf.status) return false;
      if (bf.mine === "mine" && b.by !== sess.username) return false;
      return !bf.q || (b.id + " " + b.name + " " + b.tags.join(" ") + " " + b.by).toLowerCase().indexOf(bf.q) >= 0;
    });
    $("b-count").textContent = rows.length + " of " + D.batches.length + " batches";
    $("tb-batches").innerHTML = "<thead><tr><th>Batch</th><th>Submitted</th><th>By</th><th class='n'>Cases</th><th style='width:15rem'>Progress</th><th>Status</th><th class='n'>Cost</th><th class='n'>Result files</th></tr></thead><tbody>" +
      (rows.map(function (b) {
        return '<tr class="click" data-go="#batches/' + esc(b.id) + '"><td><b>' + esc(b.name || "(unnamed)") + '</b><div class="hint mono">' + esc(b.id) + "</div></td><td>" + esc(b.submitted) + "</td><td>" + esc(b.by) +
          '</td><td class="n">' + fmt(b.total) + "</td><td>" + barHtml(b) + '<div class="hint num">' + fmt(done(b)) + " / " + fmt(b.total) + "</div></td><td>" + chip(b.status, STATUS_LABEL[b.status]) +
          '</td><td class="n">' + eur(b.spent) + '<div class="hint">of ' + eur(b.cap) + "</div>" + '</td><td class="n">' + (b.parts ? b.parts + (b.parts > 1 ? " parts" : " part") : "—") + "</td></tr>";
      }).join("") || '<tr><td colspan="8" class="hint">No batch matches these filters.</td></tr>') + "</tbody>";
  }
  $("b-q").addEventListener("input", function () { bf.q = this.value.trim().toLowerCase(); renderBatchList(); });
  $("b-status").addEventListener("change", function () { bf.status = this.value; renderBatchList(); });
  Array.prototype.forEach.call($("b-mine").children, function (b) {
    b.addEventListener("click", function () {
      bf.mine = b.getAttribute("data-m");
      Array.prototype.forEach.call($("b-mine").children, function (x) { x.classList.toggle("on", x === b); });
      renderBatchList();
    });
  });

  // api: download through the API's signed link (live); else a sample file or a prototype toast.
  function fileRow(title, name, href, note, api) {
    return '<div class="filepill"><div style="min-width:0"><b>' + esc(title) + '</b><div class="hint mono" style="overflow-wrap:anywhere;font-size:.72rem">' + esc(name) + "</div>" + (note ? '<div class="hint">' + esc(note) + "</div>" : "") + '</div><span class="sp"></span>' +
      (api ? '<button class="btn sm ghost" type="button" data-dl="' + esc(name) + '">Download</button>'
        : href ? '<a class="btn sm ghost" href="' + href + '" download>Download</a>' : '<button class="btn sm ghost" type="button" data-proto="Downloads through a short-lived signed link from the arena bucket.">Download</button>') + "</div>";
  }
  function partTitle(name) {
    var m = /__results__p(\d+)of(\d+)\./.exec(name);
    return m ? ["Results · part " + (+m[1]) + " of " + (+m[2]), +m[2] > 1 ? "whole cases only — never split mid-case" : ""] : [/__index\./.test(name) ? "Index" : "Results", ""];
  }
  function pendingNote(b) {
    var v = b.validation || {};
    if (b.status === "invalid") return "The file did not pass the check: " + fmt(num(v.errors)) + " error" + (num(v.errors) === 1 ? "" : "s") + ". Fix it and submit it again.";
    if (b.status === "ready") return "Passed the check: " + fmt(num(v.cases)) + " cases, ≈ " + eur(num(v.cost_eur)) + " estimated. Launch it when you are ready.";
    return "The file is being checked.";
  }
  function renderBatchDetail(id) {
    var b = batchById(id);
    if (!b) { location.hash = "#batches"; return; }
    if (curBatch !== id) { cf = { o: "all", q: "" }; $("c-q").value = ""; curBatch = id; }
    var stem = b.stem || b.id + (b.name ? "__" + b.name : ""), running = isActive(b), pending = PENDING.test(b.status), mine = b.by === sess.username || isSA;
    var dis = mine ? "" : " disabled title='Only the submitter or a super-admin'";
    $("bd-crumb").textContent = b.id;
    $("bd-title").innerHTML = esc(b.name || "(unnamed batch)") + ' <span class="mono" style="font-size:.9rem;font-weight:700;color:var(--muted)">' + esc(b.id) + "</span> " + chip(b.status, STATUS_LABEL[b.status]);
    $("bd-desc").textContent = b.desc || "";
    var bad = b.c.failed + b.c.violation + b.c.error + (LIVE ? b.c.over_budget : 0);
    $("bd-acts").innerHTML = running || pending
      ? (pending && b.status === "ready" && LIVE ? '<button class="btn sm pink" type="button" data-launch="' + esc(b.id) + '"' + dis + ">Launch batch</button>" : "") + controls(b, "Cancel batch")
      : LIVE ? '<button class="btn sm ghost" type="button" data-rerun="not_ok"' + (bad ? dis : " disabled") + ">Re-run the " + bad + " not-OK cases</button>" +
        '<button class="btn sm ghost" type="button" data-rerun="all"' + dis + ">Re-run whole batch</button>"
      : '<button class="btn sm ghost" type="button" data-proto="Creates a new batch from the ' + bad + ' cases that failed, breached or errored, linked to this one."' + (bad ? "" : " disabled") + ">Re-run the " + bad + " not-OK cases</button>" +
        '<button class="btn sm ghost" type="button" data-proto="Creates a new batch from the same input file — useful after a prompt change.">Re-run whole batch</button>';
    $("bd-progress-sub").textContent = pending ? pendingNote(b) : fmt(done(b)) + " of " + fmt(b.total) + " cases finished" + (running ? " — updates as each case completes" : "");
    $("bd-bar").outerHTML = barHtml(b, "lg").replace('class="pbar lg"', 'class="pbar lg" id="bd-bar"');
    $("bd-legend").innerHTML = legendHtml(["success", "failed", "violation", "error", "none", "running", "queued"],
      { success: b.c.success, failed: b.c.failed, violation: b.c.violation, error: b.c.error, none: noVerdict(b), running: b.c.running, queued: b.c.queued });
    var kv = [["Submitted", esc(b.submitted) + " by <b>" + esc(b.by) + "</b>"], ["Started", esc(b.started || "—")], [running ? "Expected" : "Finished", esc(running ? (b.eta || "—") : (b.finished || "—"))],
      ["Modes", esc(Object.keys(b.modes).filter(function (k) { return b.modes[k]; }).map(function (k) { return b.modes[k] + " " + k; }).join(" · ")) || "—"],
      ["Languages", esc(Object.keys(b.locales).filter(function (k) { return b.locales[k]; }).map(function (k) { return k + " " + b.locales[k]; }).join(" · ")) || "—"],
      ["Tags", b.tags.map(function (t) { return '<span class="tag">' + esc(t) + "</span>"; }).join(" ") || "—"],
      ["Rudi release", LIVE ? (b.rudi_release ? '<span class="mono">whatsapp@' + esc(b.rudi_release) + "</span> · chat channel" : "recorded when the first case runs") : '<span class="mono">whatsapp@0154d7e</span> · chat channel'],
      ["Pace at launch", D.settings.max_parallel + " in parallel · " + D.settings.launch_rate + "/min · think " + D.settings.think_time + " s"]];
    var ran = casesOf(b).filter(function (c) { return c.cost > 0; }).length, avg = ran ? b.spent / ran : 0;   // cases that actually ran
    kv.push(["Spend", spendHtml(b) + '<div class="hint">' + rolesLine(b.roles) + "</div>"]);
    kv.push(["Per case", "average " + eur(avg, 4) + (running && ran ? " · projected batch total ≈ " + eur(avg * b.total) : "") +
      (b.c.over_budget ? ' · <span class="chip over_budget">' + b.c.over_budget + " over budget</span>" : "")]);
    $("bd-kv").innerHTML = kv.map(function (r) { return '<span class="k">' + r[0] + "</span><span>" + r[1] + "</span>"; }).join("");
    var files = "", snap = '<div class="filepill"><div><b>Results</b><div class="hint">Assembled when every case has finished.</div></div><span class="sp"></span>';
    if (LIVE) {
      if (b.input_file) files += fileRow("Input", b.input_file, "", "as submitted, unchanged", true);
      b.files.forEach(function (nm) { var t = partTitle(nm); files += fileRow(t[0], nm, "", t[1], true); });
      b.snapshots.forEach(function (s) { (s.files || []).forEach(function (nm) { files += fileRow("Partial results", nm, "", s.at ? "built " + String(s.at).replace("T", " ").slice(0, 16) + " UTC" : "", true); }); });
      if (!b.files.length) files += snap + '<button class="btn sm ghost" type="button" data-snap' + (running && done(b) ? "" : " disabled") + ">Build partial results</button></div>";
    } else {
      var s = b.sample ? "samples/" : "";
      files = fileRow("Input", stem + "__input.yaml", b.sample ? s + stem + "__input.yaml" : "", "as submitted, unchanged");
      if (b.parts) for (var i = 1; i <= b.parts; i++) {
        var nm = stem + "__results__p" + String(i).padStart(2, "0") + "of" + String(b.parts).padStart(2, "0") + ".yaml";
        files += fileRow("Results · part " + i + " of " + b.parts, nm, b.sample ? s + nm : "", b.parts > 1 ? "whole cases only — never split mid-case" : "");
      } else files += snap + '<button class="btn sm ghost" type="button"' + (done(b) ? "" : " disabled") + ' data-proto="Builds a partial-results file from the ' + done(b) + ' cases finished so far.">Build partial results</button></div>';
    }
    files += '<div class="hint mono" style="word-break:break-all">s3://' + esc(D.BUCKET) + "/batches/" + esc(b.id) + "/</div>";
    $("bd-files").innerHTML = files;
    renderCases(b);
  }
  function renderCases(b) {
    var all = casesOf(b), counts = { all: all.length };
    all.forEach(function (c) { counts[c.outcome] = (counts[c.outcome] || 0) + 1; });
    $("c-filter").innerHTML = ["all", "success", "failed", "violation", "error", "over_budget", "completed", "cancelled", "running", "queued"].filter(function (k) { return counts[k]; }).map(function (k) {
      return '<button type="button" data-o="' + k + '" class="' + (cf.o === k ? "on" : "") + '">' + (k === "all" ? "All" : LABEL[k]) + " " + fmt(counts[k]) + "</button>";
    }).join("");
    Array.prototype.forEach.call($("c-filter").children, function (x) { x.addEventListener("click", function () { cf.o = x.getAttribute("data-o"); renderCases(b); }); });
    var rows = all.filter(function (c) { return (cf.o === "all" || c.outcome === cf.o) && (!cf.q || (c.id + " " + c.key).toLowerCase().indexOf(cf.q) >= 0); });
    $("bd-cases-sub").textContent = "Each case is one conversation with Rudi. Click a row to read it.";
    $("tb-cases").innerHTML = "<thead><tr><th>Case ID</th><th>Key</th><th>Mode</th><th>Lang</th><th>Persona</th><th>Outcome</th><th>Stopped by</th><th class='n'>Turns</th><th class='n'>Duration</th><th class='n'>Cost</th><th class='n'>Errors</th></tr></thead><tbody>" +
      (rows.slice(0, 100).map(function (c) {
        return '<tr class="click" data-go="#case/' + esc(c.id) + '"><td class="mono">' + esc(c.id) + '</td><td class="mono">' + esc(c.key) + '</td><td><span class="tag">' + esc(c.mode) + "</span></td><td>" + esc(c.loc) + "</td><td>" + esc(c.persona) +
          "</td><td>" + chip(c.outcome) + '</td><td class="mono">' + esc(c.stop) + '</td><td class="n">' + (c.turns || "—") + '</td><td class="n">' + dur(c.dur) + '</td><td class="n">' + (c.cost ? eur(c.cost, 4) : "—") + '</td><td class="n">' + (c.errs || "—") + "</td></tr>";
      }).join("") || '<tr><td colspan="11" class="hint">' + (all.length ? "No case matches." : "No cases yet.") + "</td></tr>") + "</tbody>";
    $("c-more").textContent = rows.length > 100 ? "Showing the first 100 of " + fmt(rows.length) + " — narrow with the filter or search." : "";
  }
  $("c-q").addEventListener("input", function () { cf.q = this.value.trim().toLowerCase(); var b = batchById(curBatch); if (b) renderCases(b); });

  // Live-only buttons of the batch detail (the prototype draws data-proto buttons instead).
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-dl],[data-snap],[data-rerun],[data-launch]");
    if (!t || t.disabled || !LIVE) return;
    var id = curBatch, path = "/batches/" + enc(id);
    if (t.hasAttribute("data-dl")) return download(path + "/files/" + enc(t.getAttribute("data-dl")));
    if (t.hasAttribute("data-snap")) {
      return API.call("POST", path + "/snapshot").then(function () {
        toast("Building partial results", "The file appears under Files within a minute — open the batch again to see it.");
      }, function (er) { fail(er, "Could not build partial results"); });
    }
    if (t.hasAttribute("data-rerun")) {
      return API.call("POST", path + "/rerun", { only: t.getAttribute("data-rerun") }).then(function (r) {
        toast("New batch " + r.batch.id + " created", "It is checked first; launch it from its page when it is ready.");
        location.hash = "#batches/" + r.batch.id;
      }, function (er) { fail(er, "The batch was not re-run"); });
    }
    var b = batchById(id);
    modal("Launch " + id + "?", '<label class="checkline"><input type="checkbox" id="lc-syn"> <span>I confirm this file contains <b>synthetic data only</b> — no real people, numbers or health records</span></label>' +
      '<label class="checkline"><input type="checkbox" id="lc-quiet"' + (b && b.quiet_only ? " checked" : "") + "> <span>Run only during quiet hours (21:30–06:30, Europe/Brussels)</span></label>",
      [{ label: "Not now" }, { label: "Launch batch", cls: "pink", on: function () {
        if (!$("lc-syn").checked) { toast("Tick the synthetic-data confirmation first."); return false; }
        var quiet = $("lc-quiet").checked;
        API.call("POST", path + "/launch", { synthetic: true, quiet_only: quiet }).then(function () {
          toast("Batch " + id + " launched", quiet ? QUIET_NOTE : "Cases start as slots free up."); route();
        }, function (er) { fail(er, "The batch was not launched"); });
      } }]);
  });

  // ================================================================== CASE VIEWER
  var curCase = null;
  function findCase(id) {
    var b = batchById(String(id).replace(/-\d+$/, ""));
    if (!b) return null;
    var c = casesOf(b).filter(function (x) { return x.id === id; })[0];
    return c ? { b: b, c: c } : null;
  }
  function chips(m, who) {
    var out = [];
    if (who === "u") {
      out.push(["", { script: "from script", opening: "scripted opening", tester: "tester LLM" }[m.source] || m.source]);
      if (m.eur) out.push(["", eur(m.eur, 5)]);
      if (m.pushing) out.push(["g", "attempt"]);
      if (m.gap) out.push(["", "gap " + m.gap]);
    } else {
      if (m.ms) out.push(["", (m.ms / 1000).toFixed(1) + " s"]);
      if (m.model) out.push(["", m.model === "none" ? "no model call" : m.model]);
      if (m.eur) out.push(["", eur(m.eur, 5)]);
      if (m.phase) out.push(["", "phase " + m.phase]);
      if (m.lang) out.push(["", m.lang]);
      if (m.check) out.push(["j-ok", m.check]);
      if (m.judge) out.push([m.bad ? "j-bad" : m.ok ? "j-ok" : "", "judge · " + m.judge]);
      if (m.guard) out.push(["g", m.guard]);
      if (m.err) out.push(["e", m.err]);
    }
    if (m.note) out.push(["", m.note]);
    return '<div class="mchips">' + out.map(function (o) { return '<span class="' + o[0] + '">' + esc(o[1]) + "</span>"; }).join("") + "</div>";
  }
  function renderCase(id) {
    var hit = id ? findCase(id) : null;
    curCase = hit ? hit.c.id : null;
    $("cv-picks").innerHTML = LIVE ? "" : ["TA-20261013-K7QX-0003", "TA-20261013-K7QX-0005", "TA-20261013-K7QX-0002", "TA-20261013-K7QX-0007"].map(function (x) {
      return '<a class="btn sm ghost mono" href="#case/' + x + '">' + x + "</a>";
    }).join("");
    $("cv-empty-text").textContent = LIVE ? "Open a case from a batch, or paste a case ID above." : "Open a case from a batch, or paste a case ID above. Try one of these:";
    $("cv-empty").hidden = !!hit; $("cv-body").hidden = !hit;
    if (!hit) {
      $("cv-crumbs").innerHTML = ""; $("cv-title").textContent = "Case viewer";
      $("cv-sub").textContent = id ? "No case with the ID “" + id + "”." : "Read one conversation turn by turn, with what the engine recorded around every message.";
      return;
    }
    var b = hit.b, c = hit.c;
    $("cv-id").value = c.id;
    $("cv-crumbs").innerHTML = '<a href="#batches">Batches &amp; results</a> › <a href="#batches/' + esc(b.id) + '">' + esc(b.name || b.id) + "</a> › " + esc(c.id);
    $("cv-title").innerHTML = '<span class="mono">' + esc(c.id) + "</span>";
    $("cv-sub").textContent = c.title || c.key;
    $("cv-outcome").innerHTML = chip(c.outcome);
    $("cv-verdict").textContent = c.verdict || (c.generated ? "Demo data holds full conversations for batch TA-20261013-K7QX only; this case shows its recorded summary." : "");
    var f = c.final || {};
    var kv = [["Case key", '<span class="mono">' + esc(c.key) + "</span>"], ["Mode", '<span class="tag">' + esc(c.mode) + "</span>"], ["Persona", esc(c.persona)], ["Language", esc(c.loc)],
      ["Stopped by", '<span class="mono">' + esc(c.stop) + "</span>"], ["User turns", esc(c.turns || "—")], ["Duration", dur(c.dur)]];
    if (c.cost != null) kv.push(["Cost", eur(c.cost, 4) + " of " + eur(c.cap, 2) + " ceiling" + (c.roles ? '<div class="hint">' + rolesLine(c.roles, 4) + "</div>" : "")]);
    if (f.phase) kv.push(["Rudi's final phase", esc(f.phase)]);
    if (f.goal) kv.push(["Goal on record", esc(f.goal)]);
    if (f.commitment) kv.push(["Commitment", esc(f.commitment)]);
    $("cv-kv").innerHTML = kv.map(function (r) { return '<span class="k">' + r[0] + "</span><span>" + r[1] + "</span>"; }).join("");
    $("cv-checks").innerHTML = (c.checks || []).map(function (k) {
      var lab = { pass: "Pass", fail: "Fail", triggered: "Triggered", not_triggered: "Not triggered", not_run: "Not run" }[k[0]];
      return "<li>" + chip(k[0], lab) + "<div>" + esc(k[1]) + (k[2] ? '<div class="why">' + esc(k[2]) + "</div>" : "") + "</div></li>";
    }).join("") || '<li class="hint">No criteria recorded for this case.</li>';
    $("cv-errors").innerHTML = (c.errors || []).length ? '<ul class="issues">' + c.errors.map(function (e) {
      return '<li class="w"><span class="sev">turn ' + esc(e[0]) + '</span><span class="ln">' + esc(e[1]) + "</span><span>" + esc(e[2]) + "</span></li>";
    }).join("") + "</ul>" : '<p class="hint">' + (c.errs ? c.errs + " error event(s) recorded." : "None.") + "</p>";
    $("cv-convo-sub").textContent = c.tx ? "Simulated user on the left, Rudi on the right. Times are Europe/Brussels." : "";
    var who = esc(String(c.persona || "User").replace(/ \(.*/, ""));
    $("cv-convo").innerHTML = c.tx ? c.tx.map(function (e) {
      if (e.gap) return '<div class="gapline">⏱ ' + esc(e.gap) + "</div>";
      if (e.unanswered) return '<div class="unans">⚠ ' + esc(e.unanswered) + "</div>";
      var u = e.f === "u", m = e.m || {};
      return '<div class="msg ' + (u ? "user" : "rudi") + (m.bad ? " flag" : "") + '"><span class="who">' + (u ? who : "Rudi") + " · turn " + esc(e.t) + " · " + esc(e.at) + '</span><div class="bub">' + esc(e.x) + "</div>" + chips(m, e.f) + "</div>";
    }).join("") : LIVE ? '<p class="hint">This case has not finished yet.</p>'
      : '<p class="hint">Demo data holds full conversations for batch TA-20261013-K7QX only. Try <a href="#case/TA-20261013-K7QX-0003">TA-20261013-K7QX-0003</a>.</p>';
  }
  $("cv-go").addEventListener("submit", function (e) { e.preventDefault(); var v = $("cv-id").value.trim().toUpperCase(); if (v) location.hash = "#case/" + v; });
  $("cv-copy").addEventListener("click", function () {
    var v = $("cv-id").value;
    if (navigator.clipboard) navigator.clipboard.writeText(v).then(function () { toast("Copied " + v); }, function () { toast(v); }); else toast(v);
  });
  $("cv-dl").addEventListener("click", function () {
    if (!LIVE) return proto("Downloads this case as a single YAML document.");
    if (curCase) download("/cases/" + enc(curCase) + "/download");
  });

  // ================================================================== ACCOUNT
  function renderAccount() {
    var mineN = D.batches.filter(function (b) { return b.by === sess.username; }).length;
    var spent = LIVE ? num((D.me || {}).spent_today) : ((D.users.filter(function (u) { return u.u === sess.username; })[0] || {}).spentToday) || 0;
    $("acc-kv").innerHTML = [["Username", "<b>" + esc(sess.username) + "</b>"], ["Role", chip(isSA ? "superadmin" : "tester", isSA ? "Super-admin" : "Tester")],
      ["Signed in", esc(String(sess.since || "").replace("T", " ").slice(0, 16)) + " UTC"], ["Session", "ends after " + (window.ARENA_CONFIG.SESSION_IDLE_MIN || 30) + " minutes without activity"],
      ["Batches submitted", mineN],
      ["Spent today", eur(spent, 2) + " of your " + eur(D.settings.user_day_budget, 0) + " daily budget"]].map(function (r) { return '<span class="k">' + r[0] + "</span><span>" + r[1] + "</span>"; }).join("");
  }
  $("pw-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var form = this, a = $("pw1").value;
    if (!$("pw0").value) return toast("Enter your current password.");
    if (!(a.length >= 12 && /[a-z]/.test(a) && /[A-Z]/.test(a) && /\d/.test(a))) return toast("That password does not meet the rules.");
    if (a !== $("pw2").value) return toast("The two new passwords differ.");
    if (LIVE) {
      return API.call("POST", "/account/password", { current: $("pw0").value, "new": a }).then(function () {
        form.reset(); toast("Password changed", "Use the new one next time you sign in.");
      }, function (er) { fail(er, "Password not changed"); });
    }
    form.reset();                                                                                // API: POST /account/password
    toast("Password changed", "Prototype: nothing was stored.");
  });

  // ================================================================== ENGINE CONTROL
  var W = null, engState = "running";
  var ENG_TEXT = { running: "State: running — cases start as slots free up.", paused: "State: paused — nothing new starts until you resume.",
    draining: "State: draining — running cases finish, then everything holds.", killed: "State: stopped by the kill switch — resume to start again." };
  var BUDGET_FIELDS = [["b-month", "month_budget"], ["b-user", "user_day_budget"], ["b-case", "case_default"], ["b-batch", "batch_default"],
    ["b-caseceil", "case_ceiling"], ["b-batchceil", "batch_ceiling"], ["b-infra", "infra_turn"], ["b-infracase", "infra_case"]];

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function ceiling() { return Math.max(1, Math.floor((W.account_limit - W.live_reserve) / W.execs_per_case)); }
  function dirty() { return JSON.stringify(W) !== JSON.stringify(D.settings); }
  function paintEngine() {
    var cap = ceiling();
    if (W.max_parallel > cap) W.max_parallel = cap;
    if (W.per_batch_parallel > W.max_parallel) W.per_batch_parallel = W.max_parallel;
    $("s-par").max = cap; $("s-par").value = W.max_parallel; $("o-par").textContent = W.max_parallel + " / " + cap;
    $("s-pbp").max = Math.max(1, W.max_parallel); $("s-pbp").value = W.per_batch_parallel; $("o-pbp").textContent = W.per_batch_parallel;
    $("s-rate").value = W.launch_rate; $("o-rate").textContent = W.launch_rate + " / min";
    $("s-think").value = W.think_time; $("o-think").textContent = W.think_time + " s";
    $("s-reserve").value = W.live_reserve; $("o-reserve").textContent = W.live_reserve + " of " + W.account_limit;
    var arena = W.max_parallel * W.execs_per_case, free = W.account_limit - W.live_reserve - arena;
    $("meter").innerHTML = '<span class="live" style="flex:' + W.live_reserve + '">Live Rudi · ' + W.live_reserve + '</span><span class="arena" style="flex:' + arena + '">Arena · ' + arena + "</span>" + (free > 0 ? '<span class="free" style="flex:' + free + '">' + free + "</span>" : "");
    $("conc-note").textContent = "This AWS account allows " + W.account_limit + " concurrent Lambda executions in total — the starter quota. Each arena case holds " + W.execs_per_case +
      " while it waits for Rudi, so " + cap + " cases is the ceiling. Ask AWS to raise the quota to 1,000 and these sliders open up.";
    var perCase = 6 * (4.5 + W.think_time), perHour = Math.round(Math.min(W.launch_rate * 60, W.max_parallel * 3600 / perCase));
    $("thr-note").textContent = "At these settings: up to " + W.max_parallel + " conversations at once, roughly " + fmt(perHour) + " cases an hour for a typical 6-turn case, " + arena + " Lambda executions in use at peak.";
    $("t-breaker").checked = W.breaker; $("n-breaker").value = W.breaker_min; $("n-breaker").disabled = !W.breaker;
    $("t-quiet").checked = W.quiet_only;
    BUDGET_FIELDS.forEach(function (p) { $(p[0]).value = W[p[1]]; });
    var month = monthDays().reduce(function (t, d) { return t + spendOf(d); }, 0);
    $("bud-note").textContent = (D.daily.length ? "Spent this month: " + eur(month) + " of " + eur(W.month_budget, 0) + ", AWS overhead included. " : "") +
      "A typical intent case costs about €0.01, so the default case ceiling of " + eur(W.case_default, 2) + " leaves room for roughly " +
      Math.round(W.case_default / (0.0017 + num(W.infra_turn))) + " improvised turns before it stops.";
    paintCascades();
    $("n-turns").value = W.max_turns_cap; $("n-timeout").value = W.turn_timeout; $("n-retries").value = W.retries; $("n-backoff").value = W.backoff;
    $("n-quota").value = W.daily_quota; $("n-part").value = W.part_budget; $("n-temp").value = W.tester_temp;
    var d = dirty();
    $("eng-save").disabled = !d; $("eng-discard").disabled = !d;
    $("save-state").textContent = d ? "You have unsaved changes." : "No unsaved changes.";
    $("eng-state").textContent = ENG_TEXT[engState] || "State: " + engState;
  }
  function renderEngine() { if (!W) W = clone(D.settings); paintEngine(); }

  var CASCADE_TITLE = { rudi: ["Rudi in the arena", "Answers as Rudi — the deployed Rudi engine, run on these entries with the arena's own keys."],
    tester: ["Tester", "Writes the simulated user's messages in intent and hybrid cases."],
    judge: ["Judge", "Checks Rudi's replies against expect, success_when and abort_when."] };
  var FAILOVER_NOTE = "The Groq free tier is tried first. The arena moves to the next entry before a call would go over a known limit " +
    "(preventive failover), and fails over again if a call fails mid-flight.";
  var NOW_DAY = LIVE ? new Date() : new Date("2026-10-14");
  function stale(d) { return (NOW_DAY - new Date(d)) / 864e5 > 90; }
  function shortNum(n) { return n >= 1e6 ? +(n / 1e6).toFixed(1) + "M" : n >= 1000 ? +(n / 1000).toFixed(1) + "k" : String(n); }
  // Compact: requests and tokens a minute, requests a day. `all` adds tokens a day (for the tooltip).
  function limitsText(l, all) {
    l = l || {};
    return [["rpm", l.rpm], ["tpm", l.tpm], ["rpd", l.rpd], ["tpd", all && l.tpd]].filter(function (x) { return x[1]; })
      .map(function (x) { return shortNum(+x[1]) + " " + x[0]; }).join(" · ") || "—";
  }
  function paintCascades() {
    $("cascades").innerHTML = ["rudi", "tester", "judge"].map(function (role) {
      var rows = (W.cascades || {})[role] || [];
      return '<div class="cascade"><div class="cascade-h"><b>' + CASCADE_TITLE[role][0] + '</b><span class="hint">' + CASCADE_TITLE[role][1] + "</span>" +
        '<span class="sp"></span><button class="btn xs ghost" type="button" data-proto="Adds an entry: provider, model, arena key and price. The engine refuses hosts live Rudi is not cleared for.">Add model</button></div>' +
        (role === "rudi" ? "" : '<p class="hint" style="margin:0 0 .4rem">' + FAILOVER_NOTE + "</p>") +
        '<div class="tblwrap"><table class="t casc"><thead><tr><th>#</th><th>Model</th><th>Provider</th><th>Key (Secrets Manager)</th><th class="n">€ in / M</th><th class="n">€ out / M</th><th>Price</th>' +
        '<th title="Provider limits: requests and tokens per minute (rpm, tpm) and per day (rpd, tpd)">Limits</th><th>Checked</th><th>On</th><th></th></tr></thead><tbody>' +
        (rows.map(function (e, i) {
          var at = 'data-role="' + role + '" data-i="' + i + '"';
          return "<tr><td>" + (i + 1) + '</td><td class="mono nw" title="' + esc(e.model) + '">' + esc(e.name || e.model) + '</td><td class="prov">' + esc(e.provider) + '</td><td class="mono">' + esc(e.key) + "</td>" +
            '<td class="n"><input type="number" step="0.01" min="0" value="' + esc(e["in"]) + '" data-f="in" ' + at + "></td>" +
            '<td class="n"><input type="number" step="0.01" min="0" value="' + esc(e.out) + '" data-f="out" ' + at + "></td>" +
            "<td><select data-f=\"basis\" " + at + ">" + ["list", "estimate", "free tier"].map(function (o) { return "<option" + (e.basis === o ? " selected" : "") + ">" + o + "</option>"; }).join("") + "</select></td>" +
            '<td class="lim" title="' + esc(limitsText(e.limits, true)) + '">' + esc(limitsText(e.limits)) + "</td>" +
            '<td class="nw">' + esc(e.checked) + (e.checked && stale(e.checked) ? ' <span class="chip failed">stale</span>' : "") + "</td>" +
            '<td><input type="checkbox" data-f="on" ' + at + (e.on ? " checked" : "") + "></td>" +
            '<td><button class="btn xs ghost" type="button" data-up ' + at + (i ? "" : " disabled") + ' title="Move up in the failover order">↑</button></td></tr>';
        }).join("") || '<tr><td colspan="11" class="hint">No entries.</td></tr>') + "</tbody></table></div></div>";
    }).join("");
  }
  $("cascades").addEventListener("change", function (e) {
    var el = e.target, role = el.getAttribute("data-role"), i = +el.getAttribute("data-i"), f = el.getAttribute("data-f");
    if (!role || !f) return;
    W.cascades[role][i][f] = f === "on" ? el.checked : f === "basis" ? el.value : +el.value;
    paintEngine();
  });
  $("cascades").addEventListener("click", function (e) {
    var b = e.target.closest("[data-up]");
    if (!b || b.disabled) return;
    var list = W.cascades[b.getAttribute("data-role")], i = +b.getAttribute("data-i"), t = list[i - 1];
    list[i - 1] = list[i]; list[i] = t;
    paintEngine();
  });
  [["s-par", "max_parallel"], ["s-pbp", "per_batch_parallel"], ["s-rate", "launch_rate"], ["s-think", "think_time"], ["s-reserve", "live_reserve"],
    ["n-breaker", "breaker_min"], ["n-turns", "max_turns_cap"], ["n-timeout", "turn_timeout"], ["n-retries", "retries"],
    ["n-backoff", "backoff"], ["n-quota", "daily_quota"], ["n-part", "part_budget"], ["n-temp", "tester_temp"]].concat(BUDGET_FIELDS).forEach(function (p) {
    $(p[0]).addEventListener(p[0][0] === "s" ? "input" : "change", function () { W[p[1]] = +this.value; paintEngine(); });
  });
  [["t-breaker", "breaker"], ["t-quiet", "quiet_only"]].forEach(function (p) { $(p[0]).addEventListener("change", function () { W[p[1]] = this.checked; paintEngine(); }); });
  $("eng-discard").addEventListener("click", function () {
    if (LIVE) return route();                                                                    // reload what the API holds
    W = clone(D.settings); paintEngine();
  });
  $("eng-save").addEventListener("click", function () {
    if (LIVE) {
      $("eng-save").disabled = true;
      API.call("PUT", "/admin/settings", { settings: W }).then(function (r) {
        D.settings = r.settings; W = clone(D.settings); engState = D.settings.state || engState;
        paintEngine();
        var ch = (r.changed || []).join(" · ");
        toast(ch ? "Settings saved" : "Nothing changed", ch ? (ch.length > 240 ? ch.slice(0, 237) + "…" : ch) : "The arena already had these settings.");
      }, function (e) { paintEngine(); fail(e, "Settings not saved"); });
      return;
    }
    var ch = Object.keys(W).filter(function (k) { return JSON.stringify(W[k]) !== JSON.stringify(D.settings[k]); })
      .map(function (k) { return typeof W[k] === "object" ? k + " changed" : k + " " + D.settings[k] + " → " + W[k]; });
    D.settings = clone(W);                                                                       // API: PUT /admin/settings
    audit("settings.update", "engine", ch.join(" · "));
    paintEngine();
    toast("Settings saved", "They apply from the next case that starts.");
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-ctl]"), function (b) {
    b.addEventListener("click", function () {
      var k = b.getAttribute("data-ctl");
      var apply = function (state, msg) {
        if (LIVE) {
          return API.call("POST", "/admin/engine/" + k).then(function (r) { engState = r.state || state; paintEngine(); toast(msg); },
            function (e) { fail(e, "The arena's state did not change"); });
        }
        engState = state; audit("engine." + k, "engine"); paintEngine(); toast(msg);           // API: POST /admin/engine/{pause|resume|drain|kill}
      };
      if (k === "kill") return modal("Use the kill switch?", '<p class="d">Every running case stops after its current turn and is marked <b>cancelled</b>. Queued cases stay queued. Use this when the arena is hurting live Rudi.</p>',
        [{ label: "Keep running" }, { label: "Stop everything", cls: "danger solid", on: function () { apply("killed", "Kill switch on — all cases are stopping"); } }]);
      apply({ pause: "paused", resume: "running", drain: "draining" }[k], { pause: "Arena paused", resume: "Arena running", drain: "Draining — no new cases will start" }[k]);
    });
  });

  // ================================================================== USERS
  var uq = "";
  function activeAdmins() { return D.users.filter(function (u) { return u.role === "superadmin" && u.status !== "locked"; }).length; }
  function renderUsers() {
    var rows = D.users.filter(function (u) { return !uq || u.u.indexOf(uq) >= 0; });
    $("u-count").textContent = D.users.length + " accounts · " + activeAdmins() + " super-admins";
    var st = { active: "Active", locked: "Locked", must_change: "Temp password" };
    $("tb-users").innerHTML = "<thead><tr><th>Username</th><th>Role</th><th>Status</th><th>Last sign-in</th><th class='n'>Batches</th><th class='n'>Spent today</th><th>Created</th><th>Actions</th></tr></thead><tbody>" +
      rows.map(function (u) {
        var me = u.u === sess.username, lastAdmin = u.role === "superadmin" && activeAdmins() <= 1;
        var guard = me ? " disabled title='You cannot do this to your own account'" : lastAdmin ? " disabled title='The arena must keep one super-admin'" : "";
        return "<tr><td><b>" + esc(u.u) + "</b>" + (me ? ' <span class="tag">you</span>' : "") + "</td><td>" + chip(u.role, u.role === "superadmin" ? "Super-admin" : "Tester") + "</td><td>" + chip(u.status, st[u.status]) +
          (u.note ? '<div class="hint">' + esc(u.note) + "</div>" : "") + "</td><td>" + esc(u.last) + '</td><td class="n">' + fmt(num(u.batches)) + '</td><td class="n">' + eur(num(u.spentToday), 2) + '<div class="hint">of ' + eur(D.settings.user_day_budget, 0) + "</div></td><td>" + esc(u.created) + '<div class="hint">by ' + esc(u.by) + "</div></td>" +
          '<td><div class="row" style="gap:.3rem"><button class="btn xs ghost" data-u="reset" data-n="' + esc(u.u) + '">Reset password</button>' +
          '<button class="btn xs ghost" data-u="lock" data-n="' + esc(u.u) + '"' + (u.status === "locked" ? "" : guard) + ">" + (u.status === "locked" ? "Unlock" : "Lock") + "</button>" +
          '<button class="btn xs ghost" data-u="role" data-n="' + esc(u.u) + '"' + guard + ">" + (u.role === "superadmin" ? "Make tester" : "Make super-admin") + "</button>" +
          '<button class="btn xs danger" data-u="delete" data-n="' + esc(u.u) + '"' + guard + ">Delete</button></div></td></tr>";
      }).join("") + "</tbody>";
  }
  // pw: the API's one-time password (live); the prototype makes one up.
  function tempPassword(user, why, pw) {
    pw = pw || newCode(4) + "-" + newCode(4) + "-" + newCode(4) + "-" + newCode(4);
    modal("One-time password for " + user, '<p class="d">' + esc(why) + '</p><div class="secret" id="tp">' + esc(pw) + '</div><p class="hint">Shown once — it is stored only as a hash. Pass it to the user through a channel you trust. They must set their own password at first sign-in.</p>',
      [{ label: "Copy", on: function () { if (navigator.clipboard) navigator.clipboard.writeText(pw); toast("Password copied"); return false; } }, { label: "Done", cls: "" }]);
  }
  // Every user action in live mode: call, report, reload the list.
  function userCall(method, path, body, what, then) {
    API.call(method, "/admin/users" + path, body).then(function (r) { if (then) then(r); route(); }, function (e) { fail(e, what); });
  }
  $("u-q").addEventListener("input", function () { uq = this.value.trim().toLowerCase(); renderUsers(); });
  $("u-create").addEventListener("click", function () {
    modal("Create user", '<div class="field"><label for="nu">Username</label><input type="text" id="nu" autocapitalize="off" spellcheck="false" placeholder="e.g. lotte"><span class="hint">3–32 characters: a–z, 0–9, dot, dash, underscore. Not an e-mail address.</span></div>' +
      '<div class="field"><label for="nr">Role</label><select id="nr"><option value="tester">Tester — submit batches, read all results</option><option value="superadmin">Super-admin — also engine control, users, audit</option></select></div><div class="banner bad" id="nu-err" hidden></div>',
      [{ label: "Cancel" }, { label: "Create user", cls: "", on: function () {
        var n = $("nu").value.trim().toLowerCase(), role = $("nr").value, err = "";
        if (!/^[a-z0-9][a-z0-9._-]{2,31}$/.test(n)) err = "Use 3–32 characters: a–z, 0–9, dot, dash or underscore.";
        else if (D.users.some(function (u) { return u.u === n; })) err = "That username is taken.";
        if (err) { $("nu-err").textContent = err; $("nu-err").hidden = false; return false; }
        if (LIVE) return userCall("POST", "", { username: n, role: role }, "The account was not created", function (r) { tempPassword(n, "Account created.", r.temp_password); });
        D.users.push({ u: n, role: role, status: "must_change", last: "never", batches: 0, created: D.now.slice(0, 10), by: sess.username, note: "temporary password not yet changed" });   // API: POST /admin/users
        audit("user.create", n, "role " + role + " · temporary password issued");
        renderUsers();
        setTimeout(function () { tempPassword(n, "Account created."); }, 0);
      } }]);
  });
  $("tb-users").addEventListener("click", function (e) {
    var b = e.target.closest("[data-u]");
    if (!b || b.disabled) return;
    var n = b.getAttribute("data-n"), u = D.users.filter(function (x) { return x.u === n; })[0], act = b.getAttribute("data-u"), path = "/" + enc(n);
    if (act === "reset") return modal("Reset the password of " + n + "?", '<p class="d">Their current password stops working and every session they have open is signed out.</p>',
      [{ label: "Cancel" }, { label: "Reset password", cls: "", on: function () {
        if (LIVE) return userCall("POST", path + "/reset", undefined, "The password was not reset", function (r) { tempPassword(n, "Password reset; all of their sessions were ended.", r.temp_password); });
        u.status = "must_change"; u.note = "temporary password not yet changed";                 // API: POST /admin/users/{name}/reset
        audit("user.reset_password", n); renderUsers();
        setTimeout(function () { tempPassword(n, "Password reset; all of their sessions were ended."); }, 0);
      } }]);
    if (act === "lock") {
      var locking = u.status !== "locked";
      if (LIVE) return userCall("POST", path + (locking ? "/lock" : "/unlock"), undefined, locking ? "The account was not locked" : "The account was not unlocked",
        function () { toast(locking ? n + " is locked and signed out" : n + " can sign in again"); });
      u.status = locking ? "locked" : "active"; u.note = locking ? "locked by " + sess.username : "";   // API: POST /admin/users/{name}/lock|unlock
      audit(locking ? "user.lock" : "user.unlock", n); renderUsers();
      return toast(locking ? n + " is locked and signed out" : n + " can sign in again");
    }
    if (act === "role") {
      var role = u.role === "superadmin" ? "tester" : "superadmin";
      if (LIVE) return userCall("POST", path + "/role", { role: role }, "The role did not change",
        function () { toast(n + " is now a " + (role === "superadmin" ? "super-admin" : "tester")); });
      u.role = role;                                                                             // API: POST /admin/users/{name}/role
      audit("user.role", n, "now " + u.role); renderUsers();
      return toast(n + " is now a " + (u.role === "superadmin" ? "super-admin" : "tester"));
    }
    if (act === "delete") return modal("Delete " + n + "?", '<p class="d">The account is removed and signed out. Their ' + fmt(num(u.batches)) + ' batches and results stay, credited to “' + esc(n) + ' (deleted)”.</p>' +
      '<div class="field"><label for="dc">Type the username to confirm</label><input type="text" id="dc" autocapitalize="off" spellcheck="false"></div>',
      [{ label: "Cancel" }, { label: "Delete account", cls: "danger solid", on: function () {
        if ($("dc").value.trim().toLowerCase() !== n) { toast("The username does not match."); return false; }
        if (LIVE) return userCall("DELETE", path, undefined, "The account was not deleted", function () { toast(n + " deleted"); });
        D.users = D.users.filter(function (x) { return x.u !== n; });                            // API: DELETE /admin/users/{name}
        audit("user.delete", n); renderUsers(); toast(n + " deleted");
      } }]);
  });

  // ================================================================== AUDIT & STORAGE
  function renderAudit() {
    var k = $("a-kind").value;
    $("tb-audit").innerHTML = "<thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Details</th></tr></thead><tbody>" +
      (D.audit.filter(function (a) { return !k || a[2].indexOf(k) === 0 || (k === "breaker" && a[1] === "system"); }).map(function (a) {
        return '<tr><td class="num">' + esc(a[0]) + "</td><td>" + (a[1] === "system" ? '<span class="tag">system</span>' : "<b>" + esc(a[1]) + "</b>") + '</td><td class="mono">' + esc(a[2]) + '</td><td class="mono">' + esc(a[3]) + "</td><td>" + esc(a[4]) + "</td></tr>";
      }).join("") || '<tr><td colspan="5" class="hint">Nothing recorded in this period.</td></tr>') + "</tbody>";
    $("st-sub").innerHTML = 'Bucket <span class="mono">' + esc(D.BUCKET) + "</span> · eu-central-1 · separate from the live Rudi data bucket.";
    $("tb-storage").innerHTML = "<thead><tr><th>Prefix</th><th>Holds</th><th class='n'>Size</th><th>Kept</th></tr></thead><tbody>" + D.storage.map(function (s) {
      return '<tr><td class="mono">' + esc(s[0]) + "</td><td>" + esc(s[1]) + '</td><td class="n">' + esc(s[2]) + "</td><td>" + esc(s[3]) + "</td></tr>";
    }).join("") + "</tbody>";
    $("tb-versions").innerHTML = "<thead><tr><th>Component</th><th>Version</th><th>Since</th></tr></thead><tbody>" + D.versions.map(function (v) {
      return "<tr><td>" + esc(v[0]) + '</td><td class="mono">' + esc(v[1]) + "</td><td>" + esc(v[2]) + "</td></tr>";
    }).join("") + "</tbody>";
  }
  $("a-kind").addEventListener("change", function () { if (LIVE) route(); else renderAudit(); });   // live: the API filters

  // ================================================================== render table
  var RENDER = {
    dashboard: function () {
      if (!D.daily.length) return;                                                            // nothing loaded yet
      renderKpis(); renderRunning(); chartOutcomes(); chartLatency(); chartErrorsModels(); renderTables();
      renderBudget(); chartSpend(); renderCostTables();
      var d = slice();
      $("range-note").textContent = dayLabel(d[0].date) + " – " + dayLabel(d[d.length - 1].date) + " · " + fmt(d.reduce(function (s, x) { return s + x.total; }, 0)) + " cases";
    },
    submit: renderSubmit,
    batches: function (arg) {
      $("batch-list").hidden = !!arg; $("batch-detail").hidden = !arg;
      if (arg) renderBatchDetail(arg); else renderBatchList();
    },
    "case": renderCase,
    formats: function () {
      $("s3cmd").textContent = "aws s3 cp my-batch.yaml s3://" + D.BUCKET + "/inbox/" + sess.username + "/my-batch.yaml ^\n  --region eu-central-1 --profile rudi-deployer";
    },
    account: renderAccount,
    engine: renderEngine,
    users: renderUsers,
    audit: renderAudit
  };

  route();
})();
