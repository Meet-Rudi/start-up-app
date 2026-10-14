// Training Arena runtime config.
// API_BASE is the meetrudi-arena-api Function URL (printed by `python deploy.py training-arena`).
// Empty = PROTOTYPE mode: every button works against demo-data.js and nothing leaves the browser.
// This file holds no secrets — the API owns every auth decision, and a URL is not a credential.
window.ARENA_CONFIG = {
  API_BASE: "https://3aqw3jkjx4xl762ktsymz4a3em0smahg.lambda-url.eu-central-1.on.aws",
  SESSION_IDLE_MIN: 30,
};
