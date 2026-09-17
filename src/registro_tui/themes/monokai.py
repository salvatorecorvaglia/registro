from registro_tui.themes._base import Theme

# Monokai. ``accent`` and ``error`` were both #f92672, which made an accent
# border and an error border indistinguishable; accent moves to Monokai's
# orange so the two read differently while staying in palette.
MONOKAI = Theme(
    name="monokai",
    background="#272822",
    foreground="#f8f8f2",
    surface="#3e3d32",
    primary="#66d9ef",
    accent="#fd971f",
    success="#a6e22e",
    warning="#e5c07b",
    error="#f92672",
)
