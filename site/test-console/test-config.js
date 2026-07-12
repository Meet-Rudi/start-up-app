// MEET_RUDI personality TEST console — runtime config.
// No secret here: auth is the email+password you type at the login screen (validated server-side),
// which returns a session token kept only in the browser. Just point API_BASE at the deployed
// meetrudi-test-console Function URL (deploy.py prints it as "Test console").
window.TEST_CONSOLE_CONFIG = {
  API_BASE: "https://j7kkfcmg724bjqcpsojyanehxi0tczil.lambda-url.eu-central-1.on.aws",
};
