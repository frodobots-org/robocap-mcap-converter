from robocap_to_mcap import container_cli


def test_container_cli_dispatches_local(monkeypatch):
    monkeypatch.setattr(container_cli, "local_main", lambda args: 17 if args == ["input"] else 1)
    assert container_cli.main(["local", "input"]) == 17


def test_container_cli_dispatches_s3(monkeypatch):
    monkeypatch.setattr(container_cli, "s3_main", lambda args: 23 if args == ["--json"] else 1)
    assert container_cli.main(["s3", "--json"]) == 23


def test_container_cli_version(capsys):
    assert container_cli.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.2.0"


def test_container_cli_rejects_unknown_command(capsys):
    assert container_cli.main(["unknown"]) == 2
    assert "Unknown command" in capsys.readouterr().err
