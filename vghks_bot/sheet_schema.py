"""Compatibility import; canonical implementation lives in vghks-sheet-core."""
import sys

from vghks_sheet_core import schema as _implementation

sys.modules[__name__] = _implementation
