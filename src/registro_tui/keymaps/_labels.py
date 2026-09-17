"""Human-readable rendering of binding keys.

The footer and the help modal both need to show a binding the way a user would
type it (``^↵``, not ``ctrl+enter``). Doing that in one place is what lets the
footer be *derived* from the keymap instead of restating it.
"""

from __future__ import annotations

# Keys whose display form is not simply the key name.
_SPECIAL: dict[str, str] = {
    "question_mark": "?",
    "colon": ":",
    "enter": "↵",
    "escape": "esc",
    "space": "␣",
}


def key_label(key: str) -> str:
    """Render a Textual binding key as the UI shows it.

    ``ctrl+enter`` → ``^↵``, ``question_mark`` → ``?``, ``shift+g`` → ``G``,
    and a chord such as ``g,g`` → ``gg``.
    """
    if "," in key:
        return "".join(key_label(part) for part in key.split(","))
    prefix = ""
    rest = key
    while True:
        if rest.startswith("ctrl+"):
            prefix += "^"
            rest = rest.removeprefix("ctrl+")
        elif rest.startswith("shift+"):
            # Shift shows as the upper-cased key itself: ``shift+g`` → ``G``.
            rest = rest.removeprefix("shift+").upper()
        else:
            break
    return prefix + _SPECIAL.get(rest, rest)
