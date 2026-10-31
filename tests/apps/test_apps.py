from importlib.metadata import version
from pathlib import Path
from unittest.mock import Mock

import pytest

from narrow import APPS
from narrow.apps.entrypoints.py_app import application


def test_default_app_and_entrypoints() -> None:
    assert next(iter(APPS)) == "py"

    for app_class in APPS.values():
        app = app_class()
        entrypoint_path = Path(app.get_entrypoint_path())
        assert entrypoint_path.is_file()
        assert entrypoint_path.name == f"{app.alias}_app.py"


@pytest.mark.parametrize(
    ("alias", "label"),
    [("bottle", "Bottle"), ("cherrypy", "CherryPy"), ("django", "Django"), ("flask", "Flask")],
)
def test_framework_version(alias: str, label: str) -> None:
    assert APPS[alias]().get_version() == f"{label} {version(alias)}"


def test_wsgi_response() -> None:
    start_response = Mock()

    response = application({}, start_response)

    assert response == [b"Hello there!"]
    start_response.assert_called_once_with(
        "200 OK", [("Content-Type", "text/plain"), ("Content-Length", "12")],
    )
