"""The factory's status words and shapes: one owner for how a state reads on every page and in every terminal line.

A state is always a word plus a shape, never colour alone (CLAUDE.md rule 6, the principal is red-green
colour-deficient): the shape is a glyph in plain text and an icon on a page, and the colour (a tone) only adds to
them. Pages (the Factory Desk, the costs page) take `mark()` and the stylesheet `CSS`; terminal output takes `text()`.

States:

    done · failed · interrupted · skipped     how a team run ended (engine.models.Outcome)
    running · continued                        a team run open now; one that went on as a new run
    active · paused                            a team's schedule
    within · approaching · near · over         a budget against its limit (F-33's states, same keys)
    stopped · degraded                         the engine as a whole (with running: its health)
    current · draining · drained               a build: new runs go to it; open runs finish on it; nothing left
    unknown                                    not known (a read failed, a state not seen before)
"""

from __future__ import annotations

import html
from dataclasses import dataclass

NEUTRAL, INFO, ATTENTION, NEGATIVE = "neutral", "info", "attention", "negative"  # tones, in rising urgency

# Icon bodies for a 24×24, stroke-drawn SVG (round caps and joins, no fill).
_CIRCLE = '<circle cx="12" cy="12" r="10"/>'
_CHECK = _CIRCLE + '<path d="m9 12 2 2 4-4"/>'
_CROSS = _CIRCLE + '<path d="m15 9-6 6"/><path d="m9 9 6 6"/>'
_TRIANGLE = ('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>'
             '<path d="M12 9v4"/><path d="M12 17h.01"/>')
_MOON = '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>'
_SKIP = _CIRCLE + '<path d="M8 12h8"/><path d="m12 8 4 4-4 4"/>'
_ONWARD = '<path d="m15 10 5 5-5 5"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/>'
_PLAY = _CIRCLE + '<path d="m10 8 6 4-6 4Z"/>'
_CLOCK = _CIRCLE + '<path d="M12 6v6l4 2"/>'
_PAUSE = _CIRCLE + '<path d="M10 15V9"/><path d="M14 15V9"/>'
_STOP = _CIRCLE + '<path d="M9 9h6v6H9Z"/>'
_DOT = _CIRCLE + '<circle cx="12" cy="12" r="3"/>'
_HOURGLASS = '<path d="M6 2h12"/><path d="M6 22h12"/><path d="M7 2v4l5 6 5-6V2"/><path d="M7 22v-4l5-6 5 6v4"/>'
_QUESTION =_CIRCLE + '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>'


@dataclass(frozen=True)
class Status:
    key: str
    word: str  # the default word; a page may say more ("Paused after 3 failures"), never less
    glyph: str  # the shape in plain text
    tone: str
    icon: str  # the shape on a page: an SVG body


STATUSES: dict[str, Status] = {s.key: s for s in (
    Status("done", "Done", "✓", NEUTRAL, _CHECK),
    Status("failed", "Failed", "✕", NEGATIVE, _CROSS),
    Status("interrupted", "Interrupted", "☾", ATTENTION, _MOON),  # the machine slept through the run's budget
    Status("skipped", "Skipped", "»", NEUTRAL, _SKIP),
    Status("running", "Running", "▶", INFO, _PLAY),
    Status("continued", "Continued", "↪", NEUTRAL, _ONWARD),  # continued as new: its wakes went on in the next run
    Status("active", "Active", "◷", NEUTRAL, _CLOCK),
    Status("paused", "Paused", "‖", ATTENTION, _PAUSE),
    Status("within", "Within budget", "✓", NEUTRAL, _CHECK),
    Status("approaching", "Approaching", "!", ATTENTION, _TRIANGLE),
    Status("near", "Near limit", "!", NEGATIVE, _TRIANGLE),
    Status("over", "Over limit", "✕", NEGATIVE, _CROSS),
    Status("stopped", "Stopped", "■", NEGATIVE, _STOP),
    Status("degraded", "Degraded", "!", ATTENTION, _TRIANGLE),
    Status("current", "Current", "●", INFO, _DOT),
    Status("draining", "Draining", "⧗", NEUTRAL, _HOURGLASS),
    Status("drained", "Drained", "○", NEUTRAL, _CIRCLE),
    Status("unknown", "Unknown", "?", NEUTRAL, _QUESTION),
)}


def status(key: str) -> Status:
    """The state's status; an unseen key reads as unknown rather than failing a page."""
    return STATUSES.get(key, STATUSES["unknown"])


def text(key: str, word: str | None = None) -> str:
    """Plain text: glyph and word, e.g. `✕ Failed`."""
    s = status(key)
    return f"{s.glyph} {word or s.word}"


def mark(key: str, word: str | None = None) -> str:
    """HTML: the icon and the word (escaped), toned. The icon is decorative to a screen reader, the word is not."""
    s = status(key)
    return (f'<span class="mark {s.tone}" data-state="{html.escape(s.key)}"><svg viewBox="0 0 24 24" aria-hidden="true">'
            f"{s.icon}</svg><span>{html.escape(word or s.word)}</span></span>")


# The shared tokens (light and dark) and the mark's rules; a page appends its own layout. Tones are amber and
# crimson, which stay apart for red-green colour deficiency, and each always comes with its shape and word.
CSS = """
:root{--bg:hsl(0 0% 100%);--card:hsl(0 0% 99%);--fg:hsl(0 0% 11%);--muted:hsl(0 0% 36%);--border:hsl(0 0% 90%);
--track:hsl(0 0% 93%);--ok:hsl(0 0% 11% / .55);--warn:hsl(36 92% 40%);--danger:hsl(345 70% 46%);--ink:hsl(213 53% 45%);
--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:hsl(0 0% 9%);--card:hsl(0 0% 12%);--fg:hsl(0 0% 98%);
--muted:hsl(0 0% 68%);--border:hsl(0 0% 20%);--track:hsl(0 0% 18%);--ok:hsl(0 0% 98% / .55);--warn:hsl(38 92% 55%);
--danger:hsl(345 72% 63%);--ink:hsl(213 60% 62%)}}
.mark{display:inline-flex;align-items:center;gap:4px;font-size:12px}
.mark svg{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.mark.neutral{color:var(--muted)}.mark.info{color:var(--ink)}.mark.attention{color:var(--warn)}
.mark.negative{color:var(--danger);font-weight:600}
"""
