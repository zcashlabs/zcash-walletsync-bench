"""Zcash Labs brand files for the generated pages."""

from __future__ import annotations

import pathlib
import shutil

ASSETS = pathlib.Path(__file__).parent / "site_assets"
FILES = ("mark.svg", "favicon.svg")


def copy_brand(out: pathlib.Path) -> None:
    """Put the mark and the favicon next to a page."""
    for name in FILES:
        shutil.copy(ASSETS / name, out / name)
