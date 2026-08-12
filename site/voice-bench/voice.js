/* MEET_RUDI — voice bench client.
 *
 * The browser is a microphone and a speaker. It captures one utterance, ships it to
 * meetrudi-voice-bench, plays back whatever Rudi says, and listens again. All conversation
 * state lives server-side in S3, so a reload loses nothing but the on-screen transcript.
 *
 * The number this page exists to measure is the REPLY GAP: from the moment the speaker stops
 * to the moment Rudi's voice starts. Everything else on screen is a component of that.
 */
(function () {
  "use strict";

  var API = window.VOICE_BENCH_API || "";
  var DEFAULTS = window.VOICE_BENCH_DEFAULTS || {};
  var VAD = window.VOICE_BENCH_VAD || {};

  var SILENCE_MS = VAD.silenceMs || 900;
  var MIN_SPEECH_MS = VAD.minSpeechMs || 350;
  var MAX_TURN_MS = VAD.maxTurnMs || 30000;
  var THRESHOLD_X = VAD.threshold || 2.6;
  var FLOOR_MIN = VAD.floorMin || 0.006;
  var BARGE_MS = 400;

  // ------------------------------------------------------------------ element handles
  var $ = function (id) { return document.getElementById(id); };
  var els = {
    dot: $("dot"), stateLabel: $("stateLabel"), stateHint: $("stateHint"),
    log: $("log"), micBar: $("micBar"), banner: $("banner"), callIdNote: $("callIdNote"),
    btnStart: $("btnStart"), btnHang: $("btnHang"), btnPush: $("btnPush"),
    cfgJson: $("cfgJson"),
    name: $("cfgName"), topic: $("cfgTopic"), lang: $("cfgLang"), voice: $("cfgVoice"),
    phase: $("cfgPhase"), max: $("cfgMax"), notes: $("cfgNotes"),
    store: $("cfgStore"), barge: $("cfgBarge"),
    gap: $("cfgGap"), gapOut: $("cfgGapOut"),
    avgGap: $("avgGap"), avgAsr: $("avgAsr"), avgLlm: $("avgLlm"),
    avgTts: $("avgTts"), avgNet: $("avgNet"), nTurns: $("nTurns")
  };

  // ------------------------------------------------------------------ runtime state
  var callId = null;
  var stream = null, audioCtx = null, analyser = null, buf = null;
  var recorder = null, chunks = [], recMime = "audio/webm";
  var noiseFloor = 0.01, threshold = 0.03;
  var monitorTimer = null, player = null;
  var listening = false, hadSpeech = false, speechMs = 0;
  var lastVoiceAt = 0, turnStartedAt = 0, bargeMs = 0;
  var stoppedSpeakingAt = 0, pushToTalk = false;
  var busy = false, live = false, pendingEnd = false;
  var stats = { gap: [], asr: [], llm: [], tts: [], net: [] };

  // ------------------------------------------------------------------ config panel
  function readConfig() {
    return {
      language: els.lang.value,
      user_name: els.name.value.trim(),
      topic: els.topic.value.trim(),
      voice: els.voice.value.trim() || (DEFAULTS.voice || "en"),
      start_phase: els.phase.value,
      max_minutes: parseInt(els.max.value, 10) || 12,
      store_audio: els.store.checked,
      sentence_gap_ms: parseInt(els.gap.value, 10),
      notes: els.notes.value.trim()
    };
  }

  function paintConfig() {
    var gap = parseInt(els.gap.value, 10);
    els.gapOut.innerHTML = gap === 0
      ? "0ms &middot; sentences run together, as Piper leaves them"
      : gap + "ms &middot; questions rest " + Math.round(gap * 1.5) + "ms";
    els.cfgJson.textContent = JSON.stringify(readConfig(), null, 2);
  }

  function fillVoices() {
    var list = window.VOICE_BENCH_VOICES || [["en", "default"]];
    list.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v[0];
      o.textContent = v[1] + " — " + v[0];
      els.voice.appendChild(o);
    });
  }

  function applyDefaults() {
    fillVoices();
    if (DEFAULTS.user_name) els.name.value = DEFAULTS.user_name;
    if (DEFAULTS.topic) els.topic.value = DEFAULTS.topic;
    if (DEFAULTS.language) els.lang.value = DEFAULTS.language;
    if (DEFAULTS.voice) els.voice.value = DEFAULTS.voice;
    if (DEFAULTS.start_phase) els.phase.value = DEFAULTS.start_phase;
    if (DEFAULTS.max_minutes) els.max.value = DEFAULTS.max_minutes;
    if (DEFAULTS.notes) els.notes.value = DEFAULTS.notes;
    if (typeof DEFAULTS.store_audio === "boolean") els.store.checked = DEFAULTS.store_audio;
    if (DEFAULTS.sentence_gap_ms !== undefined) els.gap.value = DEFAULTS.sentence_gap_ms;
    paintConfig();
  }

  ["input", "change"].forEach(function (ev) {
    [els.name, els.topic, els.lang, els.voice, els.phase, els.max, els.notes, els.store, els.gap]
      .forEach(function (el) { el.addEventListener(ev, paintConfig); });
  });

  // ------------------------------------------------------------------ ui helpers
  function setState(kind, label, hint) {
    els.dot.className = "dot " + (kind || "");
    els.stateLabel.textContent = label;
    if (hint !== undefined) els.stateHint.textContent = hint;
  }

  function banner(msg, ok) {
    if (!msg) { els.banner.classList.add("hidden"); return; }
    els.banner.className = "banner " + (ok ? "ok" : "err");
    els.banner.textContent = msg;
  }

  function addTurn(who, text, chips) {
    var wrap = document.createElement("div");
    wrap.className = "turn " + who;

    var label = document.createElement("div");
    label.className = "who";
    label.textContent = who === "rudi" ? "Rudi" : (who === "user" ? "You" : "Bench");
    wrap.appendChild(label);

    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);

    if (chips && chips.length) {
      var row = document.createElement("div");
      row.className = "times";
      chips.forEach(function (c) {
        var s = document.createElement("span");
        s.className = "t" + (c.hero ? " hero" : "") + (c.warn ? " warn" : "");
        s.textContent = c.text;
        row.appendChild(s);
      });
      wrap.appendChild(row);
    }
    els.log.appendChild(wrap);
    wrap.scrollIntoView({ block: "end", behavior: "smooth" });
    return wrap;
  }

  function noteTtsFailure(data) {
    if (!data || !data.tts_error) return;
    banner("Rudi has no voice this turn — " + data.tts_error);
    addTurn("sys", "Text-only turn: speech synthesis failed. The conversation itself still works.");
  }

  function mean(a) {
    if (!a.length) return null;
    return Math.round(a.reduce(function (x, y) { return x + y; }, 0) / a.length);
  }

  function paintStats() {
    var f = function (v) { return v === null ? "–" : (v >= 1000 ? (v / 1000).toFixed(2) + "s" : v + "ms"); };
    els.avgGap.textContent = f(mean(stats.gap));
    els.avgAsr.textContent = f(mean(stats.asr));
    els.avgLlm.textContent = f(mean(stats.llm));
    els.avgTts.textContent = f(mean(stats.tts));
    els.avgNet.textContent = f(mean(stats.net));
    els.nTurns.textContent = String(stats.gap.length);
  }

  // ------------------------------------------------------------------ audio plumbing
  function rms() {
    if (!analyser) return 0;
    analyser.getByteTimeDomainData(buf);
    var sum = 0;
    for (var i = 0; i < buf.length; i++) {
      var v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / buf.length);
  }

  function pickMime() {
    var options = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    for (var i = 0; i < options.length; i++) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported(options[i])) return options[i];
    }
    return "";
  }

  async function openMic() {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") await audioCtx.resume();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    buf = new Uint8Array(analyser.fftSize);
    audioCtx.createMediaStreamSource(stream).connect(analyser);
    recMime = pickMime();
  }

  function calibrate() {
    return new Promise(function (resolve) {
      var samples = [], t0 = Date.now();
      var iv = setInterval(function () {
        samples.push(rms());
        if (Date.now() - t0 > 700) {
          clearInterval(iv);
          samples.sort(function (a, b) { return a - b; });
          noiseFloor = Math.max(FLOOR_MIN, samples[Math.floor(samples.length / 2)] || FLOOR_MIN);
          threshold = noiseFloor * THRESHOLD_X;
          resolve();
        }
      }, 40);
    });
  }

  function startMonitor() {
    if (monitorTimer) return;
    monitorTimer = setInterval(function () {
      var level = rms();
      els.micBar.style.width = Math.min(100, Math.round((level / (threshold * 2.2)) * 100)) + "%";

      // barge-in while Rudi is speaking
      if (player && !player.paused && els.barge.checked) {
        bargeMs = level > threshold ? bargeMs + 60 : 0;
        if (bargeMs >= BARGE_MS) {
          bargeMs = 0;
          try { player.pause(); } catch (e) { /* already gone */ }
          addTurn("sys", "You interrupted — Rudi stopped talking.");
          onPlaybackDone();
        }
        return;
      }

      if (!listening || pushToTalk) return;

      var now = Date.now();
      if (level > threshold) {
        if (!hadSpeech) hadSpeech = true;
        speechMs += 60;
        lastVoiceAt = now;
      }
      if (hadSpeech && speechMs >= MIN_SPEECH_MS && (now - lastVoiceAt) >= SILENCE_MS) {
        stopListening("silence");
      } else if (hadSpeech && (now - turnStartedAt) >= MAX_TURN_MS) {
        stopListening("max-length");
      }
    }, 60);
  }

  function startListening() {
    if (!live || busy || listening) return;
    chunks = [];
    hadSpeech = false;
    speechMs = 0;
    lastVoiceAt = Date.now();
    turnStartedAt = Date.now();

    try {
      recorder = recMime ? new MediaRecorder(stream, { mimeType: recMime })
                         : new MediaRecorder(stream);
    } catch (e) {
      banner("This browser can't record audio: " + e.message);
      setState("error", "Blocked", "MediaRecorder unavailable");
      return;
    }
    recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
    recorder.onstop = onRecorderStop;
    recorder.start();
    listening = true;
    setState("listening", "Listening", pushToTalk ? "Hold the button and speak" : "Speak — I'll stop when you pause");
  }

  function stopListening(reason) {
    if (!listening) return;
    listening = false;
    stoppedSpeakingAt = Date.now();
    els.micBar.style.width = "0%";
    try { if (recorder && recorder.state !== "inactive") recorder.stop(); } catch (e) { /* noop */ }
    if (reason === "max-length") addTurn("sys", "That turn hit the 30-second cap and was sent as-is.");
  }

  function onRecorderStop() {
    var blob = new Blob(chunks, { type: recMime || "audio/webm" });
    chunks = [];
    if (!hadSpeech || blob.size < 1200) {   // nothing but room tone — don't spend a turn on it
      if (live) startListening();
      return;
    }
    sendTurn(blob);
  }

  function blobToBase64(blob) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onloadend = function () { resolve(String(r.result).split(",")[1] || ""); };
      r.onerror = reject;
      r.readAsDataURL(blob);
    });
  }

  function play(b64, mime) {
    return new Promise(function (resolve) {
      if (!b64) { resolve(0); return; }
      var bin = atob(b64), bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      var url = URL.createObjectURL(new Blob([bytes], { type: mime || "audio/wav" }));

      player = new Audio(url);
      var started = 0;
      player.onplaying = function () { if (!started) { started = Date.now(); resolve(started); } };
      player.onended = function () { URL.revokeObjectURL(url); onPlaybackDone(); };
      player.onerror = function () { URL.revokeObjectURL(url); resolve(0); onPlaybackDone(); };
      player.play().catch(function () { resolve(0); onPlaybackDone(); });
    });
  }

  function onPlaybackDone() {
    if (!live) return;
    // The engine decided the call is over — let Rudi finish his goodbye, THEN hang up.
    if (pendingEnd) { pendingEnd = false; hangUp("engine-ended"); return; }
    busy = false;
    setState("listening", "Listening", "Your turn");
    startListening();
  }

  // ------------------------------------------------------------------ transport
  async function post(payload) {
    var res = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    return res.json();
  }

  function chipsFor(data, gap, netMs) {
    var t = data.timings || {};
    var chips = [];
    if (gap) chips.push({ text: "gap " + (gap / 1000).toFixed(2) + "s", hero: true, warn: gap > 3000 });
    if (t.asr_ms) chips.push({ text: "asr " + t.asr_ms + "ms" });
    if (t.llm_ms) chips.push({ text: "llm " + t.llm_ms + "ms" });
    if (t.tts_ms) chips.push({ text: "tts " + t.tts_ms + "ms" });
    if (netMs > 0) chips.push({ text: "net " + netMs + "ms" });
    if (data.phase) chips.push({ text: "phase " + data.phase });
    return chips;
  }

  async function sendTurn(blob) {
    busy = true;
    setState("thinking", "Thinking", "Transcribing and composing a reply");
    var b64;
    try {
      b64 = await blobToBase64(blob);
    } catch (e) {
      banner("Could not read the recording: " + e.message);
      busy = false; startListening(); return;
    }

    var t0 = Date.now(), data;
    try {
      data = await post({ action: "turn", call_id: callId, audio_b64: b64, audio_mime: blob.type });
    } catch (e) {
      banner("Network error: " + e.message);
      setState("error", "Network error", "Check the Function URL and CORS origin");
      busy = false;
      return;
    }
    var roundtrip = Date.now() - t0;

    if (!data.ok) {
      banner(data.error === "rate_limited"
        ? "All models are rate-limited right now — wait a moment and keep talking."
        : ("Server error: " + (data.error || "unknown")));
      if (data.reply) addTurn("sys", data.reply);
      busy = false;
      startListening();
      return;
    }
    banner(null);

    if (data.empty) {
      addTurn("sys", "Didn't catch that — nothing intelligible in that turn.");
      busy = false;
      startListening();
      return;
    }

    var t = data.timings || {};
    var netMs = Math.max(0, roundtrip - (t.server_ms || 0));
    addTurn("user", data.transcript, [{ text: "asr " + (t.asr_ms || 0) + "ms" }]);

    // The round trip is finished, so release the lock before playback: a very short reply can
    // fire `onended` before this function returns, and a still-set `busy` would strand the call.
    busy = false;
    if (data.ended) pendingEnd = true;

    setState("speaking", "Speaking", "Rudi is talking");
    var startedAt = await play(data.audio_b64, data.audio_mime);
    var gap = startedAt ? (startedAt - stoppedSpeakingAt) : 0;

    addTurn("rudi", data.reply || "(no reply)", chipsFor(data, gap, netMs));
    noteTtsFailure(data);
    if (data.ended) addTurn("sys", "Rudi wrapped the call up (phase: " + data.phase + ").");

    if (gap) stats.gap.push(gap);
    if (t.asr_ms) stats.asr.push(t.asr_ms);
    if (t.llm_ms) stats.llm.push(t.llm_ms);
    if (t.tts_ms) stats.tts.push(t.tts_ms);
    if (netMs) stats.net.push(netMs);
    paintStats();

    // Playback's onended restarts listening — or hangs up, when pendingEnd is set.
    if (!data.audio_b64) onPlaybackDone();
  }

  // ------------------------------------------------------------------ call lifecycle
  async function startCall() {
    if (!API || API.indexOf("PASTE_") === 0) {
      banner("Set window.VOICE_BENCH_API in voice-config.js to the deployed Function URL first.");
      return;
    }
    els.btnStart.disabled = true;
    banner(null);
    setState("thinking", "Connecting", "Asking for the microphone");

    try {
      await openMic();
    } catch (e) {
      banner("Microphone blocked: " + e.message + " — the page must be served over HTTPS.");
      setState("error", "No microphone", "Grant access and reload");
      els.btnStart.disabled = false;
      return;
    }

    setState("thinking", "Calibrating", "Measuring the room for a second");
    await calibrate();
    startMonitor();

    setState("thinking", "Dialling", "Rudi is preparing his opening");
    var t0 = Date.now(), data;
    try {
      data = await post({ action: "start", config: readConfig() });
    } catch (e) {
      banner("Network error: " + e.message);
      setState("error", "Network error", "Check the Function URL and CORS origin");
      els.btnStart.disabled = false;
      return;
    }
    var roundtrip = Date.now() - t0;

    if (!data.ok) {
      banner("Could not start the call: " + (data.error || "unknown"));
      setState("error", "Failed to start", "");
      els.btnStart.disabled = false;
      return;
    }

    callId = data.call_id;
    live = true;
    stats = { gap: [], asr: [], llm: [], tts: [], net: [] };
    paintStats();
    els.log.innerHTML = "";
    els.btnHang.classList.remove("hidden");
    els.btnPush.classList.remove("hidden");
    els.callIdNote.textContent = "Call " + callId + " — voice-bench/calls/" + callId + "/ in the data bucket";

    var netMs = Math.max(0, roundtrip - (data.timings ? data.timings.server_ms : 0));
    setState("speaking", "Speaking", "Rudi's opening");
    await play(data.audio_b64, data.audio_mime);
    addTurn("rudi", data.reply, chipsFor(data, 0, netMs));
    noteTtsFailure(data);
    // No audio means no `onended` will ever fire — start listening ourselves.
    if (!data.audio_b64) onPlaybackDone();
  }

  async function hangUp(reason) {
    if (!live) return;
    live = false;
    listening = false;
    try { if (player) player.pause(); } catch (e) { /* noop */ }
    try { if (recorder && recorder.state !== "inactive") recorder.stop(); } catch (e) { /* noop */ }
    if (monitorTimer) { clearInterval(monitorTimer); monitorTimer = null; }
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    els.micBar.style.width = "0%";

    var data = null;
    try {
      data = await post({ action: "end", call_id: callId, reason: reason || "hangup" });
    } catch (e) { /* the record is already durable server-side */ }

    if (data && data.ok && data.manifest) {
      var m = data.manifest, a = m.averages || {};
      addTurn("sys",
        "Call ended after " + (m.duration_s || "?") + "s and " +
        ((m.totals || {}).turns || 0) + " turns.\n" +
        "Server averages — asr " + (a.asr_ms || 0) + "ms, llm " + (a.llm_ms || 0) +
        "ms, tts " + (a.tts_ms || 0) + "ms.\n" +
        "Goal captured: " + ((m.outcome || {}).goal || "none") +
        " (" + ((m.outcome || {}).final_phase || "?") + ")");
      banner("Saved to voice-bench/calls/" + callId + "/ — manifest.json holds the whole call.", true);
    }

    setState("", "Idle", "Call finished");
    els.btnStart.disabled = false;
    els.btnHang.classList.add("hidden");
    els.btnPush.classList.add("hidden");
  }

  // ------------------------------------------------------------------ push-to-talk fallback
  function holdStart() {
    if (!live || busy) return;
    pushToTalk = true;
    if (!listening) startListening();
    hadSpeech = true;
    speechMs = MIN_SPEECH_MS;
    setState("listening", "Listening", "Holding — release to send");
  }

  function holdEnd() {
    if (!pushToTalk) return;
    pushToTalk = false;
    stopListening("push-to-talk");
  }

  els.btnStart.addEventListener("click", startCall);
  els.btnHang.addEventListener("click", function () { hangUp("hangup"); });
  ["mousedown", "touchstart"].forEach(function (e) {
    els.btnPush.addEventListener(e, function (ev) { ev.preventDefault(); holdStart(); });
  });
  ["mouseup", "mouseleave", "touchend", "touchcancel"].forEach(function (e) {
    els.btnPush.addEventListener(e, holdEnd);
  });
  window.addEventListener("beforeunload", function () { if (live) hangUp("page-closed"); });

  applyDefaults();
  if (!API || API.indexOf("PASTE_") === 0) {
    banner("voice-config.js still has the placeholder API URL — deploy the backend and paste the Function URL.");
  }
})();
