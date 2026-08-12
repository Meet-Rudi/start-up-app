/* MEET_RUDI — voice bench endpoint configuration.
 *
 * Paste the Function URL printed by `python deploy.py voice-bench` here, then commit and push.
 * GitHub Pages serves this file as-is, so no build step is involved.
 */
window.VOICE_BENCH_API = "https://ddkigfzjzpu52qox4zlepvlkji0cvvyu.lambda-url.eu-central-1.on.aws/";

/* Defaults for the call config panel. Anything set here just pre-fills the form —
 * the operator can change every field before starting a call. */
window.VOICE_BENCH_DEFAULTS = {
  language: "en",
  user_name: "",
  topic: "",
  voice: "en_US-ryan-medium",
  start_phase: "goal",
  max_minutes: 12,
  store_audio: true,
  sentence_gap_ms: 300,
  notes: ""
};

/* Voices seeded in meetrudi-tts. Short keys also work: en, nl (Flemish), nl_NL, fr, de.
 * Flemish is parked pending native-speaker testing; English runs on ryan-medium. */
window.VOICE_BENCH_VOICES = [
  ["en_US-ryan-medium", "English · US · male (chosen)"],
  ["en_US-lessac-medium", "English · US · female"],
  ["en_US-amy-medium", "English · US · female"],
  ["en_GB-alan-medium", "English · UK · male"],
  ["en_GB-jenny_dioco-medium", "English · UK · female"],
  ["nl_BE-nathalie-medium", "Flemish · female"],
  ["nl_BE-rdh-medium", "Flemish · male"]
];

/* Endpointing. Tune these if the bench cuts people off or waits too long.
 *   silenceMs   — how long a pause must last before we treat the turn as finished
 *   minSpeechMs — ignore blips shorter than this (coughs, door clicks)
 *   maxTurnMs   — hard stop so a stuck mic can't upload forever
 *   threshold   — multiplier over the measured room noise floor
 */
window.VOICE_BENCH_VAD = {
  silenceMs: 900,
  minSpeechMs: 350,
  maxTurnMs: 30000,
  threshold: 2.6,
  floorMin: 0.006
};
