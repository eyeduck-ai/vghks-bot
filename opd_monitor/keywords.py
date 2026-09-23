"""Compatibility import for v2 integrations; use opd_monitor.tags."""
from .tags import extract_tags as extract_keywords

__all__ = ["extract_keywords"]
