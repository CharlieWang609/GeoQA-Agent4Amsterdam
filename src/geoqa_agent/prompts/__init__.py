# SPDX-License-Identifier: GPL-3.0-only

"""Versioned model instruction resources."""

from __future__ import annotations

from importlib import resources


def load_prompt(file_reference: str) -> str:
    """Load a versioned prompt from the installed package."""
    content = resources.files(__package__).joinpath(file_reference).read_text(
        encoding="utf-8"
    )
    return content.removesuffix("\n")
