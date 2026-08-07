# UX implementation notes

How the supplied Claude Design output maps onto what was built, and the four
places the implementation deliberately departs from it.

The specification sets the precedence: the design is the visual and interaction
source of truth **unless it conflicts with security, reliability, accessibility,
localhost-only, or functional requirements**. Every departure below sits in one
of those categories, and each is covered by a test so it cannot quietly revert.

---

## What was ported unchanged

- The Modernist palette, the Archivo type stack, the zero border-radius, and the
  `.btn` / `.card` / `.tag` / `.table` / `.seg` / `.input` component classes.
- The three-group sidebar: Videos, Collections, This computer.
- The header: brand, the "Runs only on this computer" badge, worker state, and
  free-space label.
- All eleven screens, in the design's own order: first-run readiness, dashboard,
  new job, job detail, review, outputs, imports, settings, collection list,
  create-collection flow, collection detail.
- The plain-language vocabulary throughout — "pictures" rather than frames,
  "Nothing said" rather than silence detection, "No provider API charge" rather
  than "$0.00".

---

## Departure 1 — no web font, no external request

**The design** imports Archivo from a font service in its stylesheet.

**What was built** loads no external resource at all. The font stack falls back
to the system UI font, which is a close grotesque on macOS, Windows, and Linux.

**Why.** Every page carries a badge reading "Runs only on this computer —
nothing is uploaded". A stylesheet that fetches from a CDN would contradict that
on every page load, regardless of how careful the rest of the application is.
This is the localhost-only requirement, and it outranks exact typeface fidelity.

**Test.** `test_no_external_resource_is_referenced` asserts no off-origin URL
appears in any rendered page.

---

## Departure 2 — filled buttons use a darker accent

**The design** fills primary buttons with the brand accent `#ec3013` and puts
the page background colour on top as the label.

**What was built** fills them with `--color-accent-700` (`#ae1800`) and uses
`accent-800` on hover. The brand accent keeps every non-text role: borders,
focus rings, status marks, the highlighted-note rule.

**Why.** `#ec3013` against `#f3f2f2` is **3.76:1**. That clears the 3:1 bar for
a border or a mark, but a button label is 14px semi-bold text and needs 4.5:1.
`accent-700` gives 6.41:1. The palette still reads as intended because the
accent is unchanged everywhere it is not carrying text.

**Test.** `test_reversed_text_meets_aa` computes the ratio from the palette
rather than trusting a visual check.

---

## Departure 3 — muted text is darker

**The design** sets secondary text at 55% of the text colour.

**What was built** uses 65%.

**Why.** 55% works out to **3.66:1** against the page. Muted text is still
text — a secondary label that fails contrast is one some readers cannot use at
all. 65% gives 4.96:1 and still reads as clearly secondary.

**Test.** `test_muted_text_still_meets_aa`.

---

## Departure 4 — hollow status markers are darker

**The design** draws the hollow "waiting" and "draft" markers in
`--color-neutral-500`.

**What was built** uses `neutral-600`.

**Why.** `neutral-500` is **2.59:1** against the page. A status marker carries
meaning, so it needs 3:1. `neutral-600` gives 3.85:1.

**Test.** `test_meaningful_non_text_meets_the_three_to_one_threshold`.

---

## Additions the design did not specify

**Status is never colour alone.** Each state carries a word, a shape, and a
colour, and the shape is described for screen readers. Colour-only status is
unreadable for a large minority of users and disappears entirely in a monochrome
screenshot. States sharing the accent hue — running, waiting to retry, needs you
— are given distinct shapes; there is a test asserting they do not collapse to
one.

**An unrecognised state renders as itself.** Showing an unknown status as
"Finished" would be a lie, so `present()` falls through to the raw value.

**Nothing is invented.** The design's mock data (`capture_0914.mp4`, "Session
review — 14 Feb") appears nowhere in the implementation. Every screen reads real
state, and an empty one says so. Placeholder content a user might mistake for
their own work is worse than an honest empty state, and there is a test
asserting those example names never appear on an empty dashboard.

**A stored key is never displayed.** Not the value, not a prefix, not a partial
mask. A few revealed characters still narrow a search and there is no situation
where the user needs them, so the interface reports presence only.

**Below 1024px the sidebar stacks rather than hiding.** Navigation the user
cannot reach is worse than navigation that takes vertical space. The 1024px
media query is asserted to contain no `display: none`.

**Skip link, landmarks, captions, and progressbar roles** throughout. Focus
outlines are restyled, never removed — an invisible focus ring makes the whole
interface unusable without a mouse.

---

## Where the design's interactivity became server-rendered

The design is a single-file prototype with client-side state. The
implementation is server-rendered with plain forms, because the specification
asks for a lightweight frontend without an unnecessary SPA, and because a job
that survives the browser closing should not depend on browser state.

- Screen switching → real routes.
- The step indicator in the create-collection flow → a static progress list; the
  form itself is one page, so a partially filled multi-step wizard cannot be
  lost.
- Pause / resume / cancel → `POST` with a redirect, so the browser back button
  behaves and the action cannot be replayed by refreshing.
- Frame review scrubbing → deferred to the review screen's per-video table for
  now; the underlying clean/numbered distinction and its explanatory copy are
  both present.

Collection generation is deliberately *not* behind a confirmation dialog. It is
local, free, non-destructive, and takes seconds — the specification calls out
excessive confirmation here as a fault.
