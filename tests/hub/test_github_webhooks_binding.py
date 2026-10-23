from unittest.mock import Mock

from scripts.hub import github_webhooks


def test_receiver_binds_existing_bus_resources(monkeypatch):
    monkeypatch.setenv(github_webhooks.SECRET_ENV, "fixture")
    bus = Mock()
    opened = Mock(return_value=bus)
    monkeypatch.setattr(github_webhooks, "open_bus", opened)
    monkeypatch.setattr(github_webhooks, "ThreadingHTTPServer", Mock())
    github_webhooks.main([])
    config = opened.call_args.args[0]
    assert config.provision is False
    bus.close.assert_called_once_with()


def test_receiver_listens_before_binding_any_bucket(monkeypatch):
    monkeypatch.setenv(github_webhooks.SECRET_ENV, "fixture")
    bus = Mock()
    bus.state.side_effect = LookupError("bucket not found")
    server = Mock()
    monkeypatch.setattr(github_webhooks, "open_bus", Mock(return_value=bus))
    monkeypatch.setattr(github_webhooks, "ThreadingHTTPServer", server)
    github_webhooks.main([])
    server.return_value.serve_forever.assert_called_once_with()
    bus.state.assert_not_called()
