// MEET_RUDI operator console — runtime config.
// Deploy step overwrites API_BASE with the meetrudi-wa-console-api Function URL (behind
// Cognito/CloudFront). Leave API_BASE empty to run the console in DEMO mode on synthetic
// data (no backend, no integrations) — used for Block-3 UI development.
window.CONSOLE_CONFIG = {
  API_BASE: "",          // e.g. "https://xxxx.lambda-url.eu-central-1.on.aws"
  CONSOLE_TOKEN: "",     // stopgap shared secret; replaced by Cognito auth
  OPERATOR_ID: "operator",
  POLL_MS: 4000,         // roster + active-thread poll cadence (within the 10–15s budget)
};
