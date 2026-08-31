/* MEET_RUDI tester console — deployment configuration.
 *
 * The ONE file to edit after deploying. Take API_BASE from the `TesterApiUrl` output of the
 * meetrudi-whatsapp stack:
 *
 *     aws cloudformation describe-stacks --stack-name meetrudi-whatsapp ^
 *       --region eu-central-1 --profile rudi-deployer ^
 *       --query "Stacks[0].Outputs[?OutputKey=='TesterApiUrl'].OutputValue" --output text
 *
 * Nothing secret belongs here. This file ships to GitHub Pages and is world-readable — the API
 * base is a public endpoint that authenticates every request itself. No token, no key, no
 * phone number, ever (CLAUDE.md §0.5).
 */
window.RUDI_CONFIG = {
  // Lambda Function URLs end with a slash; the client strips it, so either form works.
  // Deployed 2026-08-28 — the TesterApiUrl output of the meetrudi-whatsapp stack.
  API_BASE: "https://rczvuf3n5xrdcz5wt7yybs3uiq0vecoo.lambda-url.eu-central-1.on.aws/",

  // Shown on the login page and in the console footer so a stuck tester has somewhere to write.
  SUPPORT_EMAIL: "support@meetrudi.eu",

  // Default language of the sign-up form. A tester can switch; their choice is stored on their
  // profile and drives the console, the mails, Rudi's replies and his voice.
  DEFAULT_LOCALE: "nl-BE",
};
