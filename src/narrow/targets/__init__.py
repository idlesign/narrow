"""Targets and explicit identities for built-in, managed and external platforms."""

from .base import Target
from .command import CommandTarget
from .stand import StandTarget
from .subjects import SubjectSpec
from .url import UrlTarget

__all__ = ["CommandTarget", "StandTarget", "SubjectSpec", "Target", "UrlTarget"]
