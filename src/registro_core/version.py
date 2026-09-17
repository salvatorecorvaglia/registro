"""Distribution version, resolved from installed package metadata.

This lives outside ``registro_core/__init__.py`` on purpose. Every Registro
package imports ``registro_core``, and ``importlib.metadata`` costs ~15ms to
import, so resolving the version there would put that on the CLI's startup
path. Only the ``version`` command and the plugin compatibility check need it,
and they import this module directly.
"""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("registro")
except PackageNotFoundError:  # pragma: no cover - only without installed metadata
    __version__ = "1.0.0"  # fallback for source trees without metadata
