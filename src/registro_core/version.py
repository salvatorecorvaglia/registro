"""Distribution version, resolved from installed package metadata.

This lives outside ``registro_core/__init__.py`` on purpose. Every Registro
package imports ``registro_core``, and ``importlib.metadata`` costs ~15ms to
import, so resolving the version there would put that on the CLI's startup
path. Only the ``version`` command and the plugin compatibility check need it,
and they import this module directly.
"""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

# The *distribution* name, which is what ``importlib.metadata`` indexes by. It
# differs from the import packages (``registro_core`` & co.) and from the
# command name; looking up ``"registro"`` after the rename to ``registro-db``
# always missed, so every install silently reported the fallback below.
DISTRIBUTION = "registro-db"

try:
    __version__ = version(DISTRIBUTION)
except PackageNotFoundError:  # pragma: no cover - only without installed metadata
    __version__ = "1.1.0"  # fallback for source trees without metadata
