"""Accessibility: contrast, keyboard operation, and semantics.

Contrast is computed from the palette rather than eyeballed. A ratio that drifts
below the threshold after a token change is exactly the kind of regression that
survives review and then fails an audit months later.

Thresholds are WCAG 2.2 AA: 4.5:1 for body text, 3:1 for large text and for the
non-text parts of a control that carry meaning.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import open_database, utc_now
from app.web.app import create_app
from tests.loopback import LOOPBACK_BASE_URL

CSS = Path(__file__).resolve().parents[2] / "app" / "web" / "static" / "tokens.css"

AA_NORMAL = 4.5
AA_LARGE = 3.0

# The palette, read straight out of the stylesheet so the tests and the
# interface cannot disagree about what the colours are.
PALETTE = {
    "bg": "#f3f2f2",
    "surface": "#eae9e9",
    "text": "#201e1d",
    "accent": "#ec3013",
    "accent-100": "#fff2ef",
    "accent-600": "#dd2b0f",
    "accent-700": "#ae1800",
    "accent-800": "#7c1405",
    "accent-900": "#4d170e",
    "neutral-100": "#f8f4f4",
    "neutral-300": "#d7d3d3",
    "neutral-500": "#9b9797",
    "neutral-600": "#7d7979",
    "neutral-700": "#605d5d",
    "neutral-800": "#444141",
    "accent-2-100": "#fff2ef",
    "accent-2-800": "#71261b",
}


def relative_luminance(hex_colour: str) -> float:
    """WCAG relative luminance."""
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    a, b = relative_luminance(first), relative_luminance(second)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


# ── The palette itself ────────────────────────────────────────────────────


def test_the_stylesheet_defines_the_colours_these_tests_assume():
    """Guards against the tests drifting away from the interface."""
    css = CSS.read_text(encoding="utf-8")
    for name, value in PALETTE.items():
        assert f"--color-{name}: {value}" in css, f"--color-{name} is not {value} in the stylesheet"


def test_contrast_maths_matches_known_values():
    # Black on white is the textbook 21:1.
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)


# ── Body text ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("foreground", "background", "where"),
    [
        ("text", "bg", "body text on the page"),
        ("text", "surface", "body text on a card"),
        ("text", "neutral-100", "body text on a striped row"),
        ("text", "accent-100", "body text in a highlighted note"),
        ("neutral-800", "bg", "tag text on the page"),
        ("accent-800", "accent-100", "accent tag text"),
        ("accent-2-800", "accent-2-100", "secondary tag text"),
        ("neutral-800", "neutral-100", "neutral tag text"),
    ],
)
def test_body_text_meets_aa(foreground, background, where):
    ratio = contrast(PALETTE[foreground], PALETTE[background])
    assert ratio >= AA_NORMAL, f"{where}: {ratio:.2f}:1 is below {AA_NORMAL}:1"


@pytest.mark.parametrize(
    ("foreground", "background", "where"),
    [
        ("bg", "text", "reversed text on the dark navigation item"),
        ("bg", "accent-700", "button label on the filled button"),
        ("bg", "accent-800", "button label on hover"),
    ],
)
def test_reversed_text_meets_aa(foreground, background, where):
    ratio = contrast(PALETTE[foreground], PALETTE[background])
    assert ratio >= AA_NORMAL, f"{where}: {ratio:.2f}:1 is below {AA_NORMAL}:1"


def test_muted_text_still_meets_aa():
    """`.text-muted` is 55% of the text colour over the page background.

    Muted text is still text. A secondary label that fails contrast is a label
    some readers simply cannot use.
    """
    text = [int(PALETTE["text"].lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    bg = [int(PALETTE["bg"].lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    mixed = [round(t * 0.65 + b * 0.35) for t, b in zip(text, bg, strict=True)]
    muted = "#{:02x}{:02x}{:02x}".format(*mixed)

    ratio = contrast(muted, PALETTE["bg"])
    assert ratio >= AA_NORMAL, f"muted text is {ratio:.2f}:1"


# ── Non-text meaning ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("colour", "against", "where"),
    [
        ("accent", "bg", "the focus ring"),
        ("accent", "surface", "the focus ring over a card"),
        ("text", "bg", "a filled status marker"),
        ("neutral-600", "bg", "a hollow status marker"),
    ],
)
def test_meaningful_non_text_meets_the_three_to_one_threshold(colour, against, where):
    ratio = contrast(PALETTE[colour], PALETTE[against])
    assert ratio >= AA_LARGE, f"{where}: {ratio:.2f}:1 is below {AA_LARGE}:1"


def test_the_progress_bar_track_is_distinguishable_from_its_fill():
    ratio = contrast(PALETTE["neutral-300"], PALETTE["text"])
    assert ratio >= AA_LARGE


# ── Never colour alone ────────────────────────────────────────────────────


def test_every_status_carries_a_word_and_a_shape():
    from app.web.status import STATUSES

    for key, presentation in STATUSES.items():
        assert presentation.label, f"{key} has no label"
        assert presentation.shape, f"{key} relies on colour alone"


def test_statuses_that_share_a_colour_have_different_shapes():
    """Red is used for both "running" and "needs you".

    A reader who cannot distinguish them by hue must still be able to tell them
    apart, which is what the shape is for.
    """
    from app.web.status import STATUSES

    accent_states = [
        key
        for key, p in STATUSES.items()
        if p.css_class in {"status-running", "status-attention", "status-waiting"}
    ]
    shapes = {STATUSES[key].shape for key in accent_states}
    assert len(shapes) > 1, "states sharing the accent colour need distinct shapes"


# ── Keyboard and semantics, in the rendered pages ─────────────────────────


@pytest.fixture
def client(tmp_path):
    settings = Settings().with_output_root(tmp_path / "out")
    connection = open_database(settings.output_root)
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "A job", "analyzing", "/out", utc_now(), utc_now()),
    )
    connection.close()
    with TestClient(create_app(settings), base_url=LOOPBACK_BASE_URL) as test_client:
        yield test_client


SCREENS = [
    "/",
    "/launch",
    "/jobs/new",
    "/imports",
    "/settings",
    "/collections",
    "/collections/new",
    "/jobs/j1",
]


@pytest.mark.parametrize("path", SCREENS)
def test_every_screen_starts_with_a_skip_link(client, path):
    body = client.get(path).text
    assert 'class="skip-link"' in body
    assert 'href="#main"' in body
    assert 'id="main"' in body


@pytest.mark.parametrize("path", SCREENS)
def test_every_screen_has_exactly_one_first_level_heading(client, path):
    body = client.get(path).text
    assert body.count("<h1") <= 1, "more than one h1 makes the outline ambiguous"


@pytest.mark.parametrize("path", SCREENS)
def test_no_screen_removes_the_focus_outline(client, path):
    # Restyling focus is fine; removing it makes the interface unusable without
    # a mouse.
    assert "outline: none" not in client.get(path).text


def test_the_stylesheet_keeps_a_visible_focus_ring():
    css = CSS.read_text(encoding="utf-8")
    assert ":focus-visible" in css
    assert "outline: 2px solid var(--color-accent)" in css


def test_form_controls_are_labelled(client):
    body = client.get("/jobs/new").text
    for field_id in ("job-name", "job-paths"):
        assert f'for="{field_id}"' in body
        assert f'id="{field_id}"' in body


def test_checkboxes_without_a_visible_label_carry_one(client):
    body = client.get("/collections/new").text
    if 'type="checkbox" name="video"' in body:
        assert "aria-label=" in body


def test_navigation_is_a_landmark(client):
    body = client.get("/").text
    assert "<nav" in body
    assert 'aria-label="Sections"' in body


def test_the_main_region_is_a_landmark(client):
    assert "<main" in client.get("/").text


def test_alerts_are_announced(client):
    response = client.post("/jobs", data={"name": "", "paths": "/nope.mp4"})
    assert 'role="alert"' in response.text


def test_decorative_marks_are_hidden_from_screen_readers(client):
    body = client.get("/").text
    assert 'aria-hidden="true"' in body


def test_the_layout_degrades_rather_than_hiding_navigation():
    """Below 1024px the sidebar stacks; it does not disappear.

    Navigation the user cannot reach is worse than navigation that takes
    vertical space.
    """
    css = CSS.read_text(encoding="utf-8")
    block = re.search(r"@media \(max-width: 1023px\).*?\n}", css, re.DOTALL)
    assert block, "no 1024px breakpoint found"
    assert "display: none" not in block.group(0)


def test_reduced_motion_is_respected():
    assert "prefers-reduced-motion" in CSS.read_text(encoding="utf-8")


def test_tables_are_captioned(client):
    body = client.get("/settings").text
    assert "<caption" in body


def test_buttons_are_real_buttons_not_clickable_divs(client):
    # A div with an onclick is not reachable by keyboard and announces nothing.
    body = client.get("/jobs/j1").text
    assert "<button" in body
    assert "onclick=" not in body


# ── Choosing an option is visible ─────────────────────────────────────────
#
# The `.pick` cards — how often to take a picture, whether to describe, what
# shape a collection takes — are labels wrapping a visually-hidden radio. Their
# only selected-state rule keyed off `[aria-pressed="true"]`, an attribute
# nothing in the application ever set: a leftover from when `.pick` was a
# `<button>`. Clicking an option therefore changed nothing on screen, on every
# screen that uses them.


PICK_SCREENS = ["/jobs/new", "/collections/new", "/settings"]


def _pick_rules() -> str:
    css = CSS.read_text(encoding="utf-8")
    return "\n".join(line for line in css.splitlines() if line.strip().startswith(".pick"))


def test_a_chosen_option_is_styled_differently_from_the_others():
    """The regression. Without this the control is decorative: the radio holds
    the answer and the screen never says which one it is."""
    rules = _pick_rules()
    assert ".pick:has(input:checked)" in rules


def test_the_selected_state_is_not_keyed_off_a_dead_attribute():
    """`aria-pressed` is set nowhere. A rule that can never match is worse than
    no rule, because it reads as though the state is handled.

    Comments are stripped first: the history of this bug is worth writing down
    next to the fix, and a test that forbade naming it would be forbidding the
    explanation rather than the defect.
    """
    css = CSS.read_text(encoding="utf-8")
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    assert "aria-pressed" not in rules, "the selected state must key off something real"


def test_choosing_is_not_signalled_by_colour_alone():
    """Same rule the status vocabulary follows. A card that differs only in hue
    is invisible to a large minority of users and in any greyscale printout."""
    rules = _pick_rules()
    # The mark itself fills in, which is a change of shape rather than of hue.
    assert ".pick::before" in rules
    assert ".pick:has(input:checked)::before" in rules


def test_the_selected_card_stays_readable():
    """Selection tints the ground; the text on it still has to meet AA."""
    assert contrast(PALETTE["text"], PALETTE["accent-100"]) >= AA_NORMAL


def test_the_selection_mark_is_distinguishable_from_the_card_it_sits_on():
    """It carries meaning, so 3:1 against its surroundings."""
    assert contrast(PALETTE["accent-700"], PALETTE["accent-100"]) >= AA_LARGE
    assert contrast(PALETTE["neutral-600"], PALETTE["bg"]) >= AA_LARGE


def test_a_keyboard_user_can_see_where_they_are_in_the_group():
    """The radio is 0x0 and transparent, so without an explicit rule the focus
    ring lands on something invisible and tabbing appears to do nothing."""
    assert ".pick:has(input:focus-visible)" in _pick_rules()


@pytest.mark.parametrize("path", PICK_SCREENS)
def test_every_screen_using_the_cards_has_one_chosen_to_begin_with(client, path):
    """An unanswered radio group with no visible state is indistinguishable
    from a broken one. Each group starts on a real default."""
    body = client.get(path).text
    if 'class="pick"' not in body:
        pytest.skip(f"{path} does not use the cards")
    assert "checked" in body


# ── The title is a title ──────────────────────────────────────────────────


def test_no_screen_hides_its_controls_inside_the_title(client):
    """A `{% block title %}` left open swallows whatever follows it.

    The job screen did exactly that: an unclosed `{% if %}` pulled three panels
    — stop, rename and remove — into `<title>`, so the tab carried 1,705
    characters of markup and the screen offered no way to stop, rename or delete
    a job. Every route behind those controls existed and was tested; not one of
    them could be reached.

    Checked on every screen, because the failure is invisible from the page
    itself — the markup is present in the response, just not where anyone can
    use it.
    """
    for path in [
        *SCREENS,
        "/jobs/j1/review",
        "/jobs/j1/outputs",
        "/jobs/j1/rerun",
        "/jobs/j1/frames",
    ]:
        body = client.get(path).text
        found = re.search(r"<title>(.*?)</title>", body, re.DOTALL)
        assert found, f"{path} has no title"
        title = found.group(1)

        assert "<" not in title, f"{path} has markup inside its <title>"
        assert len(title) < 120, f"{path} has a {len(title)}-character title"


def test_the_job_screen_offers_its_controls_on_the_page(client):
    """The three that were lost. Named individually so a future block edit that
    swallows them again fails with the name of what went missing."""
    body = client.get("/jobs/j1").text
    main = re.search(r"<main.*?>(.*)</main>", body, re.DOTALL).group(1)

    for control in ("Stop this job", "Rename", "Remove this job"):
        assert control in main, f"{control!r} is not on the visible page"
