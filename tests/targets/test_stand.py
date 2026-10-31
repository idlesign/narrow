from types import SimpleNamespace

from narrow.targets import StandTarget

def test_builtin_target_keeps_existing_topology(run_environment: SimpleNamespace) -> None:
    target = StandTarget("nginx_ssl_static", None)

    metadata = target.prepare()

    with target.setup(timeout=1):
        target.check_running()

    assert metadata["ownership"] == "owned"
    assert "nginx" in metadata["servers"]
    assert metadata["subject"]["platform"] == "nginx"
    assert metadata["subject"]["artifact_sha256"]["server/nginx"] == metadata["servers"]["nginx"]["sha256"]
    assert target.ca_file.is_file()
    assert target.get_evidence()[0]["files"]["stdout.bin"] == b"server out"
    run_environment.handles[0].stop.assert_called_once()
