# MEET_RUDI — Visual Design Manual

> Canonical brand reference for all front-end work. Extracted from the live company
> site **https://www.meetrudi.eu/en** (June 2026). Build every page against these tokens;
> the machine-usable version is [`brand.css`](brand.css). Last updated: 2026-06-07.

---

## 1. Asset inventory (files in this folder)

| File | What it is | Source | Notes |
|------|-----------|--------|-------|
| [`rudi-logo.png`](rudi-logo.png) | Primary logo — two overlapping speech bubbles | `/assets/rudi-logo-DZghi9hu.png` | 2000×2000, RGBA, **transparent** background |
| [`favicon.ico`](favicon.ico) | Site favicon | `/favicon.ico` | 256×256 |
| [`social-card.webp`](social-card.webp) | Open Graph / social share image | og:image | 1200×630, reference only |
| [`brand.css`](brand.css) | Reusable CSS variables + font import | derived | **import this in pages** |

## 2. Logo

Two overlapping **speech bubbles** (navy fill):
- **Left/front bubble** — outlined in **magenta** with three magenta dots.
- **Right/back bubble** — outlined in **cyan** with three cyan dots.
- Transparent background → works on light *and* dark surfaces.
- Symbolism: conversation / chat — central to the product (Rudi as a chatting buddy).

**Usage:** keep clear space around it; don't recolor; on busy backgrounds place on a plain
panel. For favicons/small sizes use `favicon.ico`.

## 3. Typography

- **Typeface:** **Nunito** (Google Fonts), weights **400, 500, 600, 700, 800**.
- Rounded, friendly, approachable — matches the "friendly buddy" tone.
- Headings: 700–800. Body: 400–500. Buttons/labels: 600–700.
- Large display headings are set in **UPPERCASE** for impact (see the how-can-i-help hero).

## 4. Color palette

| Token | Hex | HSL | Role |
|-------|-----|-----|------|
| `--rudi-navy` | `#263d7d` | `224 53% 32%` | **Primary** — headings, body text, logo fill |
| `--rudi-navy-deep` | `#121621` | `224 30% 10%` | Dark background |
| `--rudi-blue` | `#3b75a5` | `207 47% 44%` | Secondary / links |
| `--rudi-magenta` | `#e925c9` | `310 82% 53%` | **CTA / primary action**, accent |
| `--rudi-cyan` | `#00e5e5` | `180 100% ~45%` | Interactive accent, highlights |
| `--rudi-bg` | `#f0f0f0` | `0 0% 94%` | Page background (light) |
| `--rudi-surface` | `#ffffff` | `0 0% 100%` | Cards / panels |
| `--rudi-muted` | `#9da2af` | `224 10% 65%` | Muted/secondary text |
| `--rudi-border` | `#d9d9d9` | `0 0% 85%` | Borders / inputs |

**Brand gradient:** magenta → cyan (`135deg`), echoing the twin-bubble logo.

### Usage rules
- **Magenta = the call-to-action color.** Primary buttons (e.g. "Ask Rudi") are magenta.
- **Navy = text & structure.** Default to navy for headings/body on light backgrounds.
- **Cyan = highlights/decoration**, not body text (too light for legibility on white).
- Light theme is the default; dark theme uses `--rudi-navy-deep` with light text.

## 5. Shape & feel

- Base radius **0.75rem**; large surfaces **1.5rem**; buttons **pill** (999px).
- Soft, rounded, friendly. Optional decorative **blurred magenta/cyan blobs** behind
  content (the source uses large `blur` glows) for an engaging, modern feel.
- Generous whitespace; clean and uncluttered.

## 6. Provenance
Colors/fonts extracted from the site's CSS bundle (`/assets/index-*.css`): font import
`Nunito`, and `:root` design tokens (`--brand-primary`, `--brand-cta`, `--brand-interactive`,
etc.). Logo/favicon downloaded from the site's asset paths. Re-verify if the site rebrands.
