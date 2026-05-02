"""Downloaders sub-package for the Kharej VPS worker.

Each module in this package implements a platform-specific download adapter
that accepts a normalised job payload and returns a local file path ready for
upload to Arvan S2.
"""

from __future__ import annotations
