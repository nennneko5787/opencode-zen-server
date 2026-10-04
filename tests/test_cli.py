from __future__ import annotations

import pytest

from zen_free_proxy.__main__ import _env_for, build_parser, overrides_from
from zen_free_proxy.config import Settings


def parse(*argv: str):
    return build_parser().parse_args(argv)


class TestParser:
    def test_defaults_are_all_unset(self):
        args = parse()
        assert args.host is None
        assert args.port is None
        assert args.api_key is None
        assert args.allowed_client_keys is None
        assert args.reload is False

    def test_host_and_port(self):
        args = parse("--host", "127.0.0.1", "--port", "9999")
        assert overrides_from(args) == {"host": "127.0.0.1", "port": 9999}

    def test_port_must_be_a_number(self):
        with pytest.raises(SystemExit):
            parse("--port", "not-a-port")

    def test_empty_allowed_client_keys_means_open(self):
        assert overrides_from(parse("--allowed-client-keys", "")) == {"allowed_client_keys": ""}
        assert overrides_from(parse("--allowed-client-keys", "a, b")) == {"allowed_client_keys": "a, b"}

    def test_log_level_is_validated(self):
        assert overrides_from(parse("--log-level", "debug")) == {"log_level": "debug"}
        with pytest.raises(SystemExit):
            parse("--log-level", "shout")

    def test_base_url(self):
        assert overrides_from(parse("--base-url", "http://localhost:9999/v1")) == {
            "zen_base_url": "http://localhost:9999/v1"
        }


class TestPrecedence:
    def test_cli_beats_env(self, monkeypatch):
        monkeypatch.setenv("ZEN_PROXY_PORT", "1111")
        monkeypatch.setenv("ZEN_PROXY_HOST", "10.0.0.1")
        settings = Settings(**overrides_from(parse("--host", "127.0.0.1", "--port", "2222")))
        assert settings.host == "127.0.0.1"
        assert settings.port == 2222

    def test_env_used_when_cli_silent(self, monkeypatch):
        monkeypatch.setenv("ZEN_PROXY_PORT", "1111")
        assert Settings(**overrides_from(parse())).port == 1111

    def test_api_key_from_cli_and_env(self, monkeypatch):
        assert Settings(**overrides_from(parse("--api-key", "sk-cli"))).api_key == "sk-cli"
        monkeypatch.setenv("ZEN_API_KEY", "sk-env")
        assert Settings(**overrides_from(parse())).api_key == "sk-env"
        assert Settings(**overrides_from(parse("--api-key", "sk-cli"))).api_key == "sk-cli"

    def test_blank_api_key_becomes_anonymous(self):
        assert Settings(api_key="").api_key is None
        overrides = overrides_from(parse("--api-key", ""))
        assert overrides == {"api_key": ""}
        assert Settings(**overrides).api_key is None

    def test_allowed_client_keys_split(self):
        settings = Settings(**overrides_from(parse("--allowed-client-keys", "a, b ,c")))
        assert settings.allowed_client_keys == ["a", "b", "c"]
        assert settings.auth_required is True

    def test_env_handoff_for_reload_subprocess(self):
        env = _env_for(overrides_from(parse("--host", "127.0.0.1", "--port", "2222", "--api-key", "sk-x")))
        assert env == {"ZEN_PROXY_HOST": "127.0.0.1", "ZEN_PROXY_PORT": "2222", "ZEN_API_KEY": "sk-x"}
