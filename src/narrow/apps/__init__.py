from ._base import APPS
from .py import PurePy  # This should be first as default
from .bottle import Bottle
from .cherrypy import Cherrypy
from .django import Django
from .flask import Flask
