"""Synthetic configuration and exact enrollment boundaries; no source reads."""
import json
from pathlib import Path

import pytest

from actomasto.config import ConfigError, migration_config, validate


def configured(tmp_path):
    return {"version": 2, "discovery": {"roots": [str(tmp_path)]},
            "identity": {"author_emails": ["author@example.invalid"]},
            "funes": {"executable": str(tmp_path / "funes"), "corpus": str(tmp_path / "corpus"),
                      "scope": str(tmp_path / "scope.json")}}


@pytest.mark.parametrize("key,value", [("executable", "funes"), ("corpus", "https://example.invalid/memory"),
                                        ("scope", "~/scope.json"), ("executable", ["/bin/funes"])])
def test_local_source_rejects_implicit_or_remote_locations(tmp_path, key, value):
    config = configured(tmp_path)
    config["funes"][key] = value
    with pytest.raises(ConfigError):
        validate(config)


def test_v2_rejects_legacy_roots_and_does_not_default_source_enrollment(tmp_path):
    config = configured(tmp_path)
    config["sources"] = {"claude_root": "/synthetic"}
    with pytest.raises(ConfigError):
        validate(config)
    del config["sources"]
    del config["funes"]
    with pytest.raises(ConfigError):
        validate(config)
    config.update(version=1, sources={})
    with pytest.raises(ConfigError):
        validate(config)


def test_exact_migration_expands_home_and_canonicalizes_every_legacy_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = configured(tmp_path)
    legacy = {key: value for key, value in config.items() if key != "funes"}
    legacy.update(version=1, sources={"claude_root": "~/claude", "codex_root": "~/codex", "omp_root": "~/omp"})
    roots = {client: [str(tmp_path / client)] for client in ("claude", "codex", "omp")}
    scope = Path(config["funes"]["scope"])
    scope.write_text(json.dumps({"version": 1, "roots": roots}))
    migrated = migration_config(legacy, config["funes"])
    assert migrated["version"] == 2
    assert "sources" not in migrated
    assert migrated["discovery"] == validate(legacy, legacy=True)["discovery"]
    roots["codex"].append(str(tmp_path / "extra-codex"))
    scope.write_text(json.dumps({"version": 1, "roots": roots}))
    with pytest.raises(ConfigError, match="migration_scope_mismatch"):
        migration_config(legacy, config["funes"])
    roots.pop("omp")
    scope.write_text(json.dumps({"version": 1, "roots": roots}))
    with pytest.raises(ConfigError, match="invalid_funes_scope"):
        migration_config(legacy, config["funes"])
