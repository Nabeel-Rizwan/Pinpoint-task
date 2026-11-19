"""Pinpoint Partners — corporate & M&A partner intelligence.

A small, production-shaped pipeline that keeps a current, de-duplicated,
historical view of the corporate / M&A partners across a configurable set of
law firms, and surfaces *what changed* between refreshes.

The package is intentionally dependency-free (Python standard library only) so
a reviewer can run it with nothing more than a Python 3.10+ interpreter.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
