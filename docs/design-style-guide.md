# fetchaller — Retro Computer Style Guide (v3)

Design language: **Windows 95 desktop on a CRT monitor**, framed by a physical beige PC case. Inspired by 98.css, Jurassic Park's Dennis Nedry terminals, and real Win95/98 chrome.

**Core principle**: The page background IS the Win95 teal desktop. Windows float on it. The nav and footer are the physical beige case surrounding the monitor. Commit to this one specific reference — restraint beats decoration.

## Color Palette

### Win95 Desktop
| Name | Hex | CSS Var | Usage |
|------|-----|---------|-------|
| Teal | `#008080` | `--bg` | Page background — the Win95 default desktop color |

### Physical Case (beige plastic — nav, footer ONLY)
| Name | Hex | CSS Var | Usage |
|------|-----|---------|-------|
| Warm Beige | `#C4BA9E` | `--case` | Nav/footer base |
| Raised Panel | `#D2C9AD` | `--case-raised` | Nav/footer surface |
| Border Mid | `#A89E80` | `--case-border` | Case borders |
| Bevel Highlight | `#DDD6C0` | `--case-border-light` | Case top-left bevel |
| Bevel Shadow | `#8A7F64` | `--case-border-dark` | Case bottom-right bevel |

**IMPORTANT**: Beige is ONLY for the physical case (nav/footer). Software UI is gray. Mixing these kills authenticity.

### Win98 Window Chrome (from 98.css — all windows, buttons, controls)
| Name | Hex | CSS Var | Usage |
|------|-----|---------|-------|
| Surface | `#C0C0C0` | `--surface` | Window background, button face, menu bars |
| Button Face | `#DFDFDF` | `--button-face` | Inner light bevel edge |
| Button Highlight | `#FFFFFF` | `--button-highlight` | Outer light bevel edge |
| Button Shadow | `#808080` | `--button-shadow` | Inner dark bevel edge |
| Window Frame | `#0A0A0A` | `--window-frame` | Outer dark bevel edge (near-black) |

### Brand Colors (DO NOT CHANGE)
| Name | Hex | CSS Var | Usage |
|------|-----|---------|-------|
| Purple | `#62007F` | `--purple` | CRT terminal title bars, site dots |
| Purple Light | `#8B2CB0` | `--purple-light` | Title bar gradient end |
| Red | `#FF2300` | `--red` | Primary CTA, tool names |
| Red Dark | `#CC1C00` | `--red-dark` | Button pressed state |

### Text Colors
| Name | Hex | CSS Var | Usage |
|------|-----|---------|-------|
| Dark | `#222222` | `--text` | Text on gray/white surfaces |
| Light | `#E8E8E8` | `--text-light` | Body default (on teal) |
| Muted | `#555555` | `--muted` | Secondary text on light bg |
| Muted Light | `#B0D0D0` | `--muted-light` | Lead text, descriptions on teal |

### Win95 Title Bars
| Name | Hex | CSS Var | Usage |
|------|-----|---------|-------|
| Navy | `#000080` | `--win-blue` | Title bar gradient start |
| Blue Light | `#1084D0` | `--win-blue-light` | Title bar gradient end |

### CRT Screen Colors
| Name | Hex | CSS Var | Usage |
|------|-----|---------|-------|
| Screen BG | `#1E0C2A` | `--screen-bg` | Dark purple CRT background |
| Screen Text | `#CBA4E8` | `--screen-text` | Default terminal text |
| Screen Bright | `#E0C0FF` | `--screen-bright` | Bright phosphor text |
| Screen Dim | `#5E3D78` | `--screen-dim` | Muted/comment text |
| Screen String | `#FF8A6A` | `--screen-string` | String literals, diff removals |
| Screen Key | `#A78DC4` | `--screen-key` | Keywords |
| Screen Green | `#7ECC8E` | `--screen-green` | Diff additions, success |

### JP Green Phosphor (Jurassic Park control room vibes)
| Name | Hex | CSS Var | Usage |
|------|-----|---------|-------|
| Phosphor Green | `#33FF33` | `--phosphor` | Hero h1, logo cursor blink |
| Phosphor Glow | `0 0 8px rgba(51,255,51,0.6)` | — | text-shadow on phosphor text |

### LED Colors
| LED | Hex | Shadow |
|-----|-----|--------|
| Power (green) | `#30d158` | `0 0 6px rgba(48,209,88,0.6)` |
| HDD Activity (red) | `#CC0000` | `0 0 4px rgba(204,0,0,0.6)` |

## Typography

### Primary: IBM Plex Mono
- Body text, code blocks, UI labels, nav, all software chrome
- Weights: 400 (body), 500 (nav links), 600 (FAQ summaries), 700 (headings, buttons)
- Size scale: 0.5625rem (tiny labels) → 0.625rem (status bars) → 0.6875rem (window titles) → 0.75rem (menus) → 0.8125rem (code/nav) → 0.875rem (body small) → 0.9375rem (body) → 1rem (tool names) → 1.375rem (h2)

### Secondary: VT323
- Hero h1 ONLY — pixelated CRT terminal feel
- Size: `clamp(2.5rem, 6vw, 3.8rem)`
- Always paired with phosphor glow: `text-shadow: 0 0 8px rgba(51,255,51,0.6), 0 0 30px rgba(51,255,51,0.15)`
- Google Fonts: `family=VT323&display=swap`

### Font Stack
```css
font-family: 'IBM Plex Mono', 'SF Mono', 'Menlo', monospace;
```

### Anti-Aliasing (CRITICAL for authenticity)
```css
-webkit-font-smoothing: none;
```
Applied to: `.window-title`, `.notepad-menu`, `.notepad-status`, `.win-ctrl`, `.btn-primary`, `.btn-ghost`, `.footer-specs`. This makes UI chrome text look crispy and period-correct. Do NOT apply to body/prose text.

## The 4-Color Bevel System (from 98.css)

**This is the #1 authenticity fix.** Real Win98 uses FOUR colors in a 2px inset box-shadow, not 2-color CSS borders. The order matters.

### Raised bevel (buttons, controls):
```css
box-shadow:
    inset -1px -1px var(--window-frame),    /* outer bottom-right: near-black */
    inset 1px 1px var(--button-highlight),   /* outer top-left: white */
    inset -2px -2px var(--button-shadow),    /* inner bottom-right: gray */
    inset 2px 2px var(--button-face);        /* inner top-left: light gray */
```

### Sunken bevel (text fields, recessed areas):
```css
box-shadow:
    inset -1px -1px var(--button-highlight),
    inset 1px 1px var(--window-frame),
    inset -2px -2px var(--button-face),
    inset 2px 2px var(--button-shadow);
```

### Window bevel (outer frame — slightly different from buttons):
```css
box-shadow:
    inset -1px -1px var(--window-frame),
    inset 1px 1px var(--button-face),        /* note: face, not highlight */
    inset -2px -2px var(--button-shadow),
    inset 2px 2px var(--button-highlight),
    4px 4px 0 rgba(0,0,0,0.4);              /* drop shadow on teal desktop */
```

### Active/pressed (inverted):
Swap each pair: highlight↔frame, face↔shadow.

## Page Background & Overlays

### Win95 Teal Desktop
```css
body {
    background: var(--bg); /* #008080 */
    color: var(--text-light);
}
```
No stipple texture on the desktop — just flat teal. Content windows provide visual variety.

### CRT Vignette (body::before)
Darkened edges like a real CRT monitor — the whole page is "viewed through a monitor":
```css
body::before {
    content: "";
    position: fixed;
    inset: 0;
    background: radial-gradient(ellipse at center, transparent 50%, rgba(0,0,0,0.2) 100%);
    pointer-events: none;
    z-index: 9999;
}
```

### Subtle Dark Scanlines (body::after)
Very subtle — just enough to break up the flatness:
```css
body::after {
    content: "";
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
        to bottom,
        transparent 0, transparent 2px,
        rgba(0, 0, 0, 0.04) 2px,
        rgba(0, 0, 0, 0.04) 3px
    );
    pointer-events: none;
    z-index: 10000;
}
```

## Textures & Patterns

### Nav Crosshatch (beige case panel texture)
```css
background-image:
    linear-gradient(45deg, rgba(0,0,0,0.03) 25%, transparent 25%),
    linear-gradient(-45deg, rgba(0,0,0,0.03) 25%, transparent 25%);
background-size: 6px 6px;
```

### Footer Stipple (different from nav — plastic case)
```css
background-image: radial-gradient(rgba(60, 50, 30, 0.06) 0.5px, transparent 0.5px);
background-size: 10px 10px;
```

### CRT Scanlines (::after on .crt-screen)
```css
background: linear-gradient(
    to bottom,
    rgba(18, 16, 16, 0) 50%,
    rgba(0, 0, 0, 0.18) 50%
);
background-size: 100% 4px;
```

### CRT Curvature (::before on .crt-screen)
```css
background: radial-gradient(
    ellipse at center,
    transparent 55%,
    rgba(0, 0, 0, 0.18) 100%
);
```

### Vent Grille (footer decoration)
```css
background: repeating-linear-gradient(
    to bottom,
    transparent 0, transparent 2px,
    var(--case-border-dark) 2px, var(--case-border-dark) 3px
);
height: 14px;
opacity: 0.3;
```

## UI Components

### Three Window Types (variety kills the AI look)

**1. CRT Terminal** (purple title bar — for actual terminal/code content):
- Title bar: `linear-gradient(90deg, var(--purple), var(--purple-light))`
- Text: `#E8D0FF`, uppercase, `0.6875rem`, `letter-spacing: 0.04em`
- Border-bottom: `2px solid #3A004D`
- Content area: dark CRT screen with scanlines, phosphor glow, curvature vignette
- Use for: install commands, docker commands, code examples, diff

**2. Notepad.exe** (Win95 blue title bar + menu bar + white content):
- Title bar: `linear-gradient(90deg, #000080, #1084D0)` — Win95 active window
- Menu bar: `background: #C0C0C0` with File/Edit/Format/View/Help (underlined hotkeys)
- Content: white background (`#FFFFFF`), dark text (`#1A1A1A`)
- Status bar: `#C0C0C0` with Ln/Col and encoding
- Use for: FAQ, CLAUDE.md instructions, text-heavy content

**3. File Viewer** (Win95 blue title bar + warm off-white content):
- Title bar: same Win95 blue gradient
- Content: warm off-white (`#FEFCF4`) with `1px solid #E0D8C8` border
- No menu bar, no status bar — just content
- Use for: settings.json, config files, step-by-step instructions

**CRITICAL**: NEVER use the same window type for all content. That's what makes it feel AI generated. Mix terminal, notepad, and file viewer windows.

### Window Controls (shared across all types)
```css
.win-ctrl {
    width: 16px; height: 14px;
    background: var(--surface);
    border: none;
    box-shadow:
        inset -1px -1px var(--window-frame),
        inset 1px 1px var(--button-highlight),
        inset -2px -2px var(--button-shadow),
        inset 2px 2px var(--button-face);
    font-size: 0.5rem;
    display: flex; align-items: center; justify-content: center;
    -webkit-font-smoothing: none;
}
/* Symbols: ─ (minimize), □ (maximize), × (close) */
```

### Buttons
- **Primary (red)**: `background: var(--red)`, 4-color inset bevel (red family), drop shadow
- **Ghost (gray)**: `background: var(--surface)`, standard 4-color bevel, drop shadow
- **Active state**: Invert bevel + `translate(2px, 2px)` + no drop shadow
- **Focus**: `outline: 1px dotted; outline-offset: -6px;`
- **Anti-aliasing**: `-webkit-font-smoothing: none` on both

### Branding Stripe
Multi-band badge (like IBM PC front panel):
```css
height: 10px;
background: linear-gradient(to bottom,
    var(--purple) 0, var(--purple) 4px,
    var(--case-raised) 4px, var(--case-raised) 6px,
    var(--red) 6px, var(--red) 10px
);
```

### Site Grid (sunken recessed panel)
White background with sunken 4-color bevel (inverted from raised):
```css
.sites {
    background: var(--button-highlight);
    box-shadow:
        inset -1px -1px var(--button-highlight),
        inset 1px 1px var(--window-frame),
        inset -2px -2px var(--button-face),
        inset 2px 2px var(--button-shadow);
}
```

### Physical Details
- **Power LED**: 8px green circle with glow shadow
- **HDD LED**: 6px red circle with blink animation (0.3s alternate)
- **Floppy Slot**: 80px × 5px sunken dark rectangle with 4-color bevel
- **LED Labels**: 0.5rem uppercase text below LEDs ("PWR", "HDD")
- **Monitor Chin**: `background: var(--surface)` (gray, not beige — matches window chrome)

## Animations

### CRT Power-On (hero)
```css
@keyframes crt-on {
    0%   { filter: brightness(10); transform: scaleY(0.005); }
    40%  { filter: brightness(2); transform: scaleY(1.01); }
    70%  { filter: brightness(1.2); transform: scaleY(0.998); }
    100% { filter: brightness(1); transform: scaleY(1); }
}
```

### CRT Power-Off (easter egg on × click)
```css
@keyframes crt-off {
    0%   { filter: brightness(1); transform: scaleY(1); }
    40%  { filter: brightness(4); transform: scaleY(0.005); }
    70%  { filter: brightness(8); transform: scaleY(0.005) scaleX(0.1); }
    100% { filter: brightness(0); transform: scaleY(0) scaleX(0); }
}
```

### Blinking Cursor
```css
@keyframes blink { 50% { opacity: 0; } }
/* Block cursor: content "█" with step-end timing */
```

### HDD Activity LED
```css
@keyframes hdd-blink {
    0%   { opacity: 0.2; box-shadow: none; }
    100% { opacity: 1; box-shadow: 0 0 4px rgba(204,0,0,0.6); }
}
/* 0.3s ease-in-out infinite alternate */
```

### Section Reveal (scroll-triggered)
```css
.reveal { opacity: 0; transform: translateY(8px); transition: 0.4s ease; }
.reveal.visible { opacity: 1; transform: none; }
```

## Easter Eggs

### Jurassic Park "Ah ah ah" (× button on hero monitor)
Clicking the close button on the hero CRT shows green phosphor text "Ah ah ah... You didn't say the magic word!" for 1.5s, then plays CRT off → CRT on reboot animation.

### Footer System Specs
```
ACCESS: MAIN SECURITY GRID | 486DX2-66 | 8MB RAM | 540MB HDD | MCP: ONLINE
```

## Page Composition Philosophy

Think of the page as a **Windows 95 desktop viewed on a CRT monitor, with the physical beige PC framing it**:

1. **Stripe** = Front panel branding badge (purple + red bands)
2. **Nav** = Front panel beige case with embossed label, crosshatch texture
3. **Hero** = CRT terminal window on the teal desktop
4. **Content windows** = Mix of Win95 application windows (CRT terminal, Notepad, File viewer)
5. **Teal gaps** between windows = The desktop showing through (no section borders needed)
6. **Footer** = Bottom of the beige case with vent grille and JP-flavored specs

### Key Principles
- **Teal desktop (#008080) is the page** — windows float on it with drop shadows
- **Beige = physical case** (nav/footer). **Gray = software UI** (windows, buttons). Never mix.
- **4-color bevels from 98.css** on everything — the authentic Win98 look
- **`-webkit-font-smoothing: none`** on UI chrome — crispy text sells the illusion
- **Green phosphor (#33FF33)** for the hero h1 — JP control room vibes
- **White h2 headings** with text-shadow — readable on teal, strong hierarchy
- **CRT effects** (scanlines, glow, curvature) ONLY inside `.crt-screen` areas
- **CRT vignette + dark scanlines** on entire body — "viewed through a monitor"
- Descriptions and lead text use `var(--muted-light)` (#B0D0D0) — reads well on teal
- **Window variety** is mandatory — never use the same window type twice in a row
- Physical details sell the illusion (LEDs, floppy slot, vents, system specs)
- Monospace everywhere — the era was fixed-width
- Restraint > decoration — commit to Win95 reference, don't fill every pixel with nostalgia

## Brand Voice & Personality

fetchaller is a **power tool for nerds**. It's not enterprise SaaS. It's a scrappy MCP server that rips through bot protection and gives you clean markdown. The vibe is:

- **Hacker, not corporate.** Think "guy who built this in his garage" not "team of 12 with a product manager." No marketing-speak. No "empower your workflow." Just say what it does.
- **Jurassic Park IT room, not Silicon Valley.** Dennis Nedry's terminal, not a pitch deck. Green phosphor, system specs, easter eggs. The page should feel like something you'd find on a computer that also controls a theme park's security grid.
- **Confident but not try-hard.** The tool is good. Let the comparison diff speak for itself. Don't oversell. One CTA is enough. The README-in-a-Notepad-window is more convincing than any hero copy.
- **Era-appropriate humor.** Footer specs, "ah ah ah" easter egg, floppy slot. These aren't decorations — they're personality. But don't overdo it. One JP reference is cool, twelve is theme-park merch.

### Tone in Copy
- Short sentences. Imperative mood. ("Clone and install." not "You can clone and install the repository.")
- Technical readers — don't explain what `git clone` is.
- Specific numbers beat vague claims ("65–98% cleaner" not "much cleaner")
- No emoji. No exclamation marks. Monospace font says enough.

## What Makes a Layout Feel AI-Generated (and how to avoid it)

### The SaaS Template Problem
AI always generates the same landing page skeleton:
1. Hero with gradient + CTA buttons
2. "Why us?" comparison section
3. Features (3 cards in a row)
4. How it works (3 numbered steps)
5. Pricing
6. FAQ accordion
7. Footer CTA

This structure is **instantly recognizable** as AI output. Even with good art direction, if the content flows in this exact order with this exact rhythm, it reads as generated.

### Current Layout Problems (TODO)
The current page follows this pattern too closely:
- Hero → Comparison → Setup → Features → FAQ — textbook SaaS template
- Every section is the same width (740px), same spacing (56px), same structure (h2 → desc → window)
- No rhythm variation — it's window, window, window, window all the way down
- The "What's included" section repeats the same block three times (tool name → desc → code window)
- Everything is perfectly centered and symmetrical

### Layout Principles to Fix This
- **Break the grid occasionally.** Not everything needs to be centered in a 740px column. A window could be wider. A section could be narrower. The site grid could break out.
- **Vary section density.** Some sections should be compact (just a window, no preamble). Others can breathe. Don't give every section the same h2 → description → content treatment.
- **Front-load the "what is this" answer.** People land on this page from search. They need to know what fetchaller is within 2 seconds. The hero h1 does this well. But the comparison diff should feel like it's part of the hero moment, not a separate section with its own heading.
- **Combine related content.** "Run locally" and "Host at home" are both setup — they could be one section with tabs or a toggle, not two separate sections with separate h2 headings.
- **The Notepad-with-CLAUDE.md is the killer feature visually.** It shows the actual README instructions inside a Notepad.exe window. That's the most convincing "this works" moment on the page. It should be more prominent.
- **Site grid needs to do more work.** Right now it's buried in "What's included" as a sub-section. This is one of the strongest selling points (15+ sites with specific cleanup). Consider making it more prominent or visual.
- **FAQ can be shorter or killed.** Most FAQ content duplicates what's already on the page. Keep only questions that genuinely aren't answered elsewhere.
- **Think in "desktop windows" not "sections."** On a Win95 desktop, windows overlap, stack, and have different sizes. The page should feel like someone arranged windows on their desktop, not like a vertical scroll of identically-spaced content blocks.

### Anti-Patterns (NEVER do these)
- Don't make every section the same height/spacing
- Don't use the same h2 → desc → window structure for every section
- Don't center everything — some elements should be left-aligned or offset
- Don't add a section just to have a section — if two things are related, combine them
- Don't repeat the same window type in consecutive sections
- Don't use "Get started" AND "View source" as a pair of hero CTAs — that's the AI default. One CTA is stronger.
- Don't write h2 headings that sound like a product manager wrote them ("Why not WebFetch?", "What's included", "Host at home")

## Design Research & Inspiration

### Sites That Feel Authentic (studied 2026-02)
- **98.css** (jdan.github.io/98.css) — The gold standard for Win98 CSS. 4-color bevel system, exact pixel values. We use their bevel pattern directly.
- **poolsuite.net** — Retro computer aesthetic done right. Committed to ONE era, didn't try to do everything. Key lesson: restraint.
- **68k.news** — HN reader styled as classic Mac. Just does the thing, no hero section, no CTA buttons.
- **low-tech magazine (solar.lowtechmagazine.com)** — Runs on a solar-powered server. Dithered images, system fonts, no JavaScript. Authentic because the constraints are real.
- **Jurassic Park terminals (reference)** — Green phosphor on dark bg, blinking cursors, system diagnostics. The vibe is "this computer controls something important."

### What These Sites Have in Common
1. **They commit to ONE reference.** 98.css does Win98. 68k.news does classic Mac. They don't mix eras or pile on signifiers from different decades.
2. **The design serves the content, not the other way around.** They don't have sections just to fill space.
3. **No marketing structure.** No hero → features → CTA → FAQ pipeline. The content is organized by what it IS, not by a conversion funnel.
4. **Personality comes from details, not decoration.** A well-chosen title bar label does more than ten CSS effects.
5. **They feel like someone MADE them, not generated them.** Imperfections, personal choices, opinions baked into the design.

## Reference Libraries (not dependencies, just references)
- **98.css** — Windows 98 UI: https://jdan.github.io/98.css/
- **afterglow-crt** — CRT CSS overlay: https://github.com/HauntedCrusader/afterglow-crt
- **system.css** — Apple System OS: https://github.com/sakofchit/system.css
