from registro_tui.themes._base import Theme

# Nord. As with Monokai, ``accent`` is pulled off ``error`` (both were
# #bf616a) — accent takes Nord's frost purple, error keeps aurora red.
NORD = Theme(
    name="nord",
    background="#2e3440",
    foreground="#d8dee9",
    surface="#3b4252",
    primary="#88c0d0",
    accent="#b48ead",
    success="#a3be8c",
    warning="#ebcb8b",
    error="#bf616a",
)
