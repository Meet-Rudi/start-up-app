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
  var BARGE_MS = VAD.bargeMs || 380;              // sustained speech before we accept a barge-in
  var BARGE_THRESHOLD_X = VAD.bargeThreshold || 1.9;   // multiplier ON TOP of the normal threshold
  var BARGE_GRACE_MS = VAD.bargeGraceMs || 700;   // ignore the first moment of Rudi's audio

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
    adapt: $("cfgAdapt"), adaptOut: $("cfgAdaptOut"),
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
  var lastVoiceAt = 0, turnStartedAt = 0, bargeMs = 0, firstVoiceAt = 0;
  var stoppedSpeakingAt = 0, pushToTalk = false;
  var busy = false, live = false, pendingEnd = false;
  var audioQueue = [], waitingForRest = false, playingSince = 0, playbackStartedAt = 0;
  var stats = { gap: [], asr: [], llm: [], tts: [], net: [] };

  /* ---- pace adaptation -------------------------------------------------------------------
   * The primary signal is CADENCE — characters per second of the transcript, over the time the
   * speaker was actually voicing.
   *
   * Characters rather than words, because word length is not constant across languages: Dutch
   * and German compound words are far longer than English ones, so a words-per-second measure
   * would read an ordinary Flemish speaker as slow. Characters track syllable rate closely
   * enough to be a fair proxy in every language we will serve.
   *
   * Cadence rather than pause behaviour, because it is measurable on EVERY turn. Many people
   * speak in one continuous run on a phone call, so an adaptation that only learned from
   * mid-turn pauses would sit inert for exactly the fluent-but-slow speaker it exists to help.
   *
   * Cadence drives two things: how long we wait before deciding they have finished, and how
   * fast Rudi answers. Someone at 10 characters per second leaves longer gaps between words
   * too, so a fixed 900ms endpoint clips them mid-sentence.
   *
   * Mid-turn pauses are kept as a secondary refinement. When one does occur it is hard evidence
   * of how long this person is willing to leave a gap, so we take whichever signal asks for
   * more room — but nothing depends on them existing.
   *
   * Everything is clamped and drifts slowly. Over-adapting is worse than not adapting: an
   * endpoint that creeps long makes every reply feel sluggish, and a voice that mirrors someone
   * exactly reads as uncanny rather than warm.
   */
  // Unhurried conversational speech, ~155 wpm. This is the anchor: a speaker AT this cadence
  // gets the default wait and Rudi's natural pace. Everything is a deviation from here, so
  // adaptation is a no-op for an average speaker rather than a constant nudge.
  var REF_CPS = 15;
  var adapt = {
    rates: [],             // patient characters/sec, per turn — the primary signal
    pauses: [],            // longest mid-turn pause, when one happens at all
    silenceMs: SILENCE_MS,
    lengthScale: 1.0,
    longestPauseThisTurn: 0,
    silentSince: 0,
    lastRate: null
  };

  function median(a) {
    if (!a.length) return null;
    var s = a.slice().sort(function (x, y) { return x - y; });
    return s[Math.floor(s.length / 2)];
  }

  function updateAdaptation(transcript, speechSeconds) {
    if (!els.adapt.checked) return;

    // Letters only: Whisper's punctuation and digit formatting are its stylistic choices, not
    // something the speaker voiced, and counting them would skew the cadence.
    var chars = (transcript || "").replace(/[^\p{L}\p{N}]/gu, "").length;
    if (chars >= 12 && speechSeconds >= 1.0) {
      adapt.lastRate = chars / speechSeconds;
      adapt.rates.push(adapt.lastRate);
    }
    if (adapt.longestPauseThisTurn > 0) adapt.pauses.push(adapt.longestPauseThisTurn);
    adapt.rates = adapt.rates.slice(-3);
    adapt.pauses = adapt.pauses.slice(-3);

    if (adapt.rates.length < 2) return;          // one turn is not evidence

    var rate = median(adapt.rates);

    // Slower cadence, longer wait — proportionally, then bounded.
    var want = Math.round(SILENCE_MS * (REF_CPS / Math.max(5, rate)));

    // If they have actually demonstrated a long pause, respect it: it beats any inference.
    if (adapt.pauses.length >= 2) {
      want = Math.max(want, Math.round(median(adapt.pauses) * 1.35 + 150));
    }
    want = Math.max(700, Math.min(1800, want));

    // Drift rather than jump, so one odd turn cannot swing the rest of the call.
    adapt.silenceMs += Math.max(-150, Math.min(150, want - adapt.silenceMs));

    // Meet them halfway on pace rather than matching exactly. Relative to REF_CPS, so a
    // speaker at the reference cadence leaves Rudi at exactly 1.00x.
    var full = REF_CPS / Math.max(6, rate);
    adapt.lengthScale = Math.max(0.9, Math.min(1.35,
      Math.round((1 + (full - 1) * 0.5) * 100) / 100));

    paintAdapt();
  }

  function paintAdapt() {
    if (!els.adaptOut) return;
    if (!els.adapt.checked) { els.adaptOut.textContent = "off — fixed " + SILENCE_MS + "ms"; return; }
    var cadence = adapt.rates.length ? median(adapt.rates).toFixed(1) + " ch/s · " : "";
    els.adaptOut.textContent = adapt.rates.length < 2
      ? "learning your cadence… " + cadence + "wait " + adapt.silenceMs + "ms"
      : cadence + "wait " + adapt.silenceMs + "ms · Rudi " + adapt.lengthScale.toFixed(2) + "×";
  }

  function currentSilenceMs() {
    return els.adapt.checked ? adapt.silenceMs : SILENCE_MS;
  }

  function currentLengthScale() {
    return els.adapt.checked ? adapt.lengthScale : null;
  }

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
    paintAdapt();
  }

  ["input", "change"].forEach(function (ev) {
    [els.name, els.topic, els.lang, els.voice, els.phase, els.max, els.notes, els.store, els.gap, els.adapt]
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

      var now = Date.now();

      // While Rudi speaks we are still recording, so an interruption is already on tape by the
      // time we detect it. The bar is set higher here because echo cancellation is imperfect
      // and Rudi's own voice must not trigger this; the grace period ignores the first moment
      // of playback, where leakage is worst.
      if (isPlaying() && els.barge.checked) {
        var grace = (now - playingSince) < BARGE_GRACE_MS;
        bargeMs = (!grace && level > threshold * BARGE_THRESHOLD_X) ? bargeMs + 60 : 0;
        if (bargeMs >= BARGE_MS) {
          bargeMs = 0;
          stopPlayback();
          addTurn("sys", "You interrupted — Rudi stopped and is listening.");
          setState("listening", "Listening", "Go ahead");
          hadSpeech = true;           // keep the audio already captured; it is your turn now
          speechMs = Math.max(speechMs, MIN_SPEECH_MS);
          lastVoiceAt = now;
        }
        return;
      }

      if (!listening || pushToTalk) return;

      if (level > threshold) {
        // They carried on speaking, so whatever silence just elapsed was a mid-thought pause,
        // not the end of their turn. That is exactly the number worth learning from.
        if (hadSpeech && adapt.silentSince) {
          var wasQuiet = now - adapt.silentSince;
          if (wasQuiet > adapt.longestPauseThisTurn) adapt.longestPauseThisTurn = wasQuiet;
        }
        adapt.silentSince = 0;
        if (!hadSpeech) { hadSpeech = true; firstVoiceAt = now; }
        speechMs += 60;
        lastVoiceAt = now;
      } else if (hadSpeech && !adapt.silentSince) {
        adapt.silentSince = now;
      }

      if (hadSpeech && speechMs >= MIN_SPEECH_MS && (now - lastVoiceAt) >= currentSilenceMs()) {
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
    adapt.longestPauseThisTurn = 0;
    adapt.silentSince = 0;
    firstVoiceAt = 0;

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

  function toUrl(b64, mime) {
    var bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return URL.createObjectURL(new Blob([bytes], { type: mime || "audio/wav" }));
  }

  /* Rudi's reply arrives in two pieces: the lead, synthesised immediately, and the rest,
   * fetched while the lead is already playing. Queueing them keeps that seam inaudible. */
  function enqueue(b64, mime) {
    if (!b64) return;
    audioQueue.push(toUrl(b64, mime));
    if (!player || player.ended || player.paused) playNext();
  }

  function playNext() {
    if (!live || !audioQueue.length) {
      if (!audioQueue.length && !waitingForRest) onPlaybackDone();
      return;
    }
    var url = audioQueue.shift();
    player = new Audio(url);
    player.onplaying = function () {
      if (!playbackStartedAt) playbackStartedAt = Date.now();
      playingSince = Date.now();
      // Listen THROUGH Rudi's turn, so an interruption is captured from the moment it starts
      // rather than from the moment we notice it 400ms later.
      if (!listening) startListening();
    };
    player.onended = function () { URL.revokeObjectURL(url); playNext(); };
    player.onerror = function () { URL.revokeObjectURL(url); playNext(); };
    player.play().catch(function () { playNext(); });
  }

  function stopPlayback() {
    try { if (player) player.pause(); } catch (e) { /* already gone */ }
    audioQueue.forEach(function (u) { URL.revokeObjectURL(u); });
    audioQueue = [];
    waitingForRest = false;
    player = null;
    playingSince = 0;
  }

  function isPlaying() {
    return !!(player && !player.paused && !player.ended);
  }

  function onPlaybackDone() {
    if (!live) return;
    // The engine decided the call is over — let Rudi finish his goodbye, THEN hang up.
    if (pendingEnd) { pendingEnd = false; hangUp("engine-ended"); return; }
    busy = false;
    playingSince = 0;
    setState("listening", "Listening", "Your turn");
    if (!listening) startListening();
  }

  // ------------------------------------------------------------------ transport
  /* Resolves with the measured reply gap once Rudi's first sound actually reaches the speaker. */
  function firstSound() {
    return new Promise(function (resolve) {
      var t0 = Date.now();
      (function poll() {
        if (playbackStartedAt) return resolve(playbackStartedAt - stoppedSpeakingAt);
        if (!live || Date.now() - t0 > 15000) return resolve(0);
        setTimeout(poll, 25);
      })();
    });
  }

  /* The remainder of the reply, fetched while the lead is already playing. If the listener
   * interrupts before it arrives we simply drop it — they have moved on. */
  async function fetchRest(text) {
    if (!text) { waitingForRest = false; return; }
    try {
      var data = await post({ action: "speak", call_id: callId, text: text,
                             length_scale: currentLengthScale() });
      if (!live || !isPlaying() && !audioQueue.length && !busy) { waitingForRest = false; return; }
      waitingForRest = false;
      if (data && data.ok) enqueue(data.audio_b64, data.audio_mime);
      else onPlaybackDone();
    } catch (e) {
      waitingForRest = false;
    }
  }

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
      data = await post({ action: "turn", call_id: callId, audio_b64: b64,
                          audio_mime: blob.type, length_scale: currentLengthScale() });
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

    var voicedSeconds = (firstVoiceAt && lastVoiceAt > firstVoiceAt)
      ? (lastVoiceAt - firstVoiceAt) / 1000 : 0;
    updateAdaptation(data.transcript, voicedSeconds);
    var userChips = [{ text: "asr " + (t.asr_ms || 0) + "ms" }];
    if (els.adapt.checked && adapt.pauses.length >= 2) {
      userChips.push({ text: "wait " + adapt.silenceMs + "ms" });
    }
    addTurn("user", data.transcript, userChips);

    // The round trip is finished, so release the lock before playback: a very short reply can
    // fire `onended` before this function returns, and a still-set `busy` would strand the call.
    busy = false;
    if (data.ended) pendingEnd = true;

    setState("speaking", "Speaking", "Rudi is talking");
    playbackStartedAt = 0;
    waitingForRest = !!data.rest_text;
    enqueue(data.audio_b64, data.audio_mime);
    fetchRest(data.rest_text);

    var gap = await firstSound();
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
    adapt.pauses = []; adapt.rates = [];
    adapt.silenceMs = SILENCE_MS; adapt.lengthScale = 1.0;
    paintStats(); paintAdapt();
    els.log.innerHTML = "";
    els.btnHang.classList.remove("hidden");
    els.btnPush.classList.remove("hidden");
    els.callIdNote.textContent = "Call " + callId + " — voice-bench/calls/" + callId + "/ in the data bucket";

    var netMs = Math.max(0, roundtrip - (data.timings ? data.timings.server_ms : 0));
    setState("speaking", "Speaking", "Rudi's opening");
    playbackStartedAt = 0;
    waitingForRest = !!data.rest_text;
    enqueue(data.audio_b64, data.audio_mime);
    fetchRest(data.rest_text);
    addTurn("rudi", data.reply, chipsFor(data, 0, netMs));
    noteTtsFailure(data);
    // No audio means no `onended` will ever fire — start listening ourselves.
    if (!data.audio_b64) onPlaybackDone();
  }

  async function hangUp(reason) {
    if (!live) return;
    live = false;
    listening = false;
    stopPlayback();
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
