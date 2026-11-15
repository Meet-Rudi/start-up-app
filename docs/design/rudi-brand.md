# Rudi visual identity: the front-end design template

> Binding for every front-end we build: tenant and ops consoles, sign-up and consent pages,
> the Training Arena, widgets, e-mails, and mock-ups. Read from the live **meetrudi.eu**
> stylesheet and pages (EN and NL) on 2026-11-12; the site is the reference when this file is
> silent. Code version of these rules: [web/brand/rudi-brand.css](../../web/brand/rudi-brand.css).
> Assets: [web/brand/assets](../../web/brand/assets) and [web/brand/fonts](../../web/brand/fonts).
> Example page: [web/mockups/clone-request.html](../../web/mockups/clone-request.html).
> Decision #45 in [project.md](../../project.md). Last updated: 2026-11-13.

## 1. The feel in one paragraph

Warm, calm and human: an ivory page, deep petrol ink, butter-yellow warmth. Big, light
headlines in short sentences that end with a full stop. Generous space, few elements per
screen, soft organic shapes that look hand-made rather than machined, and a friendly yellow
mascot used sparingly. Nothing shouts: no gradients, no glossy shadows, no neon, no stock-photo
gloss. It should feel like a kind coach, not a clinic or a fintech dashboard.

## 2. Colour

Use the tokens, never raw values, and add no colour without changing this file. Hex values
are the site's HSL tokens converted.

| Token | Hex | HSL | Use |
|---|---|---|---|
| `--ivory` | `#F8F5EC` | 44 47% 95% | Page background (default) |
| `--paper` | `#FFFCF5` | 45 100% 98% | Cards, popovers, fields, chat surface |
| `--petrol` | `#244A4C` | 183 35% 22% | Text, primary buttons, dark bands, logo |
| `--petrol-hover` | `#345E60` | 183 30% 29% | Hover on petrol buttons |
| `--petrol-2` | `#3D6466` | 183 25% 32% | Secondary dark (rarely) |
| `--ink-muted` | `#48615F` | 175 15% 33% | Secondary text, captions, footer |
| `--butter` | `#E6CF79` | 47 69% 69% | The accent: headline underline, bullets, CTA band, card fill, links on petrol |
| `--wash` | `#FBF2D5` | 46 84% 91% | Soft alternate band, Rudi's chat bubbles |
| `--apricot` | `#EAAC86` | 23 70% 72% | Thin accent outlines (a tagline pill), never text |
| `--card-sage` | `#E5EDE3` | 108 22% 91% | Card fill (second of the three) |
| `--card-apricot` | `#F6E3D5` | 26 64% 90% | Card fill (third of the three) |
| `--stone` | `#F3E9CE` | 44 60% 88% | Quiet fills, tags |
| `--line` | `#C2D5D6` | 183 20% 80% | Decorative hairlines only (dividers) |
| `--field-border` | `#5E8A8D` | 183 20% 46% | Form field and control borders (ours: ≥ 3:1) |
| `--ring` | `#A67A21` | 40 67% 39% | Focus ring on light surfaces |
| `--danger` | `#BD2828` | 0 65% 45% | Errors only, always with words |

Hairlines on the site are petrol at 15% (`rgba(36,74,76,.15)`) and pill outlines petrol at
30%; both are decorative.

**Pairings that pass WCAG AA** (contrast measured):

| Text | On | Ratio | Rule |
|---|---|---|---|
| petrol | ivory / paper / wash | 8.9 / 9.5 / 8.7 | Default text everywhere |
| petrol | butter, card-sage, card-apricot | 6.3 / 8.1 / 7.8 | Text on coloured cards and the CTA band |
| ink-muted | ivory / wash | 6.1 / 6.0 | Secondary text on light bands only |
| ivory | petrol | 8.9 | Text in dark bands and on primary buttons |
| butter | petrol | 6.3 | Eyebrows, icons and links inside dark bands |

**Never:** butter, apricot or `--line` as text on a light background (1.4–1.8:1); ink-muted
on butter (4.3:1, too low for small text); white `#FFF` anywhere (use paper or ivory).

**Proportions:** mostly ivory, petrol as the ink, butter as the one warm accent per view. A
page alternates full-width bands: ivory → wash → ivory → petrol → butter (closing CTA) →
ivory footer. One petrol band and one butter band per page at most.

## 3. Type

| Role | Font | Weight | Size (desktop → mobile) | Line height | Tracking |
|---|---|---|---|---|---|
| Display (h1) | Manrope | 500 | `clamp(44px, 4.9vw, 66px)` | 1.13 | −0.05em |
| Section title (h2) | Manrope | 500 | 44–56px → 34px | 1.13 | −0.05em |
| Card title (h3) | Manrope | 500 | 28px → 24px | 1.22 | 0 |
| Small title (h4) | Manrope | 500 | 21–22px | 1.15 | 0 to −0.05em |
| Lead | DM Sans | 400 | 18–19px | 1.6–1.8 | 0 |
| Body | DM Sans | 400 | 16px | 1.6 | 0 |
| Small / caption | DM Sans | 400 | 12–14px | 1.35 | 0 |
| Eyebrow | DM Sans | 700 | 11px, uppercase | 1.6 | 0.15em |
| Button | DM Sans | 600 | 16px (12–14px small) | 1.6 | 0 |

- Headings are **Manrope 500, never bold**; the size does the work. Body is DM Sans.
- Headlines are short sentences ending with a full stop ("Small steps. Meaningful
  milestones."), often broken over two or three lines.
- Mark **one** key phrase per headline with the butter underline (`.r-under`): a flat, rounded
  butter stroke behind the last words, rotated −3°.
- Every section title gets an eyebrow above it: a short uppercase phrase, optionally led by a
  small petrol dot.
- Text columns stay narrow: 470px for lead text, 680px for headlines.
- **Fonts are self-hosted** from `web/brand/fonts/` (SIL OFL, licences included). Never load
  Google Fonts or any font CDN: it sends visitors' IP addresses to a third party
  (CLAUDE.md §0, Prime Directive 3). Fallback: `system-ui, sans-serif`.

## 4. Shape and depth

- Base radius `--radius: 0.75rem` (12px) for fields, images and small panels.
- **Pills** (fully round) for buttons, chips, the language toggle and tags.
- **Organic cards:** a soft squircle with gently bulging sides and a solid petrol edge offset
  4px down and right, like a cut-out lying on the page. Fills rotate butter → card-sage →
  card-apricot across a row of three. Number them `01`, `02`, `03` in small text.
- **Chat bubbles:** 18px radius with the tail corner at 3px. Rudi's bubbles are wash with
  petrol text, on the left; the user's are petrol with ivory text, on the right.
- **Nudge cards:** paper, 18/18/18/3px radius, tilted 1–3°, the only soft shadow on the site
  (`0 12px 25px rgba(36,74,76,.04)`).
- Photos sit in rounded or organic masks, never full-bleed rectangles with hard corners.
- Otherwise flat: no gradients, no glass, no heavy drop shadows.

## 5. Layout and spacing

- Content width 1172px (`--container`), side gutter 24px on mobile; at 1440px wide the
  content starts 134px from the edge.
- Bands: 64–80px vertical padding (40–56px on mobile); a thin strip band 28px.
- Grids: 12-column feel; heroes are text left (about 55%) and mascot or visual right; cards in
  three equal columns with 20–28px gaps, one column below 768px.
- Header: sticky, ivory at 95% opacity, hairline bottom border; logo left; nav links 14–15px;
  language pill (`EN` / `NL`); one primary pill button on the right.
- Footer: hairline top border, small logo, one-line tagline ("AI support for everyday
  follow-through."), muted 12px links on the right.

## 6. Components

| Component | Look |
|---|---|
| Primary button | Petrol pill, ivory text, DM Sans 600, padding 15px 24px, trailing `↗` when it leaves the page; hover petrol-hover |
| Secondary action | A text link with a 1px petrol bottom border, 14px, no underline decoration |
| Action on a dark band | Butter text link, 600, with `↗` |
| Eyebrow | 11px uppercase, 0.15em tracking, 700, optional 6px petrol dot before it |
| Headline mark | `.r-under` butter stroke under the key phrase |
| Topic list / bullets | The butter "leaf" (a lemon-shaped diamond) instead of dots; on petrol bands too |
| Chip | Paper pill, 12px text, a small line icon (message bubble) on the left |
| Tagline pill | Paper pill with a 1px apricot outline, slightly rotated, 12px, 0.08em tracking |
| FAQ | `<details>` rows separated by petrol-15% hairlines, a `+` on the right, 18px question text |
| Privacy points | Line icon in butter on petrol, small Manrope title, muted text below |
| Dark panel | A rounded-3xl (24px) outline panel inside a petrol band, items split by hairlines |

**Forms** (the site has none; these extend the system):

- Label above the field: DM Sans 600, 15px, petrol. Help text below the label: 13px,
  ink-muted. Mark optional fields with "(optional)", not required ones with asterisks.
- Fields: paper background, 1px `--field-border`, radius 12px, min height 48px, 16px text.
- Choices: checkboxes and radios as **choice tiles** (paper, 1px field-border, radius 12px);
  a checked tile turns wash with a petrol border. Native inputs with `accent-color: petrol`.
- Errors: `--danger` border plus a written message; never colour alone. Validate after the
  user has interacted (`:user-invalid`), not on page load.
- Group long forms in numbered sections (`01`, `02`, …) with an eyebrow and a Manrope title.

## 7. Imagery and icons

- **The mascot:** a butter-yellow rounded slab with a petrol back and a simple face (two
  eyes, brows, a small smile), usually tilted about −7° inside a paper circle. Use it for
  heroes, empty states and Rudi's avatar; one per view. Files: `rudi-mascot.png` (the
  welcoming pose, also the chat avatar), `rudi-logo.png` (wordmark), `favicon.png`. The
  site's faceless body pose gets its face from an animated SVG and is not used without it.
- Never redraw, recolour or stretch the logo or mascot. Logo on light backgrounds only.
- Icons: line icons in the Lucide style (1.5–2px stroke, round caps), petrol on light,
  butter on petrol.
- Photos: real people, warm and candid, in rounded masks.

## 8. Motion

Gentle and playful: elements fade or rise in once on scroll, the mascot may hop, bubbles pop
in. Under `prefers-reduced-motion: reduce`, nothing moves. Never animate text people are
reading, and never loop.

## 9. Accessibility (our minimums, stricter than the site)

- Text contrast AA (4.5:1, large text 3:1); field borders and focus rings 3:1 (pairings in §2).
- Focus: 3px outline offset 3–4px; `--ring` on light surfaces, butter on petrol bands, petrol on
  butter bands.
- Hit targets at least 44px high. The page works at 320px wide without sideways scrolling.
- Every image has `alt` text, or `alt=""` when decorative (the mascot usually is).
- Language: `<html lang>` set; every user-facing string comes from the i18n catalogue in real
  front-ends (CLAUDE.md §3). English and Dutch (Flemish) first.

## 10. Voice on screen

Plain, warm and short. Speak to "you"; Rudi is "he". Lead with the person's life, not the
technology ("Support that continues between appointments."). Small, honest promises: no
miracle claims, no medical claims. Rudi supports follow-through; he doesn't diagnose,
prescribe or replace professional care (Prime Directive 2). Label illustrations as such
("Illustrative conversation · not a live WhatsApp chat").

## 11. Building with it

1. Link `web/brand/rudi-brand.css` and use its tokens and `r-` classes; add page-specific CSS
   on top, never new colours or fonts.
2. Plain HTML and CSS first. JavaScript only where HTML cannot do the job (native form
   validation, `<details>`, `:has()` and `:user-invalid` cover most needs).
3. No third-party requests from the page (fonts, icons, analytics) unless EU-hosted under a
   DPA (CLAUDE.md §0).
4. Existing pages (`site/`, `console/`, `web/cm-consent-form/`) predate this identity; move a
   page onto it when it is next changed, not in a big-bang restyle.
5. When the live site changes its identity, update this file and the CSS together.
