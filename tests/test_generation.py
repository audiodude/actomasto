"""Deterministic generation acceptance tests: real Store, no provider network."""
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from actomasto.generation import Generator, MAX_INPUT_TOKENS, MODEL
from actomasto.policy import Policy
from actomasto.store import Store

NOW = 1_780_000_000.0
REPOSITORY = "github.com:17"
ORIGIN = "github.com/example/public-project"


def config():
    return {
        "discovery": {"roots": ["/public"]},
        "identity": {"author_emails": ["author@example.org"]},
        "version": 2,
        "generation": {"model": MODEL, "character_limit": 500, "interval_minutes": 30},
        "budget": {"monthly_usd": "20.00", "timezone": "UTC"},
        "blocklist": {"repositories": [], "paths": [], "text": [], "scoped": []},
        "notifications": {"enabled": False},
    }


def unit(name="one", texts=None):
    texts = texts or ["I improved the public parser's handling of empty input."]
    return {
        "id": hashlib.sha256(name.encode()).hexdigest(), "repository_id": REPOSITORY,
        "repository_display": ORIGIN, "kind": "commit", "event_time": NOW - 60,
        "event_end": NOW - 59, "equivalent_id": None,
        "source_ref": {"commit": hashlib.sha1(name.encode()).hexdigest()},
        "paths": ["parser.py"], "adapter": "git", "adapter_version": "1",
        "partial_source": False,
        "items": [{"id": f"{name}-{index}", "text": text, "provenance": "committed",
                   "event_time": NOW - 60, "source_ref": {"commit": hashlib.sha1(name.encode()).hexdigest(),
                                                            "path": "parser.py"}}
                  for index, text in enumerate(texts)],
    }


@pytest.fixture
def prepared(tmp_path):
    store = Store(tmp_path)
    cfg = config()
    scope = tmp_path / 'scope.json'
    scope.write_text(json.dumps({'version': 1, 'roots': {name: [] for name in ('claude', 'codex', 'omp')}}))
    scope.chmod(0o600)
    cfg['funes'] = {'executable': '/missing/funes', 'corpus': str(tmp_path / 'corpus'), 'scope': str(scope)}
    store.apply_config(cfg, NOW)
    store.set_enabled(True, NOW)
    store.sync_repositories(
        [{"path": "/public/project", "origin": ORIGIN, "host": "github.com",
          "project_path": "example/public-project", "reason": None}],
        {ORIGIN: {"state": "public", "id": REPOSITORY, "retry_after": 0, "reason": None}}, NOW)
    yield store, cfg
    store.close()


def enqueue(store, cfg, source=None):
    source = source or unit()
    filtered = Policy(cfg).filter(source)
    assert filtered is not None
    assert store.enqueue(filtered, NOW)
    return filtered


def message(candidates=None, usage=None):
    return httpx.Response(200, json={
        "model": MODEL, "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps({"candidates": candidates or []})}],
        "usage": usage if usage is not None else {"input_tokens": 80, "output_tokens": 20},
    })


def generator(store, cfg, handler, visibility=None, clock=None):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return Generator(store, cfg, Policy(cfg), visibility or (lambda _: True),
                     api_key="synthetic-test-key", client=client, clock=clock)


def test_empty_and_zero_candidates_are_terminal_without_replay(prepared):
    store, cfg = prepared
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"input_tokens": 100}) if request.url.path.endswith("count_tokens") else message()

    gen = generator(store, cfg, handler)
    assert gen.cycle(NOW)["generation_requests"] == 0
    assert calls == []
    source = enqueue(store, cfg)
    assert gen.cycle(NOW + 1)["processed"] == 1
    assert store.seen(source)
    assert not store.pending(NOW + 2)
    assert store.list_suggestions() == []
    assert not store.enqueue(source, NOW + 3)
    gen.cycle(NOW + 4)
    assert calls == ["/v1/messages/count_tokens", "/v1/messages"]


def test_one_bad_candidate_rejects_entire_response_and_only_one_malformed_retry(prepared):
    store, cfg = prepared
    source = enqueue(store, cfg)
    candidates = [{"text": "I improved the parser.", "evidence_ids": [source["items"][0]["id"]]},
                  {"text": "I invented this claim.", "evidence_ids": ["not-in-request"]}]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"input_tokens": 100}) if request.url.path.endswith("count_tokens") else message(candidates)

    gen = generator(store, cfg, handler)
    gen.cycle(NOW + 1)
    assert store.list_suggestions() == []
    assert len(store.pending(NOW + 62)) == 1
    gen.cycle(NOW + 62)
    gen.cycle(NOW + 2_000)
    assert calls.count("/v1/messages") == 2
    assert store.list_suggestions() == []
    assert store.seen(source)
    assert not store.pending(NOW + 2_001)


def test_unknown_usage_consumes_reservation_and_blocks_unaffordable_retry(prepared):
    store, cfg = prepared
    cfg = copy.deepcopy(cfg)
    cfg["budget"]["monthly_usd"] = "0.011"
    store.apply_config(cfg, NOW)
    enqueue(store, cfg)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        raise httpx.ReadTimeout("synthetic transport failure", request=request)

    gen = generator(store, cfg, handler)
    gen.cycle(NOW + 1)
    gen.cycle(NOW + 62)
    assert calls.count("/v1/messages") == 1
    assert store.settings()["budget_paused"]
    assert store.list_suggestions() == []


def test_actual_usage_releases_unspent_reservation(prepared):
    store, cfg = prepared
    cfg = copy.deepcopy(cfg)
    cfg["budget"]["monthly_usd"] = "0.011"
    store.apply_config(cfg, NOW)
    enqueue(store, cfg, unit("first"))
    enqueue(store, cfg, unit("second"))
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"input_tokens": 100}) if request.url.path.endswith("count_tokens") else message()

    generator(store, cfg, handler).cycle(NOW + 1)
    assert calls.count("/v1/messages") == 2
    assert not store.settings()["budget_paused"]
    assert not store.pending(NOW + 2)


@pytest.mark.parametrize("action", ["off", "purge"])
def test_late_response_cannot_resurrect_suggestions(prepared, action):
    store, cfg = prepared
    source = enqueue(store, cfg)

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        if action == "off":
            store.set_enabled(False, NOW + 1)
        else:
            store.purge(REPOSITORY, NOW + 1)
        return message([{"text": "I improved the parser.", "evidence_ids": [source["items"][0]["id"]]}])

    result = generator(store, cfg, handler).cycle(NOW + 1)
    assert result["candidates"] == 0
    assert store.list_suggestions() == []
    if action == "purge":
        assert not store.pending(NOW + 2)


def test_visibility_rechecked_before_each_split_count(prepared):
    store, cfg = prepared
    enqueue(store, cfg, unit(texts=["First safe change", "Second safe change"]))
    calls, visibility_calls = [], []

    def visibility(repository):
        visibility_calls.append(repository)
        return len(visibility_calls) == 1

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"input_tokens": MAX_INPUT_TOKENS + 1})

    generator(store, cfg, handler, visibility).cycle(NOW + 1)
    assert calls == ["/v1/messages/count_tokens"]
    assert len(visibility_calls) == 2
    assert len(store.pending(NOW + 2)) == 1


def test_off_during_count_prevents_billable_dispatch(prepared):
    store, cfg = prepared
    enqueue(store, cfg)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        store.set_enabled(False, NOW + 1)
        return httpx.Response(200, json={"input_tokens": 100})

    generator(store, cfg, handler).cycle(NOW + 1)
    assert calls == ["/v1/messages/count_tokens"]
    assert store.list_suggestions() == []


@pytest.mark.parametrize("target", ["/v1/messages/count_tokens", "/v1/messages"])
def test_off_barrier_covers_gap_between_final_check_and_request(prepared, monkeypatch, target):
    store, cfg = prepared
    source = enqueue(store, cfg)
    before_request = threading.Event()
    release_request = threading.Event()
    barrier_observed = threading.Event()
    acknowledged = threading.Event()
    calls = []

    def handler(request):
        calls.append((request.url.path, acknowledged.is_set()))
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return message([{"text": "I improved the parser.",
                         "evidence_ids": [source["items"][0]["id"]]}])

    gen = generator(store, cfg, handler, clock=lambda: NOW + 1)
    request = gen._request

    def paused_request(path, body):
        if path.endswith(target):
            before_request.set()
            assert release_request.wait(5)
        return request(path, body)

    def turn_off():
        # Match the runtime handshake: close the gate without waiting on the
        # dispatch lock, then acquire the barrier outside Store.lock.
        store.set_enabled(False, NOW + 1)
        if store.dispatch_lock.acquire(blocking=False):
            try:
                acknowledged.set()
            finally:
                store.dispatch_lock.release()
            barrier_observed.set()
        else:
            barrier_observed.set()
            with store.dispatch_lock:
                acknowledged.set()

    monkeypatch.setattr(gen, "_request", paused_request)
    with ThreadPoolExecutor(max_workers=2) as workers:
        cycle = workers.submit(gen.cycle, NOW + 1)
        try:
            assert before_request.wait(5)
            off = workers.submit(turn_off)
            # This also proves Store.lock is not held across the request gap:
            # the control command must be able to change the enabled state.
            assert barrier_observed.wait(5)
        finally:
            release_request.set()
        result = cycle.result(timeout=5)
        off.result(timeout=5)

    expected = ["/v1/messages/count_tokens"]
    if target == "/v1/messages":
        expected.append(target)
    assert calls == [(path, False) for path in expected]
    assert acknowledged.is_set()
    assert result["candidates"] == 0
    assert store.list_suggestions() == []
    budget = store.status(NOW + 2)["budget"]
    assert budget["reserved_micro_usd"] == 0
    assert budget["spent_micro_usd"] == (180 if target == "/v1/messages" else 0)


@pytest.mark.parametrize("stage", ["count", "generation"])
def test_acknowledged_off_on_invalidates_prepared_payload(prepared, monkeypatch, stage):
    store, cfg = prepared
    enqueue(store, cfg)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"input_tokens": 100})

    gen = generator(store, cfg, handler, clock=lambda: NOW + 1)
    dispatch = gen._dispatch
    target = "/v1/messages/count_tokens" if stage == "count" else "/v1/messages"

    def dispatch_after_toggle(units, path, body, counter):
        if path.endswith(target):
            store.set_enabled(False, NOW + 1)
            with store.dispatch_lock:
                pass  # off is acknowledged before this operation resumes
            store.set_enabled(True, NOW + 1)
        return dispatch(units, path, body, counter)

    monkeypatch.setattr(gen, "_dispatch", dispatch_after_toggle)
    result = gen.cycle(NOW + 1)
    assert calls == ([] if stage == "count" else ["/v1/messages/count_tokens"])
    assert result["generation_requests"] == 0
    budget = store.status(NOW + 2)["budget"]
    assert budget["spent_micro_usd"] == 0
    assert budget["reserved_micro_usd"] == 0


def test_runtime_clock_follows_visibility_updates(prepared):
    store, cfg = prepared
    enqueue(store, cfg)
    wall = [NOW + 1]

    def visibility(_):
        wall[0] += 0.25
        store.expire(wall[0])
        return True

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return message()

    result = generator(store, cfg, handler, visibility, clock=lambda: wall[0]).cycle(NOW + 1)
    assert result["processed"] == 1
    assert store.status(wall[0])["budget"]["spent_micro_usd"] == 180


def test_count_failure_keeps_pending_and_never_sends_unbounded_input(prepared):
    store, cfg = prepared
    enqueue(store, cfg)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(503)

    gen = generator(store, cfg, handler)
    gen.cycle(NOW + 1)
    gen.cycle(NOW + 2)
    assert calls == ["/v1/messages/count_tokens"]
    assert len(store.pending(NOW + 62)) == 1
    gen.cycle(NOW + 62)
    assert calls == ["/v1/messages/count_tokens"] * 2
    assert len(store.pending(NOW + 123)) == 1


def test_split_preserves_whole_items_and_marks_partial_evidence(prepared):
    store, cfg = prepared
    source = enqueue(store, cfg, unit(texts=["Small safe change", "Unfit item", "Another small change"]))
    sent = []

    def handler(request):
        body = json.loads(request.content)
        data = json.loads(body["messages"][0]["content"])
        evidence = data["evidence"]
        if request.url.path.endswith("count_tokens"):
            count = 13_000 if any(i["text"] == "Unfit item" for i in evidence) else len(evidence) * 7_000
            return httpx.Response(200, json={"input_tokens": count})
        sent.append(evidence)
        assert body["max_tokens"] == 2_000
        return message([{"text": "I refined the parser.", "evidence_ids": [evidence[0]["id"]]}])

    result = generator(store, cfg, handler).cycle(NOW + 1)
    assert result["oversized_items"] == 1
    assert [[i["text"] for i in request] for request in sent] == [["Small safe change"], ["Another small change"]]
    assert all(i["partial_source"] for request in sent for i in request)
    assert store.seen(source)
    suggestions = store.list_suggestions()
    assert len(suggestions) == 2
    for suggestion in suggestions:
        record = store.show(suggestion["id"], evidence=True)
        assert all(item["partial_source"] for item in record["evidence"])


def test_rate_limit_honors_longer_retry_after(prepared):
    store, cfg = prepared
    enqueue(store, cfg)
    generated = []

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        generated.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "600"}) if len(generated) == 1 else message()

    gen = generator(store, cfg, handler)
    gen.cycle(NOW + 1)
    gen.cycle(NOW + 62)
    assert len(generated) == 1
    gen.cycle(NOW + 602)
    assert len(generated) == 2
    assert not store.pending(NOW + 603)


def test_authentication_failure_pauses_until_config_change(prepared):
    store, cfg = prepared
    enqueue(store, cfg)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(401) if len(calls) == 1 else (
            httpx.Response(200, json={"input_tokens": 100}) if request.url.path.endswith("count_tokens") else message())

    gen = generator(store, cfg, handler)
    gen.cycle(NOW + 1)
    gen.cycle(NOW + 62)
    assert calls == ["/v1/messages/count_tokens"]
    assert store.settings()["generation_paused"]
    cfg = copy.deepcopy(cfg)
    cfg["budget"]["monthly_usd"] = "21.00"
    store.apply_config(cfg, NOW + 63)
    gen.cycle(NOW + 64)
    assert calls[-1] == "/v1/messages"
    assert not store.pending(NOW + 65)


def test_global_gate_prevents_overlapping_generators(prepared):
    store, cfg = prepared
    enqueue(store, cfg)
    second_results = []
    second_calls = []
    second = generator(store, cfg, lambda request: second_calls.append(request))

    def handler(request):
        second_results.append(second.cycle(NOW + 1))
        return httpx.Response(200, json={"input_tokens": 100}) if request.url.path.endswith("count_tokens") else message()

    generator(store, cfg, handler).cycle(NOW + 1)
    assert second_calls == []
    assert all(result["count_requests"] == result["generation_requests"] == 0 for result in second_results)
    assert not store.pending(NOW + 2)


def test_four_billable_failures_are_all_charged_and_never_replayed(prepared):
    store, cfg = prepared
    source = enqueue(store, cfg)
    generated = []

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        generated.append(request.url.path)
        raise httpx.ReadTimeout("synthetic failure", request=request)

    gen = generator(store, cfg, handler)
    for elapsed in (1, 62, 363, 2_164, 5_000):
        gen.cycle(NOW + elapsed)
    assert len(generated) == 4
    assert store.status(NOW + 5_001)["budget"]["spent_micro_usd"] == 40_400
    assert store.status(NOW + 5_001)["budget"]["reserved_micro_usd"] == 0
    assert not store.pending(NOW + 5_001)
    assert not store.enqueue(source, NOW + 5_002)


def test_generated_secret_rejects_whole_response_without_rewriting(prepared):
    store, cfg = prepared
    source = enqueue(store, cfg)

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        return message([
            {"text": "I improved the parser.", "evidence_ids": [source["items"][0]["id"]]},
            {"text": "I set password=synthetic-sensitive-value.", "evidence_ids": [source["items"][0]["id"]]},
        ])

    generator(store, cfg, handler).cycle(NOW + 1)
    assert store.list_suggestions() == []
    assert len(store.pending(NOW + 62)) == 1
    assert store.status(NOW + 62)["budget"]["spent_micro_usd"] == 180


def test_unicode_character_limit_rejects_overlength_without_clipping(prepared):
    store, cfg = prepared
    cfg = copy.deepcopy(cfg)
    cfg["generation"]["character_limit"] = 20
    store.apply_config(cfg, NOW)
    source = enqueue(store, cfg)
    text = "I explored " + "\u00e9" * 9
    generated = []

    def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        generated.append(1)
        post = text + "!" if len(generated) == 1 else text
        return message([{"text": post, "evidence_ids": [source["items"][0]["id"]]}])

    gen = generator(store, cfg, handler)
    gen.cycle(NOW + 1)
    assert store.list_suggestions() == []
    gen.cycle(NOW + 62)
    assert [row["text"] for row in store.list_suggestions()] == [text]


def test_unknown_model_pauses_without_even_a_count_request(prepared):
    store, cfg = prepared
    cfg = copy.deepcopy(cfg)
    cfg["generation"]["model"] = "unpriced-model"
    store.apply_config(cfg, NOW)
    enqueue(store, cfg)
    calls = []
    generator(store, cfg, lambda request: calls.append(request)).cycle(NOW + 1)
    assert calls == []
    assert store.settings()["generation_paused"]


def test_blocklist_change_between_count_and_generation_closes_gate(prepared):
    store, cfg = prepared
    enqueue(store, cfg)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        revised = copy.deepcopy(cfg)
        revised["blocklist"]["text"] = ["parser"]
        store.apply_config(revised, NOW + 1)
        return httpx.Response(200, json={"input_tokens": 100})

    generator(store, cfg, handler).cycle(NOW + 1)
    assert calls == ["/v1/messages/count_tokens"]
    assert store.list_suggestions() == []
    assert not store.pending(NOW + 2)


def test_split_retries_keep_original_expiry_and_expired_pieces_never_transmit(prepared):
    store, cfg = prepared
    source = enqueue(store, cfg, unit(texts=["First safe change", "Second safe change"]))
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("count_tokens"):
            evidence = json.loads(json.loads(request.content)["messages"][0]["content"])["evidence"]
            return httpx.Response(200, json={"input_tokens": len(evidence) * 7_000})
        raise httpx.ReadTimeout("synthetic failure", request=request)

    gen = generator(store, cfg, handler)
    gen.cycle(NOW + 1)
    pieces = store.pending(NOW + 2)
    assert len(pieces) == 2
    assert all(piece["expires_at"] == NOW + 86_400 for piece in pieces)
    assert all(piece["collected_at"] == NOW for piece in pieces)
    before_expiry = len(calls)
    gen.cycle(NOW + 86_400)
    assert len(calls) == before_expiry
    assert not store.pending(NOW + 86_401)
    assert not store.enqueue(source, NOW + 86_402)
