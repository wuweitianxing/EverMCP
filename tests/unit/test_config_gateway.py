"""Unit tests for the [gateway] section in evermcp.security.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from evermcp.security.config import Config, GatewayConfig

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestGatewayDefaults:
    def test_default_host(self) -> None:
        cfg = Config()
        assert cfg.gateway.host == "127.0.0.1"

    def test_default_port(self) -> None:
        cfg = Config()
        assert cfg.gateway.port == 8787

    def test_default_require_key_false(self) -> None:
        cfg = Config()
        assert cfg.gateway.require_key is False

    def test_gatewayconfig_direct_defaults(self) -> None:
        g = GatewayConfig()
        assert g.host == "127.0.0.1"
        assert g.port == 8787
        assert g.require_key is False


# ---------------------------------------------------------------------------
# TOML overrides
# ---------------------------------------------------------------------------


class TestGatewayTomlOverrides:
    def test_toml_overrides_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[gateway]\nhost = "0.0.0.0"\nport = 9000\nrequire_key = true\n',
            encoding="utf-8",
        )

        cfg = Config.load(config_file=str(config_file))

        assert cfg.gateway.host == "0.0.0.0"
        assert cfg.gateway.port == 9000
        assert cfg.gateway.require_key is True

    def test_toml_partial_overrides_only_set_fields(self, tmp_path: Path) -> None:
        """Only fields present in TOML are overridden; the rest keep defaults."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[gateway]\nport = 9001\n",
            encoding="utf-8",
        )

        cfg = Config.load(config_file=str(config_file))

        assert cfg.gateway.port == 9001
        # host / require_key keep their defaults
        assert cfg.gateway.host == "127.0.0.1"
        assert cfg.gateway.require_key is False


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------


class TestGatewayEnvOverrides:
    def test_env_overrides_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env vars take precedence over TOML (same precedence as other fields)."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[gateway]\nhost = "0.0.0.0"\nport = 9000\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("EVERMCP_GATEWAY_HOST", "10.0.0.1")
        monkeypatch.setenv("EVERMCP_GATEWAY_PORT", "12345")

        cfg = Config.load(config_file=str(config_file))

        assert cfg.gateway.host == "10.0.0.1"
        assert cfg.gateway.port == 12345

    def test_env_require_key_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_GATEWAY_REQUIRE_KEY", "true")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.gateway.require_key is True

    def test_env_require_key_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_GATEWAY_REQUIRE_KEY", "yes")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.gateway.require_key is True

    def test_env_require_key_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_GATEWAY_REQUIRE_KEY", "on")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.gateway.require_key is True

    def test_env_require_key_one_is_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EVERMCP_GATEWAY_REQUIRE_KEY=1 must be coerced to True (per spec)."""
        monkeypatch.setenv("EVERMCP_GATEWAY_REQUIRE_KEY", "1")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.gateway.require_key is True

    def test_env_require_key_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EVERMCP_GATEWAY_REQUIRE_KEY=false must yield False (per spec)."""
        monkeypatch.setenv("EVERMCP_GATEWAY_REQUIRE_KEY", "false")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.gateway.require_key is False

    def test_env_host_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_GATEWAY_HOST", "10.0.0.5")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.gateway.host == "10.0.0.5"
        # other fields keep defaults
        assert cfg.gateway.port == 8787
        assert cfg.gateway.require_key is False

    def test_env_port_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVERMCP_GATEWAY_PORT", "5555")
        cfg = Config()
        cfg = Config._apply_env(cfg)
        assert cfg.gateway.port == 5555


# ---------------------------------------------------------------------------
# Repr / round-trip
# ---------------------------------------------------------------------------


class TestGatewayRepr:
    def test_gatewayconfig_repr_does_not_raise(self) -> None:
        g = GatewayConfig()
        # Must not raise — used by Config.__repr__ and logging paths.
        text = repr(g)
        assert "GatewayConfig" in text
        assert "host=" in text
        assert "port=" in text
        assert "require_key=" in text

    def test_gatewayconfig_repr_with_custom_values(self) -> None:
        g = GatewayConfig(host="0.0.0.0", port=9000, require_key=True)
        text = repr(g)
        assert "0.0.0.0" in text
        assert "9000" in text
        assert "True" in text

    def test_config_repr_includes_gateway(self) -> None:
        cfg = Config()
        text = repr(cfg)
        assert "gateway=" in text
        assert "GatewayConfig" in text
