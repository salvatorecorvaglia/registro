"""Shared behaviour for Registro's modal screens.

Every modal in the app answers the same two keys — ``escape`` cancels,
``enter`` submits — and each one used to hand-roll that, which is how they
drifted apart (only one of them, for instance, deferred ``enter`` to a focused
list). Subclasses implement :meth:`submit` and inherit the rest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from textual.screen import ModalScreen

if TYPE_CHECKING:
    from textual.await_complete import AwaitComplete
    from textual.events import Key

ResultT = TypeVar("ResultT")


class BaseModal(ModalScreen[ResultT | None]):
    """Modal that cancels on ``escape`` and submits on ``enter``."""

    def __init__(self) -> None:
        super().__init__()
        self._resolved = False

    def submit(self) -> None:
        """Resolve the modal with a value. Subclasses must override."""
        raise NotImplementedError

    def cancel(self) -> None:
        self.dismiss(None)

    def dismiss(self, result: ResultT | None = None) -> AwaitComplete:
        """Resolve the modal, at most once.

        Textual can deliver a single key press to this screen twice: the
        focused widget receives it and bubbles it up, and the screen is also
        offered it directly. ``escape`` therefore reached :meth:`cancel` a
        second time *after* the screen had already been popped, and the second
        ``dismiss`` raised ``ScreenStackError`` off an empty stack — crashing
        the app on the connection dialog, which is the first thing a user sees
        when ``registro tui`` is started without a DSN.

        Guarding here rather than in ``on_key`` covers every route to a
        double resolution, including a subclass's ``submit`` being reached from
        both a list selection and the ``enter`` key.
        """
        if self._resolved:
            return self._nothing()
        self._resolved = True
        return super().dismiss(result)

    def _nothing(self) -> AwaitComplete:
        """An already-completed awaitable, for a dismissal with nothing to do."""
        from textual.await_complete import AwaitComplete  # noqa: PLC0415

        return AwaitComplete()

    def handles_enter_itself(self) -> bool:
        """True when the focused widget should get ``enter`` instead.

        A focused list, for example, turns ``enter`` into its own selection
        event, and intercepting it here would submit twice.
        """
        return False

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            event.stop()
            self.cancel()
        elif event.key == "enter" and not self.handles_enter_itself():
            event.stop()
            self.submit()
