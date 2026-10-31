from unittest.mock import Mock

import pytest

from narrow.targets import UrlTarget, command

def test_external_url_does_not_own_or_probe_a_server(monkeypatch: pytest.MonkeyPatch) -> None:
    start = Mock(side_effect=AssertionError("foreign server must not be touched"))
    monkeypatch.setattr(command, "ProcessHandle", start)
    target = UrlTarget("https://example.test/api?token=PRIVATE", name="external")

    metadata = target.prepare()

    with target.setup(timeout=1):
        target.check_running()

    assert metadata["ownership"] == "external"
    assert "PRIVATE" not in repr(metadata)
    assert target.get_evidence() == []
    start.assert_not_called()
