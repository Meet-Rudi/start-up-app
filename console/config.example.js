// MEET_RUDI operator console — runtime config TEMPLATE.
// Copy this file to `config.js` (which is gitignored) and fill in the real values locally.
// NEVER put the real CONSOLE_TOKEN in a tracked file — config.js holds it, and only locally.
//
//   cmd:  copy console\config.example.js console\config.js
//
// Leave API_BASE empty to run the console in DEMO mode (synthetic data, no backend).
window.CONSOLE_CONFIG = {
  API_BASE: "",                       // e.g. https://<console-api-id>.lambda-url.eu-central-1.on.aws
  CONSOLE_TOKEN: "",                  // the meetrudi/whatsapp/console-token value (interim, until Cognito)
  OPERATOR_ID: "operator",
  POLL_MS: 4000,
};
