import copy

import pytest

from actomasto.policy import Policy, PolicyError, glob_match


def unit(*texts, paths=(), kind="conversation", repository="github.com/demo/public"):
    return {"id": "unit", "repository_id": "github.com:1", "repository_display": repository,
            "kind": kind, "paths": list(paths), "items": [
                {"id": f"message-{index}", "text": text, "provenance": "user_reported"}
                for index, text in enumerate(texts)]}


def test_whole_unit_blocklist_runs_before_redaction_and_does_not_mutate_source():
    original = unit("An otherwise safe update.", "password=forbidden-value")
    before = copy.deepcopy(original)
    policy = Policy({"blocklist": {"text": ["forbidden-value"]}})
    assert policy.filter(original) is None
    assert policy.last_reason == "blocked_text"
    assert original == before
    assert Policy({}).filter(original)["items"][1]["text"] == "password=[REDACTED_SECRET]"


def test_unicode_casefold_and_scoped_repository_rules():
    policy = Policy({"blocklist": {"scoped": [{"repository": "github.com/demo/*", "text": ["STRASSE"], "paths": ["internal/**"]}]}})
    assert policy.filter(unit("Straße details")) is None
    assert policy.filter(unit("Straße details", repository="gitlab.com/demo/public")) is not None
    assert policy.filter(unit("safe", paths=["internal/change.py"], kind="commit")) is None


def test_root_anchored_posix_globs_distinguish_components():
    assert glob_match("src/module/file.py", "src/**/file.py")
    assert glob_match("src/file.py", "src/**/file.py")
    assert not glob_match("src/module/file.py", "src/*.py")
    assert not glob_match("SRC/file.py", "src/*.py")
    assert glob_match("src/a.py", "src/?.py")
    assert not glob_match("src/ab.py", "src/?.py")


@pytest.mark.parametrize("path", [".env", "config/.env.production", ".dev.vars", ".secrets", "config/cert.pem", "config/signing.key", "credentials.json", "config/service-account.json"])
def test_sensitive_paths_exclude_entire_commit_including_message(path):
    policy = Policy({})
    assert policy.filter(unit("Safe commit message", "safe diff", paths=["src/a.py", path], kind="commit")) is None
    assert policy.last_reason == "blocked_path"


def test_rename_old_path_is_also_blocked_and_templates_still_scanned():
    policy = Policy({"blocklist": {"paths": ["old/private/**"]}})
    assert policy.filter(unit("Renamed file", paths=["old/private/file.py", "src/file.py"], kind="commit")) is None
    safe = Policy({}).filter(unit("token='sample-value'", paths=[".env.example"], kind="commit"))
    assert safe["items"][0]["text"] == "token=[REDACTED_SECRET]"


def test_service_account_schema_is_sensitive_even_with_innocent_filename():
    assert Policy({}).filter(unit('+  "type": "service_account",', paths=["sample.json"], kind="commit")) is None


@pytest.mark.parametrize("secret_text", [
    "-----BEGIN PRIVATE KEY-----\nSYNTHETIC_KEY_MATERIAL\n-----END PRIVATE KEY-----",
    "sk-ant-api03-" + "Ab9_" * 20,
    "ghp_" + "A1b2" * 10,
    "glpat-" + "A1b2" * 8,
    "https://synthetic:sample-password@example.invalid/path",
    "https://synthetic-token@example.invalid/path",
    "Authorization: Bearer synthetic-value",
    'api_key = "synthetic value with spaces"',
    "password: 'synthetic value'",
    "access_token=synthetic-unquoted",
    "d7Pz4xQ9rT2vB8nK5mY1aC6hE3sF0uLw",
])
def test_mandatory_secret_formats_redact_and_revalidation_is_idempotent(secret_text):
    policy = Policy({})
    source = unit("Observed credential: " + secret_text)
    filtered = policy.filter(source)
    assert filtered is not None
    text = filtered["items"][0]["text"]
    assert "[REDACTED_SECRET]" in text
    assert secret_text not in text
    assert policy.filter(filtered) == filtered


def test_conversation_absolute_paths_removed_without_scrubbing_web_urls():
    policy = Policy({})
    filtered = policy.filter(unit("Read /home/sample/private/file.txt and C:\\Users\\sample\\secret.txt; see https://example.invalid/docs."))
    text = filtered["items"][0]["text"]
    assert "/home/sample" not in text
    assert "C:\\Users" not in text
    assert text.count("[REDACTED_PATH]") == 2
    assert "https://example.invalid/docs." in text
    assert policy.filter(filtered) == filtered


def test_detector_failure_blocks_even_ordinary_text(monkeypatch):
    policy = Policy({})
    def broken():
        raise RuntimeError("synthetic detector failure")
    monkeypatch.setattr(policy, "_detectors", broken)
    assert policy.filter(unit("A safe cache update")) is None
    assert policy.last_reason == "detector_failure"
    with pytest.raises(PolicyError, match="detector_failure"):
        policy.output("A safe cache update", "github.com/demo/public")


def test_no_detector_network_verification(monkeypatch):
    import socket
    import requests
    def forbidden(*args, **kwargs):
        raise AssertionError("network verification is forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    assert Policy({}).filter(unit("password=synthetic-value"))["items"][0]["text"] == "password=[REDACTED_SECRET]"


def test_generated_secrets_paths_and_blocklisted_text_reject_whole_output():
    policy = Policy({"blocklist": {"text": ["blocked topic"]}})
    for text in ("I learned a blocked topic.", "I used token=synthetic-value", "I edited /home/sample/private.txt"):
        with pytest.raises(PolicyError):
            policy.output(text, "github.com/demo/public")
    assert policy.output("I fixed cache invalidation.", "github.com/demo/public") == "I fixed cache invalidation."


def test_empty_rules_are_rejected_not_universal_matches():
    with pytest.raises(PolicyError, match="invalid_blocklist"):
        Policy({"blocklist": {"text": [""]}})


def test_size_bound_excludes_whole_source_without_partial_redaction(monkeypatch):
    monkeypatch.setattr("actomasto.policy.MAX_UNIT_BYTES", 10)
    policy = Policy({})
    assert policy.filter(unit("123456", "abcdef")) is None
    assert policy.last_reason == "oversized_source"


def test_preexisting_redaction_marker_cannot_shield_an_adjacent_secret():
    policy = Policy({})
    filtered = policy.filter(unit('password="[REDACTED_SECRET] synthetic-password"'))
    assert filtered["items"][0]["text"] == "password=[REDACTED_SECRET]"
    assert policy.filter(filtered) == filtered
