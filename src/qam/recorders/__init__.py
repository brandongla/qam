"""Forward recorders for venue market data (DESIGN.md §11.8, D-025).

Recorders are deliberately thin: they subscribe, keep connections alive, and write every
received message verbatim to the raw archive with receive timestamps and connection metadata.
They do not parse data into canonical schemas. That happens offline in versioned
normalizers, so a parsing bug can never destroy data that can't be re-collected.
"""
