from .py import PurePy  # This should be first as default

# isort: split

from ._base import APPS
from .bottle import Bottle
from .cherrypy import Cherrypy
from .django import Django
from .flask import Flask

__all__ = ["APPS", "Bottle", "Cherrypy", "Django", "Flask", "PurePy"]
