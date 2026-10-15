// Training Arena — the IMPROVEMENTS pane (every signed-in user). Pick a results log — one the arena
// stored, or a file from this computer — and get three lists: the application errors met during
// the run, Rudi's weak points, and feature requests a coding agent can implement, each tied to a
// skill of the catalogue. The analysis runs on the user's own Claude (the local generator, on
// their subscription) when it is there, otherwise on the arena's model in AWS (meetrudi-arena-
// extractor). The report is saved next to its results in S3, as HTML, Markdown and JSON.
(function () {
  "use strict";
  var GEN = window.ArenaGen, C = null;
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function on(id, ev, fn) { var el = $(id); if (el) el.addEventListener(ev, fn); }
  function each(sel, fn) { Array.prototype.forEach.call(document.querySelectorAll(sel), fn); }
  function visible() { return $("p-improve") && !$("p-improve").hidden; }
  function kb(n) { return n < 1024 * 1024 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1048576).toFixed(1) + " MB"; }

  var STEPS = ["Results", "Analyse", "Report"];
  var I = { step: 1, reached: 1, from: "arena", batch: "", files: [], upload: null, local: { state: "unknown", health: null },
            engine: null, model: "sonnet", running: false, steps: [], report: null, record: null, error: null, list: [], starting: false };

  // The final results of a batch; while it still runs, its latest partial-results snapshot. The
  // prototype's sample batch has no file list, so its names are made the way the batch pane does.
  function resultsFiles(b) {
    var fin = (b.files || []).filter(function (f) { return f.indexOf("__results__") >= 0; });
    if (fin.length) return fin;
    var snaps = (b.snapshots || []).filter(function (s) { return (s.files || []).length; });
    if (snaps.length) return snaps[snaps.length - 1].files.slice();
    if (!C.LIVE && b.sample && b.parts) {
      var out = [];
      for (var i = 1; i <= b.parts; i++) out.push(b.id + "__" + b.name + "__results__p" + String(i).padStart(2, "0") + "of" + String(b.parts).padStart(2, "0") + ".yaml");
      return out;
    }
    return [];
  }
  function isPartial(f) { return f.indexOf("__partial-results-") >= 0; }
  function batchesWithResults() { return C.D.batches.filter(function (b) { return resultsFiles(b).length; }); }
  function source() {
    if (I.from === "arena") {
      if (!I.batch || !I.files.length) return null;
      var b = C.D.batches.filter(function (x) { return x.id === I.batch; })[0];
      return { kind: "arena", batch: I.batch, files: I.files.slice(), name: I.files.length === 1 ? I.files[0] : (b ? b.stem || b.id : I.batch) + " (" + I.files.length + " parts)" };
    }
    return I.upload ? { kind: "file", name: I.upload.name, size: I.upload.size } : null;
  }
  function localReady() { return I.local.state === "ready" && I.local.health && I.local.health.claude_ok === true; }

  // ------------------------------------------------------------------ frame (the Generator's wizard look)
  function go(n, deal) { I.step = n; I.reached = Math.max(I.reached, n); paint(deal !== false); }
  function paint(deal) {
    var src = source();
    $("p-improve").querySelector(".gen-wizard").classList.toggle("wide", I.step === 3 && !!I.report);
    var vals = [src ? src.name : "not chosen", I.engine ? (I.engine === "local" ? "your Claude" : "the arena's model") : "—",
                I.report ? I.report.report.feature_requests.length + " feature requests" : I.running ? "analysing…" : "—"];
    $("imp-stepper").innerHTML = STEPS.map(function (lb, i) {
      var n = i + 1, cls = n === I.step ? "cur" : n <= I.reached ? "done" : "todo";
      return '<li><button type="button" class="gen-st ' + cls + '" data-imp-go="' + n + '"' + (cls === "done" ? "" : ' tabindex="-1" aria-disabled="true"') + ">" +
        '<span class="no">' + (cls === "done" ? "✓" : n) + '</span><span class="lb">' + lb + '</span><span class="val">' + esc(vals[i]) + "</span></button></li>";
    }).join("");
    $("imp-track").style.width = Math.max(4, ((I.report ? 3 : I.step - 1) / 3) * 100) + "%";
    var k = I.step - 1, past = "";
    for (var i = 1; i <= k; i++) {
      past += '<button type="button" class="gen-strip" data-imp-go="' + i + '" style="width:calc(100% - ' + (k - i + 1) * 1.4 + 'rem)"><span class="no">✓</span><b>' + STEPS[i - 1] +
        '</b><span class="v">' + esc(vals[i - 1]) + '</span><span class="ch">Change</span></button>';
    }
    $("imp-past").innerHTML = past;
    $("imp-edges").innerHTML = new Array(3 - I.step + 1).join("<i></i>");
    var f = $("imp-front");
    f.innerHTML = [card1, card2, card3][I.step - 1]();
    [bind1, bind2, bind3][I.step - 1]();
    if (deal) { f.classList.remove("deal"); void f.offsetWidth; f.classList.add("deal"); }
    renderPill();
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-imp-go]");
    if (b && !b.hasAttribute("aria-disabled") && !I.running) { var n = +b.getAttribute("data-imp-go"); if (n <= I.reached && n !== I.step) go(n); }
  });
  function head(n, title, sub) {
    return '<div class="gen-head"><div class="gen-kick">Step ' + n + " of 3 · " + STEPS[n - 1] + "</div><h2>" + title + "</h2>" + (sub ? "<p>" + sub + "</p>" : "") + "</div>";
  }

  // ------------------------------------------------------------------ the local Claude
  function checkLocal() {
    return GEN.status().then(function (s) { I.local = s; renderPill(); if (I.step === 2 && visible()) paint(false); return s; });
  }
  function renderPill() {
    var p = $("imp-conn");
    if (!p) return;
    var t = I.starting ? ["warn", "Starting your Claude…"] : localReady() ? ["", "Your Claude is ready"]
      : I.local.state === "ready" ? ["warn", "Your Claude: checking…"] : I.local.state === "unpaired" ? ["warn", "Your Claude: connect"]
      : ["off", "Your Claude is not running"];
    p.innerHTML = '<span class="gen-dot ' + t[0] + '"></span>' + esc(t[1]);
  }
  function startLocal() {
    if (I.starting) return;
    I.starting = true; renderPill(); if (I.step === 2) paint(false);
    C.toast("Starting your Claude…", "If your browser asks to open Python, allow it (tick Always allow).");
    GEN.start(30000).then(function () { I.starting = false; return checkLocal(); }).then(function () {
      if (localReady()) { I.engine = "local"; C.toast("Your Claude is ready."); }
      if (I.step === 2) paint(false);
    }, function () {
      I.starting = false; checkLocal();
      C.modal("Your Claude did not start", '<p class="d">The Improvement Extractor can use Claude Code on this computer, on your own Claude subscription. ' +
        "To set that up once: in File Explorer open the repository folder <span class=\"mono\">services\\training-arena\\generator</span> and double-click " +
        "<b>Install-Generator.cmd</b>. Claude Code must be installed and signed in.</p><p class=\"d\" style=\"margin-top:.6rem\">Until then, the arena's own model does the analysis.</p>", [{ label: "Close" }]);
    });
  }

  // ------------------------------------------------------------------ step 1 · results
  function card1() {
    var bs = batchesWithResults(), b = bs.filter(function (x) { return x.id === I.batch; })[0];
    var arena = '<div class="field"><label for="imp-batch">Batch</label><select id="imp-batch"><option value="">Choose a batch…</option>' +
      bs.map(function (x) { return '<option value="' + esc(x.id) + '"' + (x.id === I.batch ? " selected" : "") + ">" + esc(x.id + " · " + (x.name || "unnamed") + " · " + (x.finished || x.submitted || "")) + "</option>"; }).join("") +
      "</select>" + (bs.length ? "" : '<span class="hint">No batch has results yet.</span>') + "</div>" +
      (b ? '<div class="field"><span class="lab">Results files</span>' + resultsFiles(b).map(function (f) {
        return '<label class="checkline"><input type="checkbox" data-imp-file="' + esc(f) + '"' + (I.files.indexOf(f) >= 0 ? " checked" : "") + '> <span class="mono">' + esc(f) + "</span>" +
          (isPartial(f) ? ' <span class="hint">· snapshot taken while the batch was running</span>' : "") + "</label>";
      }).join("") + '<span class="hint">' + esc(b.total + " cases · " + b.c.success + " success · " + (b.c.failed + b.c.violation) + " failed or violated · " + b.c.error + " errors") + "</span></div>" : "");
    var local = '<div class="field"><label for="imp-file">Results file</label><input type="file" id="imp-file" accept=".yaml,.yml">' +
      (I.upload ? '<span class="hint">' + esc(I.upload.name + " · " + kb(I.upload.size) + " · " + I.upload.cases + " cases") + "</span>" : '<span class="hint">A Training Arena results file: …__results__p01ofNN.yaml</span>') + "</div>";
    var ok = !!source() && (I.from === "file" || I.files.length);
    return head(1, "Which run should be analysed?", "A results log the arena stored, or one from this computer.") +
      '<div class="seg" role="group" aria-label="Where the results are"><button type="button" data-imp-from="arena" class="' + (I.from === "arena" ? "on" : "") + '">From the arena</button>' +
      '<button type="button" data-imp-from="file" class="' + (I.from === "file" ? "on" : "") + '">From this computer</button></div>' +
      (I.from === "arena" ? arena : local) +
      '<div class="gen-foot"><span class="hint">' + (ok ? "" : I.from === "arena" ? "Choose a batch and at least one results file." : "Choose a results file.") + '</span><button type="button" class="btn pink" id="imp-next1"' + (ok ? "" : " disabled") + ">Next: analyse →</button></div>";
  }
  function bind1() {
    each("[data-imp-from]", function (b) { b.addEventListener("click", function () { I.from = b.getAttribute("data-imp-from"); paint(false); }); });
    on("imp-batch", "change", function () {
      I.batch = this.value;
      var b = C.D.batches.filter(function (x) { return x.id === I.batch; })[0];
      I.files = b ? resultsFiles(b) : [];
      paint(false);
    });
    each("[data-imp-file]", function (x) {
      x.addEventListener("change", function () {
        var f = x.getAttribute("data-imp-file");
        I.files = I.files.filter(function (y) { return y !== f; });
        if (x.checked) I.files.push(f);
        paint(false);
      });
    });
    on("imp-file", "change", function () {
      var f = this.files && this.files[0];
      if (!f) return;
      var r = new FileReader();
      r.onload = function () {
        var text = String(r.result || "");
        if (text.indexOf("rudi-arena/results@1") < 0 && text.indexOf("\ncase:") < 0) {
          C.toast("Not a results file", "Choose a Training Arena results file (…__results__p01ofNN.yaml)."); return;
        }
        I.upload = { name: f.name, size: f.size, text: text, blob: f, cases: (text.match(/\ncase:/g) || []).length };
        paint(false);
      };
      r.readAsText(f);
    });
    on("imp-next1", "click", function () { go(2); checkLocal().then(function () { if (!I.engine) I.engine = localReady() ? "local" : "arena"; paint(false); }); });
  }

  // ------------------------------------------------------------------ step 2 · analyse
  function card2() {
    if (I.running) {
      return '<div class="gen-busy"><div class="gen-orb" style="--p:' + Math.min(92, 18 + I.steps.length * 22) + '%"></div><div><h2>' + esc(I.engine === "local" ? "Your Claude is reading the run" : "The arena's model is reading the run") + '</h2><p class="hint" style="margin-top:.3rem">Usually under two minutes.</p></div>' +
        '<ul class="gen-log">' + I.steps.map(function (s) {
          return '<li class="' + esc(s.state) + '"><span class="ic">' + ({ done: "✓", fix: "!", run: "", todo: "·" }[s.state] || "·") + "</span><span>" + esc(s.text) + "</span>" + (s.t ? '<span class="t">' + esc(s.t) + "</span>" : "") + "</li>";
        }).join("") + "</ul></div>";
    }
    var lr = localReady(), st = I.local.state, h = I.local.health || {};
    var localHint = lr ? "Claude Code on this computer, on your own Claude subscription. No API cost."
      : I.starting ? "Starting…"
      : st === "ready" && h.claude_ok === false ? "Running, but Claude Code does not answer: " + (h.claude_error || "is it installed and signed in?")
      : st === "ready" ? "Checking that Claude Code answers…"
      : st === "unpaired" ? "Running on this computer, not yet connected to this tab." : "Not running on this computer.";
    var action = lr || I.starting || st === "ready" ? "" : st === "unpaired" ? "Click to connect it" : "Click to start it";
    return head(2, "Who reads the run?", "The results go to the model with the skills catalogue; the answer comes back as three lists.") +
      (I.error ? '<div class="banner bad">' + esc(I.error) + "</div>" : "") +
      '<div class="gen-tiles">' +
      '<button type="button" class="gen-tile' + (I.engine === "local" ? " on" : "") + (lr ? "" : " off") + '" data-imp-engine="local"' + (I.starting ? " disabled" : "") + '><b>Your Claude</b><span class="hint">' + esc(localHint) + "</span>" +
      (action ? '<span class="hint imp-act">' + action + " →</span>" : "") + "</button>" +
      '<button type="button" class="gen-tile' + (I.engine === "arena" ? " on" : "") + '" data-imp-engine="arena"' + (C.LIVE ? "" : " disabled") + '><b>The arena\'s model</b><span class="hint">' +
      (C.LIVE ? "Runs in AWS on the arena's own EU model. About €0.01 per run, counted in the arena's month budget." : "Needs the live arena — this console runs as a prototype.") + "</span></button></div>" +
      (I.engine === "local" ? '<div class="field"><span class="lab">Claude model</span><div class="seg" role="group" aria-label="Claude model"><button type="button" data-imp-model="sonnet" class="' + (I.model === "sonnet" ? "on" : "") + '">Sonnet · faster</button><button type="button" data-imp-model="opus" class="' + (I.model === "opus" ? "on" : "") + '">Opus · more thorough</button></div></div>' : "") +
      '<p class="hint">Results logs hold synthetic conversations only. ' + (I.engine === "local" ? "They go to Anthropic through your Claude subscription, with e-mail addresses, phone numbers and IBANs masked first (project decisions 21 and 22)." : "They stay with the arena's EU providers.") + "</p>" +
      '<div class="gen-foot"><button type="button" class="btn plain" id="imp-back2">← Back</button><span class="hint"></span><button type="button" class="btn pink" id="imp-run"' + (I.engine ? "" : " disabled") + ">Analyse →</button></div>";
  }
  function bind2() {
    each("[data-imp-engine]", function (b) {
      b.addEventListener("click", function () {
        var eng = b.getAttribute("data-imp-engine");
        if (eng === "local" && !localReady()) { if (I.local.state === "ready") checkLocal(); else startLocal(); return; }
        I.engine = eng; paint(false);
      });
    });
    each("[data-imp-model]", function (b) { b.addEventListener("click", function () { I.model = b.getAttribute("data-imp-model"); paint(false); }); });
    on("imp-back2", "click", function () { go(1); });
    on("imp-run", "click", run);
  }
  function setSteps(steps) { I.steps = steps || []; if (I.step === 2 && visible()) paint(false); }
  function fail(e, rec) {
    I.running = false; I.error = (e && (e.detail || e.message)) || "The analysis did not finish."; setSteps([]);
    if (rec && I.engine === "local") C.API.call("POST", "/analyses/" + rec.id + "/result", { error: I.error }).catch(function () { return null; });
    refreshList();
  }
  function done(rec, data) {
    I.running = false; I.record = rec; I.report = data; I.error = null; I.reached = 3; go(3); refreshList();
  }
  function fetchText(url) { return fetch(url).then(function (r) { if (!r.ok) throw new Error("download"); return r.text(); }); }
  function sourceText() {
    var s = source();
    if (s.kind === "file") return Promise.resolve(I.upload.text);
    if (!C.LIVE) return Promise.all(s.files.map(function (f) { return fetchText("samples/" + f); })).then(function (t) { return t.join("\n"); });
    return Promise.all(s.files.map(function (f) {
      return C.API.call("GET", "/batches/" + encodeURIComponent(s.batch) + "/files/" + encodeURIComponent(f)).then(function (r) { return fetchText(r.url); });
    })).then(function (t) { return t.join("\n"); });
  }
  function run() {
    I.running = true; I.error = null; I.report = null; setSteps([{ text: "Preparing the results", state: "run" }]);
    var s = source();
    if (I.engine === "local") {
      var rec = null, text = null;
      var start = C.LIVE ? C.API.call("POST", "/analyses", { engine: "local", source: s.kind === "arena" ? { batch: s.batch, files: s.files } : { name: s.name } })
        .then(function (r) { rec = r.analysis; }) : Promise.resolve(rec = { id: "IM-" + new Date().toISOString().slice(0, 10).replace(/-/g, "") + "-PROT" });
      start.then(sourceText).then(function (t) {
        text = t;
        var job = GEN.run("/analyze", { text: text, analysis_id: rec.id, model: I.model, source: { name: s.name, batch_id: s.batch || null } },
          function (j) { setSteps(j.steps); });
        return job.promise;
      }).then(function (j) {
        if (j.state !== "done") throw { detail: j.error || "Your Claude did not finish the analysis." };
        var out = j.result;
        if (!C.LIVE) return done({ id: rec.id, file: out.file, engine: "local", engine_label: out.json.meta.engine }, out.json);
        return C.API.call("POST", "/analyses/" + rec.id + "/result", { json: out.json, model: out.model })   // the arena renders the stored page itself
          .then(function (r) { done(r.analysis, out.json); });
      }).catch(function (e) { fail(e, rec); });
      return;
    }
    var created = s.kind === "arena" ? C.API.call("POST", "/analyses", { source: { batch: s.batch, files: s.files } })
      : C.API.call("POST", "/analyses/uploads", { filename: I.upload.name, size: I.upload.size }).then(function (u) {
        return fetch(u.url, { method: "PUT", body: I.upload.blob.slice(0, I.upload.size) }).then(function (res) {
          if (!res.ok) throw { detail: "The upload did not go through (HTTP " + res.status + ")." };
          return C.API.call("POST", "/analyses", { source: { key: u.key } });
        });
      });
    created.then(function (r) {
      var id = r.analysis.id, t0 = Date.now();
      return new Promise(function (resolve, reject) {
        (function poll() {
          C.API.call("GET", "/analyses/" + id).then(function (x) {
            var a = x.analysis;
            setSteps(a.steps && a.steps.length ? a.steps : [{ text: "Waiting for the arena's extractor", state: "run" }]);
            if (a.status === "done") return resolve(a);
            if (a.status === "failed") return reject({ detail: a.error });
            if (Date.now() - t0 > 600000) return reject({ detail: "The analysis is taking longer than ten minutes; it will appear under Earlier analyses when it is done." });
            setTimeout(poll, 2000);
          }, reject);
        })();
      });
    }).then(function (a) {
      return C.API.call("GET", "/analyses/" + a.id + "/download?as=json").then(function (r) { return fetchText(r.url); })
        .then(function (t) { done(a, JSON.parse(t)); });
    }).catch(function (e) { fail(e, null); });
  }

  // ------------------------------------------------------------------ step 3 · report
  function chip(cls, text) { return text ? '<span class="imp-chip ' + esc(cls) + '">' + esc(text) + "</span>" : ""; }
  function cases(ids) { return (ids || []).map(function (i) { return '<span class="mono">' + esc(i) + "</span>"; }).join(", ") || "—"; }
  function card3() {
    if (!I.report) return head(3, "No report yet", "") + '<div class="gen-foot"><button type="button" class="btn plain" id="imp-back3">← Back</button></div>';
    var r = I.report.report, m = I.report.meta || {}, rec = I.record || {};
    var errs = r.app_errors.map(function (x) {
      return '<div class="imp-card ' + esc(x.severity) + '"><b>' + esc(x.title) + "</b>" + '<div class="imp-chips">' + chip(x.severity, x.severity) + chip("", x.count ? x.count + "×" : "") + chip("", x.recorded_only ? "as recorded" : "") + "</div>" +
        '<p>' + esc(x.detail) + "</p>" + (x.probable_cause ? '<p class="hint">Probable cause: ' + esc(x.probable_cause) + "</p>" : "") + (x.where ? '<p class="hint">Where: ' + esc(x.where) + "</p>" : "") +
        '<p class="hint">Cases: ' + cases(x.cases) + "</p></div>";
    }).join("") || '<p class="hint">None in this run.</p>';
    var defs = r.deficiencies.map(function (d) {
      return '<div class="imp-card ' + esc(d.severity) + '"><b>' + esc(d.title) + '</b><div class="imp-chips">' + chip(d.severity, d.severity) + chip("skill", d.skill) + "</div><p>" + esc(d.description) + "</p>" +
        d.evidence.map(function (v) { return '<blockquote class="imp-q">' + esc(v.case) + ", turn " + esc(v.turn) + ": “" + esc(v.quote) + "”</blockquote>"; }).join("") +
        '<p class="hint">Cases: ' + cases(d.cases) + "</p></div>";
    }).join("") || '<p class="hint">None in this run.</p>';
    var frs = r.feature_requests.map(function (f) {
      return '<div class="imp-card ' + { P1: "high", P2: "medium", P3: "low" }[f.priority] + '"><b>' + esc(f.id + " · " + f.title) + '</b><div class="imp-chips">' + chip(f.priority, f.priority) + chip("skill", f.skill) + "</div>" +
        '<p><span class="hint">Problem.</span> ' + esc(f.problem) + '</p><p><span class="hint">Change.</span> ' + esc(f.change) + "</p>" +
        (f.acceptance_criteria.length ? '<ul class="imp-ul">' + f.acceptance_criteria.map(function (a) { return "<li>" + esc(a) + "</li>"; }).join("") + "</ul>" : "") +
        (f.code_pointers.length ? '<p class="hint">Code: ' + f.code_pointers.map(function (c) { return '<span class="mono">' + esc(c) + "</span>"; }).join(", ") + "</p>" : "") +
        (f.test_case ? '<p class="hint">Arena test: ' + esc(f.test_case) + "</p>" : "") + "</div>";
    }).join("") || '<p class="hint">None — nothing to change.</p>';
    return head(3, "What this run shows", esc(r.summary || "")) +
      '<div class="gen-path">' + esc(rec.report_key ? "s3://" + (C.D.BUCKET || "…") + "/" + rec.report_key : (rec.file || m.report || "")) + "</div>" +
      '<p class="hint">' + esc((m.engine || rec.engine_label || "") + (rec.cost_eur ? " · €" + Number(rec.cost_eur).toFixed(4) : "") + " · skills catalogue " + (m.catalogue || "") + (r.notes && r.notes.length ? " · " + r.notes.join("; ") : "")) + "</p>" +
      '<div class="imp-boxes">' +
      '<section class="imp-box"><h3>Application errors <span class="imp-n">' + r.app_errors.length + "</span></h3>" + errs + "</section>" +
      '<section class="imp-box"><h3>Rudi\'s weak points <span class="imp-n">' + r.deficiencies.length + "</span></h3>" + defs + "</section>" +
      '<section class="imp-box"><h3>Feature requests <span class="imp-n">' + r.feature_requests.length + "</span></h3>" + frs + "</section></div>" +
      '<div class="gen-foot"><button type="button" class="btn plain" id="imp-again">Analyse another run</button><span class="hint"></span>' +
      '<button type="button" class="btn ghost" id="imp-copy">Copy feature requests (Markdown)</button><button type="button" class="btn pink" id="imp-dl"' + (rec.report_key ? "" : " disabled") + ">Download report</button></div>";
  }
  function markdownOf(data) {
    var r = data.report, m = data.meta || {}, L = ["# Feature requests from Training Arena run " + (m.batch_id || m.source || ""), "", "Source: `" + (m.source || "") + "` · " + (m.engine || ""), "", r.summary || "", ""];
    r.feature_requests.forEach(function (f) {
      L.push("## " + f.id + " · " + f.priority + " — " + f.title, "", "**Skill:** `" + f.skill + "`", "", "**Problem.** " + f.problem, "", "**Change.** " + f.change, "");
      if (f.acceptance_criteria.length) L.push("**Acceptance criteria**", f.acceptance_criteria.map(function (a) { return "- " + a; }).join("\n"), "");
      if (f.code_pointers.length) L.push("**Code pointers:** " + f.code_pointers.map(function (c) { return "`" + c + "`"; }).join(", "), "");
      if (f.test_case) L.push("**Arena test case.** " + f.test_case, "");
    });
    return L.join("\n");
  }
  function bind3() {
    on("imp-back3", "click", function () { go(2); });
    on("imp-again", "click", function () { I.report = null; I.record = null; I.steps = []; I.reached = 1; go(1); });
    on("imp-copy", "click", function () {
      var md = markdownOf(I.report);
      try { navigator.clipboard.writeText(md).then(function () { C.toast("Copied " + I.report.report.feature_requests.length + " feature requests as Markdown."); }, function () { C.toast("Could not copy", "Download the report instead."); }); }
      catch (e) { C.toast("Could not copy", "Download the report instead."); }
    });
    on("imp-dl", "click", function () { download(I.record.id, "html"); });
  }
  function download(id, as) {
    C.API.call("GET", "/analyses/" + id + "/download?as=" + as).then(function (r) { location.href = r.url; }, function (e) { C.fail(e, "No download"); });
  }

  // ------------------------------------------------------------------ earlier analyses
  function refreshList() {
    if (!C.LIVE) { $("imp-list").innerHTML = '<p class="hint">Saved analyses appear here in the live arena.</p>'; return; }
    C.API.call("GET", "/analyses").then(function (r) {
      I.list = r.analyses || [];
      $("imp-list").innerHTML = I.list.length ? '<div class="tblwrap"><table class="t"><thead><tr><th>When</th><th>Run</th><th>By</th><th>Read by</th><th>Found</th><th>Status</th><th></th></tr></thead><tbody>' +
        I.list.map(function (a) {
          var c = a.counts || {};
          return "<tr><td class=\"num\" style=\"white-space:nowrap\">" + esc(a.created) + '</td><td style="white-space:nowrap" title="' + esc(a.source || "") + '"><span class="mono">' + esc(a.batch_id || a.source || "") + "</span></td><td>" + esc(a.by) + "</td><td>" + esc(a.engine === "local" ? "their Claude" : "arena model") +
            "</td><td>" + (a.status === "done" ? esc(c.errors + " errors · " + c.weaknesses + " weak points · " + c.requests + " requests") : "") + "</td><td>" + esc(a.status === "failed" ? "failed: " + (a.error || "") : a.status) +
            "</td><td style=\"white-space:nowrap\">" + (a.status === "done" ? '<button type="button" class="btn ghost xs" data-imp-open="' + esc(a.id) + '">Open</button> <button type="button" class="btn ghost xs" data-imp-dl="' + esc(a.id) + '">HTML</button> <button type="button" class="btn ghost xs" data-imp-md="' + esc(a.id) + '">Markdown</button>' : "") + "</td></tr>";
        }).join("") + "</tbody></table></div>" : '<p class="hint">No analyses yet.</p>';
      each("[data-imp-dl]", function (b) { b.addEventListener("click", function () { download(b.getAttribute("data-imp-dl"), "html"); }); });
      each("[data-imp-md]", function (b) { b.addEventListener("click", function () { download(b.getAttribute("data-imp-md"), "md"); }); });
      each("[data-imp-open]", function (b) {
        b.addEventListener("click", function () {
          var a = I.list.filter(function (x) { return x.id === b.getAttribute("data-imp-open"); })[0];
          C.API.call("GET", "/analyses/" + a.id + "/download?as=json").then(function (r) { return fetchText(r.url); })
            .then(function (t) { done(a, JSON.parse(t)); window.scrollTo(0, 0); }, function (e) { C.fail(e, "Could not open it"); });
        });
      });
    }, function (e) { $("imp-list").innerHTML = '<p class="hint">' + esc((e && e.detail) || "Could not load the list.") + "</p>"; });
  }

  var bound = false;
  window.ArenaImprovePane = {
    render: function (ctx) {
      C = ctx;
      if (!bound) { bound = true; on("imp-conn", "click", function () { if (localReady()) C.toast("Your Claude is ready", "Claude Code on this computer, on your subscription."); else startLocal(); }); }
      paint(false);
      refreshList();
      checkLocal();
    }
  };
})();
