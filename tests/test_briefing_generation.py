"""Personal briefing boundaries and trust rules, without provider access."""
import copy
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from actomasto.briefing_generation import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_TOKENS,
    MAX_RESPONSE_BYTES,
    MODEL,
    BriefingGenerationError,
    generate,
    prepare,
    render,
    validate,
)

DAY = date(2026, 9, 24)
ZONE = "America/Los_Angeles"


def evidence(identifier="commit-1", *, text="Fixed the parser boundary.",
             at="2026-09-23T16:00:00-07:00", kind="commit", provenance="committed"):
    return {"id": identifier, "kind": kind, "provenance": provenance,
            "time": at, "text": text, "source": "local: commit record"}


def project(identifier="parser", items=None, *, last=None, unfinished=None):
    return {"id": identifier, "name": identifier, "hosts": ["private-host"],
            "last_activity": last, "evidence": [evidence()] if items is None else items,
            "unfinished": unfinished or []}


def bundle(*projects):
    return {"projects": list(projects), "coverage": ["Remote host was unavailable."], "blocklist": {}}


def packet(data=None, kind="daily", **kwargs):
    return prepare(data or bundle(project()), kind=kind, report_date=DAY, timezone=ZONE, **kwargs)


def ref(item, project_id="parser", quote_index=0):
    return {"project_id": project_id, "evidence_id": item["id"],
            "quote_index": quote_index, "summary": item["text"].replace("\n", " ")[:500]}


def provider_ref(item, quote_index=0):
    return {key: value for key, value in ref(item, quote_index=quote_index).items()
            if key != "project_id"}


def report(**kwargs):
    return {"recap": [], "continuity": [], "resurfacing": [], "reentry": [], **kwargs}


def entry(identifier, citation, **kwargs):
    return {"project_id": identifier, "context": [citation], "decisions": [],
            "intention": [], "next_step": [], **kwargs}


def provider_reply(value, **kwargs):
    return httpx.Response(200, json={
        "model": MODEL, "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps(value)}],
        "usage": {"input_tokens": 120, "output_tokens": 30}, **kwargs,
    })


@pytest.mark.parametrize("delivery,expected_hours", [(date(2026, 3, 9), 23), (date(2026, 11, 2), 25)])
def test_daily_uses_dst_calendar_day_not_twenty_four_hours(delivery, expected_hours):
    prepared = prepare(bundle(), kind="daily", report_date=delivery, timezone=ZONE)
    start = datetime.fromisoformat(prepared["window"]["start"])
    end = datetime.fromisoformat(prepared["window"]["end"])
    assert end - start == timedelta(hours=expected_hours)
    assert start.hour == end.hour == 0
    assert start.date() == delivery - timedelta(days=1)


def test_daily_boundaries_include_local_midnight_and_exclude_today_and_untimed_tree():
    items = [
        evidence("before", at="2026-09-23T06:59:59+00:00"),
        evidence("start", at="2026-09-23T07:00:00+00:00"),
        evidence("end-minus", at="2026-09-24T06:59:59+00:00"),
        evidence("end", at="2026-09-24T07:00:00+00:00"),
        evidence("snapshot", at=None, kind="working_tree", provenance="observed", text="Unfinished parser changes."),
    ]
    prepared = packet(bundle(project(items=items)))
    assert prepared["recap_ids"] == ["start", "end-minus"]
    assert "end" not in prepared["sources"]
    with pytest.raises(BriefingGenerationError, match="unknown_evidence"):
        validate(report(recap=[ref(items[-1])]), prepared)


def test_weekly_uses_seven_complete_days_and_preserves_older_context():
    items = [evidence("older", at="2026-09-16T23:59:59-07:00"),
             evidence("first", at="2026-09-17T00:00:00-07:00"), evidence("last"),
             evidence("today", at="2026-09-24T00:00:00-07:00")]
    prepared = packet(bundle(project(items=items)), kind="weekly")
    assert prepared["recap_ids"] == ["first", "last"]
    assert "older" in prepared["sources"]
    assert prepared["window"] == {"start": "2026-09-17T00:00:00-07:00", "end": "2026-09-24T00:00:00-07:00"}


def parked(identifier, age, hook=True):
    when = (datetime(2026, 9, 24, tzinfo=timezone.utc) - timedelta(days=age)).strftime("%Y-%m-%dT12:00:00-07:00")
    items = [evidence(f"{identifier}-commit", at=when)]
    if hook:
        items.append(evidence(f"{identifier}-hook", at=None, kind="planning", provenance="observed", text="TODO: Check empty input."))
    return project(identifier, items, last=when)


def test_resurfacing_requires_exclusive_age_bounds_and_hook_and_selects_at_most_two():
    prepared = packet(bundle(parked("seven", 7), parked("eight", 8), parked("nine", 9),
                             parked("twentynine", 29), parked("thirty", 30), parked("ageonly", 10, False)), "weekly")
    assert set(prepared["resurfacing_ids"]) == {"eight", "nine"}
    assert {p["id"] for p in prepared["projects"] if p["resurfacing_eligible"]} == {"eight", "nine", "twentynine"}
    value = report(resurfacing=[entry(name, ref(prepared["sources"][f"{name}-hook"], name))
                                for name in ("eight", "nine", "twentynine")])
    with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
        validate(value, prepared)


def test_age_only_and_duplicated_resurfacing_fail_closed():
    prepared = packet(bundle(parked("eight", 8)))
    value = entry("eight", ref(prepared["sources"]["eight-commit"], "eight"))
    with pytest.raises(BriefingGenerationError, match="unsupported_resurfacing"):
        validate(report(resurfacing=[value]), prepared)
    value["context"] = [ref(prepared["sources"]["eight-hook"], "eight")]
    with pytest.raises(BriefingGenerationError, match="duplicate_resurfacing"):
        validate(report(resurfacing=[value, copy.deepcopy(value)]), prepared)


@pytest.mark.parametrize("role", ["user_reported", "assistant_reported"])
def test_conversation_reports_cover_noncommit_work_without_claiming_verification(role):
    item = evidence("conversation", kind="conversation", provenance=role,
                    text="I investigated the failure and reported a likely cause.")
    prepared = packet(bundle(project(items=[item])))
    assert prepared["recap_ids"] == ["conversation"]
    text = render(report(recap=[ref(item)]), prepared)["text"]
    if role == "assistant_reported":
        assert "assistant reported (unverified)" in text


@pytest.mark.parametrize("mutation", ["id", "quote_index", "project"])
def test_unknown_sources_invalid_quotes_and_cross_project_attribution_fail(mutation):
    citation = ref(evidence())
    citation[{"id": "evidence_id", "quote_index": "quote_index", "project": "project_id"}[mutation]] = "invented"
    with pytest.raises(BriefingGenerationError):
        validate(report(recap=[citation]), packet())


@pytest.mark.parametrize("kind,provenance", [("code", "user_reported"), ("commit", "user_reported"),
                                            ("conversation", "verified"), ("planning", "user_reported")])
def test_invalid_source_kind_and_provenance_cannot_cross_trust_boundary(kind, provenance):
    with pytest.raises(BriefingGenerationError, match="invalid_evidence_kind"):
        packet(bundle(project(items=[evidence(kind=kind, provenance=provenance)])))


@pytest.mark.parametrize("kind,provenance", [("commit", "committed"), ("planning", "observed"),
                                            ("conversation", "assistant_reported")])
def test_code_repository_prose_and_assistant_cannot_supply_user_intention(kind, provenance):
    item = evidence("intent", text="I want to make empty input safe.", kind=kind, provenance=provenance)
    prepared = packet(bundle(project(items=[item])), "reentry", project="parser")
    value = report(reentry=[entry("parser", ref(item), intention=[ref(item)])])
    with pytest.raises(BriefingGenerationError, match="unsupported_intention"):
        validate(value, prepared)
    value["reentry"][0]["intention"] = []
    value["reentry"][0]["next_step"] = [{**ref(item), "kind": "recorded", "text": ""}]
    with pytest.raises(BriefingGenerationError, match="unsupported_next_step"):
        validate(value, prepared)


def test_recorded_user_goal_and_next_action_require_exact_quote_and_are_cited():
    item = evidence("intent", text="I want to make empty input safe.", kind="conversation", provenance="user_reported")
    prepared = packet(bundle(project(items=[item])), "reentry", project="parser")
    value = report(reentry=[entry("parser", ref(item), intention=[ref(item)],
                                next_step=[{**ref(item), "kind": "recorded", "text": ""}])])
    rendered = render(value, prepared)
    text = rendered["text"]
    assert f'Original intention, in your words: “{item["text"]}” [1]' in text
    assert f'Recorded next step (not verified as still current): “{item["text"]}” [1]' in text
    assert rendered["citations"][0]["id"] == "intent"
    assert "evidence intent" not in text
    value["reentry"][0]["next_step"][0]["text"] = "Ship the completed feature."
    with pytest.raises(BriefingGenerationError, match="unsupported_next_step"):
        validate(value, prepared)


@pytest.mark.parametrize("status,text", [("rejected", "Rejected: replace the parser."),
                                         ("deferred", "Deferred: replace the parser later."),
                                         ("speculative", "Maybe replace the parser."),
                                         ("accepted", "I will keep the current parser."),
                                         ("unresolved", "TODO: Investigate the remaining boundary.")])
def test_weekly_preserves_decision_status_without_turning_parked_work_into_tasks(status, text):
    item = evidence("decision", text=text, kind="conversation", provenance="user_reported")
    prepared = packet(bundle(project(items=[item])), "weekly")
    value = report(continuity=[{**ref(item), "status": status}])
    rendered = render(value, prepared)["text"]
    assert f"parser · {status}" in rendered
    if status in {"deferred", "rejected", "speculative"}:
        assert f"{status} — not a task" in rendered
    value["continuity"][0]["status"] = "verified"
    with pytest.raises(BriefingGenerationError, match="unsupported_status"):
        validate(value, prepared)


@pytest.mark.parametrize("word", ["Rejected", "Deferred"])
def test_rejected_and_deferred_projects_are_not_revived_as_next_steps(word):
    item = evidence("decision", at="2026-09-12T12:00:00-07:00", text=f"{word}: replace the parser.",
                    kind="conversation", provenance="user_reported")
    candidate = parked("parser", 12)
    candidate["evidence"].append(item)
    prepared = packet(bundle(candidate), "reentry", project="parser")
    latest = max((e for e in candidate["evidence"] if e["time"]), key=lambda e: (e["time"], e["id"]))
    value = report(reentry=[entry("parser", ref(latest), next_step=[{**ref(item), "kind": "suggestion", "text": "Replace it now."}])])
    with pytest.raises(BriefingGenerationError, match="unsupported_next_step"):
        validate(value, prepared)


def test_cited_rejection_cannot_be_relabelled_as_acceptance():
    item = evidence("decision", text="Rejected: I want to replace the parser.", kind="conversation", provenance="user_reported")
    prepared = packet(bundle(project(items=[item])), "weekly")
    with pytest.raises(BriefingGenerationError, match="unsupported_status"):
        validate(report(continuity=[{**ref(item), "status": "accepted"}]), prepared)


def test_new_suggestion_is_separate_from_unknown_original_intention():
    candidate = parked("parser", 12)
    prepared = packet(bundle(candidate))
    hook = candidate["evidence"][1]
    value = report(resurfacing=[entry("parser", ref(hook), next_step=[{
        **ref(hook), "kind": "suggestion", "text": "Try an empty input against the existing parser."}])])
    text = render(value, prepared)["text"]
    assert "New suggestion — not a recorded intention: Try an empty input" in text
    assert "The available sources do not establish your original intention." in text
    assert "current/undated context" in text


def test_missing_sources_return_honest_coverage_without_provider_call():
    def never_called(_):
        pytest.fail("empty evidence must not call a provider")
    with httpx.Client(transport=httpx.MockTransport(never_called)) as client:
        result = generate(bundle(project(items=[])), kind="daily", report_date=DAY,
                          timezone=ZONE, api_key="", client=client)
    assert result["usage"] == {"input_tokens": 0, "output_tokens": 0}
    assert "not a claim that nothing happened" in result["text"]
    assert "Remote host was unavailable." in result["text"]
    assert "Missing evidence does not establish inactivity." in result["text"]


def test_project_selection_is_exact_and_ambiguous_names_fail_closed():
    first = project("one", [evidence("one-record")])
    second = project("two", [evidence("two-record")])
    prepared = packet(bundle(first, second), "reentry", project="two")
    assert set(prepared["sources"]) == {"two-record"}
    first["name"] = second["name"] = "same"
    with pytest.raises(BriefingGenerationError, match="project_not_found_or_ambiguous"):
        packet(bundle(first, second), "reentry", project="same")
    with pytest.raises(BriefingGenerationError, match="project_required"):
        packet(bundle(first), "reentry")


def test_reentry_requires_latest_dated_record_not_only_old_work():
    old = evidence("old", at="2026-08-01T12:00:00-07:00")
    latest = evidence("latest")
    prepared = packet(bundle(project(items=[old, latest])), "reentry", project="parser")
    with pytest.raises(BriefingGenerationError, match="missing_last_state"):
        validate(report(reentry=[entry("parser", ref(old))]), prepared)


def test_bounded_unicode_evidence_discloses_trim_and_keeps_ids_resolvable():
    items = [evidence(f"item-{number}", text="A measured change to the parser. 界 " * 180) for number in range(14)]
    prepared = packet(bundle(project(items=items)))
    assert len(json.dumps(prepared["payload"], ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_INPUT_BYTES
    assert any("trimmed" in line for line in prepared["coverage"])
    assert set(prepared["recap_ids"]) <= set(prepared["sources"])
    omitted = set(item["id"] for item in items) - set(prepared["sources"])
    assert omitted
    value = ref(next(item for item in items if item["id"] in omitted))
    with pytest.raises(BriefingGenerationError, match="unknown_evidence"):
        validate(report(recap=[value]), prepared)


def test_hosts_paths_and_blocklist_stay_out_of_hosted_payload():
    item = evidence(text="Updated /home/private/code/parser.py safely.")
    item["source"] = "private-host:/home/private/code/parser.py"
    data = bundle(project(items=[item]))
    data["blocklist"] = {"text": ["sensitive-term"]}
    prepared = packet(data)
    hosted = json.dumps(prepared["payload"])
    assert "private-host" not in hosted
    assert "/home/private" not in hosted
    assert "sensitive-term" not in hosted
    assert "REDACTED_PATH" in hosted


def test_output_policy_rejects_new_suggestion_secrets_and_blocklisted_text():
    candidate = parked("parser", 12)
    data = bundle(candidate)
    data["blocklist"] = {"text": ["forbidden phrase"]}
    prepared = packet(data)
    for unsafe in ("forbidden phrase", "Set password=private-value-now.", "Open /home/private/secrets."):
        value = report(resurfacing=[entry("parser", ref(candidate["evidence"][1]), next_step=[{
            **ref(candidate["evidence"][1]), "kind": "suggestion", "text": unsafe}])])
        with pytest.raises(BriefingGenerationError, match="unsafe_output"):
            validate(value, prepared)


def test_generate_uses_one_bounded_messages_call_and_returns_usage():
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["model"] == MODEL
        assert body["max_tokens"] == MAX_OUTPUT_TOKENS
        assert "tools" not in body
        assert len(body["messages"][0]["content"].encode()) <= MAX_INPUT_BYTES
        return provider_reply({"recap": [provider_ref(evidence())]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = generate(bundle(project()), kind="daily", report_date=DAY,
                          timezone=ZONE, api_key="synthetic-key", client=client)
    assert [request.url.path for request in calls] == ["/v1/messages"]
    assert result["usage"] == {"input_tokens": 120, "output_tokens": 30}
    assert "Fixed the parser boundary." in result["text"]
    assert result["model"] == MODEL


@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "https://example.org/steal"}),
    httpx.Response(429),
    httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
    provider_reply({"recap": []}, stop_reason="max_tokens"),
    provider_reply({"recap": []}, usage={"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 20}),
    provider_reply({"recap": []}, content=[{"type": "tool_use", "name": "shell"}]),
    provider_reply({"recap": []}, content=[{"type": "text", "text": '```json\n{"recap":[]}\n```'}]),
    provider_reply({"recap": []}, content=[{"type": "text", "text": '{"recap":[],"recap":[]}'}]),
    provider_reply({"recap": [], "unvalidated_summary": "The user completed everything."}),
    provider_reply({"recap": [{"evidence_id": "invented", "quote_index": 0, "summary": "invented"}]}),
])
def test_provider_errors_are_content_free_and_never_retried_or_redirected(response):
    calls = []
    def handler(request):
        calls.append(request)
        return response
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BriefingGenerationError) as error:
            generate(bundle(project()), kind="daily", report_date=DAY,
                     timezone=ZONE, api_key="synthetic-key", client=client)
    assert len(calls) == 1
    assert "synthetic-key" not in str(error.value)
    assert "invented" not in str(error.value)
    if response.status_code != 200:
        assert str(error.value) == f"provider_http_{response.status_code}"


def test_constraint_does_not_reject_project_or_hide_its_unfinished_hook():
    candidate = parked("parser", 12)
    candidate["evidence"].append(evidence("constraint", at="2026-09-12T16:00:00-07:00",
        kind="conversation", provenance="user_reported",
        text="Please implement the empty-input fix; do not deploy. TODO: Check boundary behavior."))
    prepared = packet(bundle(candidate))
    assert prepared["resurfacing_ids"] == ["parser"]
    citation = ref(candidate["evidence"][-1])
    value = report(resurfacing=[entry("parser", citation, next_step=[{
        **citation, "kind": "recorded", "text": ""}])])
    assert validate(value, prepared) is value


def test_canonical_project_id_and_narrative_summary_keep_private_citations_out_of_email():
    identifier = "git:github.com/example/parser"
    item = evidence()
    prepared = packet(bundle(project(identifier, [item])))
    citation = ref(item, identifier)
    citation["summary"] = "The parser's boundary handling was corrected."
    rendered = render(report(recap=[citation]), prepared)
    assert citation["summary"] in rendered["text"]
    assert f'“{item["text"]}”' not in rendered["text"]
    assert "commit-1" not in rendered["text"]
    assert rendered["citations"][0]["project_id"] == identifier
    assert rendered["citations"][0]["quotes"][rendered["report"]["recap"][0]["quote_index"]] == item["text"]


def test_assistant_summary_cannot_invent_a_user_intention():
    item = evidence("assistant", kind="conversation", provenance="assistant_reported")
    prepared = packet(bundle(project(items=[item])))
    citation = ref(item)
    citation["summary"] = "You wanted to replace this parser."
    with pytest.raises(BriefingGenerationError, match="unsupported_intention"):
        validate(report(recap=[citation]), prepared)


def test_noisy_project_cannot_starve_other_daily_projects_or_return_hooks():
    noisy = project("noisy", [evidence(f"noisy-{index}", kind="conversation",
        provenance="assistant_reported", text="Investigated the parser carefully. " * 140)
        for index in range(15)])
    quiet = [project(f"quiet-{index}", [evidence(f"quiet-record-{index}")]) for index in range(3)]
    returning = parked("returning", 12)
    prepared = packet(bundle(noisy, *quiet, returning))
    assert {f"quiet-record-{index}" for index in range(3)} <= set(prepared["recap_ids"])
    assert "returning-hook" in prepared["sources"]
    assert prepared["resurfacing_ids"] == ["returning"]
    assert any("trimmed" in line for line in prepared["coverage"])


def test_generated_summary_cannot_inject_unvalidated_citation_numbers():
    citation = ref(evidence())
    citation["summary"] = "The parser was fixed [999]."
    with pytest.raises(BriefingGenerationError, match="invalid_summary"):
        validate(report(recap=[citation]), packet())


@pytest.mark.parametrize("field", ["reentry", "intention", "next_step"])
@pytest.mark.parametrize("invalid", [None, {}, [None], [{}, {}]])
def test_optional_report_sections_require_zero_or_one_objects(field, invalid):
    prepared = packet(kind="reentry", project="parser")
    value = report(reentry=[entry("parser", ref(evidence()))])
    if field == "reentry":
        value[field] = invalid
    else:
        value["reentry"][0][field] = invalid
    with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
        validate(value, prepared)


def test_short_dirty_file_excerpt_supports_a_long_recorded_resurfacing_hook():
    observation = "M parser.py\n" + "\n".join(f"M components/parser_part_{index}.py" for index in range(40))
    candidate = parked("parser", 12, hook=False)
    dirty = evidence("dirty", text=observation, at=None, kind="working_tree", provenance="observed")
    candidate["evidence"].append(dirty)
    candidate["unfinished"] = [observation]
    prepared = packet(bundle(candidate))
    citation = ref(dirty)
    citation["summary"] = "Local parser edits provide concrete context to pick up again."
    value = report(resurfacing=[entry("parser", citation)])
    assert prepared["resurfacing_ids"] == ["parser"]
    assert validate(value, prepared) is value
    citation["quote_index"] = len(prepared["sources"]["dirty"]["quotes"])
    with pytest.raises(BriefingGenerationError, match="invalid_quote_index"):
        validate(value, prepared)


@pytest.mark.parametrize("index", [-1, 1, True, 0.0, "0", None])
def test_quote_index_requires_an_in_bounds_integer(index):
    citation = ref(evidence(), quote_index=index)
    with pytest.raises(BriefingGenerationError, match="invalid_quote_index"):
        validate(report(recap=[citation]), packet())


def test_indexed_quote_preserves_original_markdown_and_multiline_intention():
    original = "I want to add [fal.ai](https://fal.ai) support\nwithout changing the existing parser."
    item = evidence("markdown", text=original, kind="conversation", provenance="user_reported")
    prepared = packet(bundle(project(items=[item])), "reentry", project="parser")
    citation = ref(item)
    value = report(reentry=[entry("parser", citation, intention=[citation],
        next_step=[{**citation, "kind": "recorded", "text": ""}])])
    rendered = render(value, prepared)
    assert original in rendered["text"]
    assert rendered["citations"][0]["quotes"] == [original]
    assert "text" not in prepared["payload"]["projects"][0]["evidence"][0]
    assert prepared["payload"]["projects"][0]["evidence"][0]["quotes"] == [{"index": 0, "text": original}]


def test_bounded_excerpts_are_original_slices_and_full_local_evidence_is_retained():
    original = "A parser observation with preserved punctuation.\n" * 180
    item = evidence("long-record", text=original)
    prepared = packet(bundle(project(items=[item])))
    source = prepared["sources"]["long-record"]
    assert source["text"] == original
    assert all(0 < len(quote) <= 600 and quote in original for quote in source["quotes"])
    assert any("trimmed" in line for line in prepared["coverage"])


def test_model_cannot_supply_its_own_retyped_quote_field():
    citation = ref(evidence())
    citation["quote"] = "An invented quotation."
    with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
        validate(report(recap=[citation]), packet())


@pytest.mark.parametrize("text,eligible", [
    ("Review pending candidates in the grid before choosing one.", False),
    ("The queue pauses blocked jobs until workers return.", False),
    ("TODO: Add empty-input validation.", True),
    ("- [ ] Add empty-input validation.", True),
    ("## Next steps\nAdd empty-input validation.", True),
])
def test_resurfacing_distinguishes_planning_tasks_from_product_state(text, eligible):
    candidate = parked("parser", 12, hook=False)
    candidate["evidence"].append(evidence("readme", text=text, at=None, kind="planning", provenance="observed"))
    prepared = packet(bundle(candidate))
    assert prepared["resurfacing_ids"] == (["parser"] if eligible else [])


def test_weekly_curates_representative_recaps_without_discarding_context():
    projects = [project(f"project-{number}", [
        evidence(f"record-{number}-{index}", text="Adjusted parser boundary handling.",
                 kind="conversation", provenance="user_reported")
        for index in range(3)]) for number in range(10)]
    prepared = packet(bundle(*projects), "weekly")
    counts = {}
    for identity in prepared["recap_ids"]:
        project_id = prepared["sources"][identity]["project_id"]
        counts[project_id] = counts.get(project_id, 0) + 1
    assert len(prepared["recap_ids"]) == 12
    assert set(counts) == {value["id"] for value in projects}
    assert all(count <= 2 for count in counts.values())
    assert any("curated" in line for line in prepared["coverage"])
    contextual = next(source for identity, source in prepared["sources"].items()
                      if identity not in prepared["recap_ids"])
    value = report(continuity=[{**ref(contextual, contextual["project_id"]), "status": "reported"}])
    assert validate(value, prepared) is value


@pytest.mark.parametrize("section", ["continuity", "context", "decisions"])
def test_weekly_and_reentry_sections_enforce_concise_cardinality(section):
    items = [evidence(f"decision-{index}", kind="conversation", provenance="user_reported",
                      text="TODO: Check parser boundary handling.") for index in range(7)]
    if section == "continuity":
        prepared = packet(bundle(project(items=items)), "weekly")
        value = report(continuity=[{**ref(item), "status": "unresolved"} for item in items])
    else:
        prepared = packet(bundle(project(items=items)), "reentry", project="parser")
        value = report(reentry=[entry("parser", ref(items[-1]))])
        value["reentry"][0][section] = [
            ref(item) if section == "context" else {**ref(item), "status": "unresolved"} for item in items[:4]]
    with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
        validate(value, prepared)


@pytest.mark.parametrize("kind", ["daily", "reentry"])
def test_project_hosts_appear_once_without_being_sent_to_provider(kind):
    candidate = project()
    candidate["hosts"] = ["local", "tmoney-macbook.local", "local"]
    prepared = packet(bundle(candidate), kind, project="parser" if kind == "reentry" else None)
    value = (report(reentry=[entry("parser", ref(evidence()))]) if kind == "reentry"
             else report(recap=[ref(evidence())]))
    text = render(value, prepared)["text"]
    assert text.count("(local, tmoney-macbook.local)") == 1
    assert "tmoney-macbook.local" not in json.dumps(prepared["payload"])


def test_daily_excludes_old_nonreturning_context_but_keeps_real_return_context():
    current = project("active", [evidence("today-work"),
        evidence("old-active", at="2026-09-12T12:00:00-07:00"),
        evidence("active-readme", text="Project documentation.", kind="planning", provenance="observed", at=None)])
    returning = parked("returning", 12)
    stale = parked("stale", 12, hook=False)
    prepared = packet(bundle(current, returning, stale))
    assert set(prepared["sources"]) == {"today-work", "returning-commit", "returning-hook"}
    value = report(recap=[ref(prepared["sources"]["returning-commit"], "returning")])
    with pytest.raises(BriefingGenerationError, match="outside_recap_window"):
        validate(value, prepared)


@pytest.mark.parametrize("oid", [
    "c0a0bb13181b602d9aac4b53375d8592d5087cba",
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
])
def test_private_git_references_remain_resolvable_without_hosted_or_email_leakage(oid):
    item = evidence()
    item["source"] = f"local:/home/private/code/parser@{oid}\nmacbook:/Users/private/code/parser@{oid}"
    prepared = packet(bundle(project(items=[item])))
    rendered = render(report(recap=[ref(item)]), prepared)
    assert rendered["citations"][0]["source"] == item["source"]
    assert oid not in rendered["text"]
    assert "/home/private" not in rendered["text"]
    assert oid not in json.dumps(prepared["payload"])
    assert "/Users/private" not in json.dumps(prepared["payload"])


def test_provider_omits_source_known_empty_recap_and_canonical_report_restores_it():
    item = evidence()
    provider_report = {"reentry": [{"project_id": "parser", "context": [provider_ref(item)],
                                    "next_step": []}]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(provider_report))) as client:
        result = generate(bundle(project(items=[item])), kind="reentry", report_date=DAY,
                          timezone=ZONE, project="parser", api_key="synthetic-key", client=client)
    assert result["report"]["recap"] == []
    assert result["report"]["reentry"][0]["context"][0]["evidence_id"] == item["id"]
    assert result["report"]["reentry"][0]["decisions"] == []
    assert item["text"] in result["text"]


def test_provider_cannot_add_recap_when_request_schema_omits_it():
    value = {"recap": [], "reentry": [{"project_id": "parser", "context": [provider_ref(evidence())],
                                      "next_step": []}]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(value))) as client:
        with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
            generate(bundle(project()), kind="reentry", report_date=DAY,
                     timezone=ZONE, project="parser", api_key="synthetic-key", client=client)


@pytest.mark.parametrize("variant", ["valid", "reentry", "resurfacing", "missing_continuity"])
def test_weekly_provider_must_follow_mode_and_source_eligible_sections(variant):
    item = evidence(kind="conversation", provenance="user_reported")
    value = {"recap": [provider_ref(item)], "continuity": []}
    if variant == "reentry":
        value["reentry"] = [{"project_id": "parser", "context": [provider_ref(item)], "decisions": [], "next_step": []}]
    elif variant == "resurfacing":
        value["resurfacing"] = []
    elif variant == "missing_continuity":
        del value["continuity"]
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(value))) as client:
        if variant == "valid":
            result = generate(bundle(project(items=[item])), kind="weekly", report_date=DAY,
                              timezone=ZONE, api_key="synthetic-key", client=client)
            assert result["report"]["recap"][0]["evidence_id"] == item["id"]
            assert result["report"]["reentry"] == result["report"]["resurfacing"] == []
        else:
            with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
                generate(bundle(project(items=[item])), kind="weekly", report_date=DAY,
                         timezone=ZONE, api_key="synthetic-key", client=client)


@pytest.mark.parametrize("kind,provenance", [
    ("commit", "committed"), ("planning", "observed"), ("conversation", "assistant_reported"),
])
@pytest.mark.parametrize("status", ["accepted", "reported"])
def test_decision_sections_cannot_cite_non_user_evidence(kind, provenance, status):
    item = evidence(kind=kind, provenance=provenance)
    prepared = packet(bundle(project(items=[item])), "weekly")
    with pytest.raises(BriefingGenerationError, match="unsupported_status"):
        validate(report(continuity=[{**ref(item), "status": status}]), prepared)


def test_provider_cannot_add_decisions_when_no_trusted_source_exists():
    value = {"reentry": [{"project_id": "parser", "context": [provider_ref(evidence())], "next_step": [],
                         "decisions": [{**provider_ref(evidence()), "status": "accepted"}]}]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(value))) as client:
        with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
            generate(bundle(project()), kind="reentry", report_date=DAY,
                     timezone=ZONE, project="parser", api_key="synthetic-key", client=client)


def test_weekly_without_trusted_decision_evidence_restores_empty_continuity():
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply({"recap": [provider_ref(evidence())]}))) as client:
        result = generate(bundle(project()), kind="weekly", report_date=DAY,
                          timezone=ZONE, api_key="synthetic-key", client=client)
    assert result["report"]["continuity"] == []
    assert result["report"]["recap"][0]["evidence_id"] == evidence()["id"]


@pytest.mark.parametrize("role,text,eligible", [
    ("user_reported", "What next after milestone one?", False),
    ("user_reported", "Status update: midway through testing.", False),
    ("assistant_reported", "I want to add offline support.", False),
    ("user_reported", "Deferred: I want to add offline support.", False),
    ("user_reported", "I want to add offline support.", True),
])
def test_provider_intention_requires_an_explicit_trusted_goal(role, text, eligible):
    item = evidence("goal", text=text, kind="conversation", provenance=role)
    citation = provider_ref(item)
    provider_entry = {"project_id": "parser", "context": [citation], "next_step": []}
    if role == "user_reported":
        provider_entry["decisions"] = []
    if eligible:
        provider_entry["intention"] = [citation]
    value = {"reentry": [provider_entry]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(value))) as client:
        result = generate(bundle(project(items=[item])), kind="reentry", report_date=DAY,
                          timezone=ZONE, project="parser", api_key="synthetic-key", client=client)
    assert result["report"]["reentry"][0]["intention"] == ([ref(item)] if eligible else [])


def test_provider_cannot_present_an_assistant_proposal_as_original_intention():
    item = evidence("proposal", text="I want to add offline support.",
                    kind="conversation", provenance="assistant_reported")
    citation = provider_ref(item)
    value = {"reentry": [{"project_id": "parser", "context": [citation],
                          "intention": [citation], "next_step": []}]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(value))) as client:
        with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
            generate(bundle(project(items=[item])), kind="reentry", report_date=DAY,
                     timezone=ZONE, project="parser", api_key="synthetic-key", client=client)


def test_named_source_gaps_are_not_hidden_by_selection_and_size_notes():
    data = bundle(project())
    data["coverage"] = [f"Discovery cap applied to root {index}." for index in range(24)]
    data["coverage"] += ["Evidence trimmed to size limit.", "Recap curated.",
                         "tmoney-macbook.local unavailable.", "omp incomplete.",
                         "tmoney-macbook.local unavailable."]
    result = render(report(recap=[ref(evidence())]), packet(data))
    assert result["text"].count("tmoney-macbook.local") == 1
    assert "omp incomplete." in result["text"]
    assert "tmoney-macbook.local unavailable." in result["coverage"]
    assert "Discovery cap applied to root 23." in result["coverage"]
    assert "Discovery cap applied to root 23." not in result["text"]


def test_provider_citation_uses_authoritative_canonical_repository_identity():
    identity = "git:github.com/audiodude/funes"
    item = evidence("funes-change")
    value = {"recap": [provider_ref(item)]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(value))) as client:
        result = generate(bundle(project(identity, [item])), kind="weekly", report_date=DAY,
                          timezone=ZONE, api_key="synthetic-key", client=client)
    assert result["report"]["recap"][0]["project_id"] == identity
    assert result["citations"][0]["project_id"] == identity


@pytest.mark.parametrize("claimed", ["git:github.com/audiodude/funes", "git:github.com/funes"])
def test_provider_cannot_supply_a_redundant_citation_project_identity(claimed):
    identity = "git:github.com/audiodude/funes"
    item = evidence("funes-change")
    value = {"recap": [{**provider_ref(item), "project_id": claimed}]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(value))) as client:
        with pytest.raises(BriefingGenerationError, match="invalid_report_shape"):
            generate(bundle(project(identity, [item])), kind="weekly", report_date=DAY,
                     timezone=ZONE, api_key="synthetic-key", client=client)


def test_reentry_citation_identity_is_derived_for_context_decisions_intention_and_steps():
    identity = "git:github.com/audiodude/funes"
    item = evidence("funes-goal", text="I want to add offline support.",
                    kind="conversation", provenance="user_reported")
    citation = provider_ref(item)
    value = {"reentry": [{"project_id": identity, "context": [citation],
                          "decisions": [{**citation, "status": "accepted"}],
                          "intention": [citation],
                          "next_step": [{**citation, "kind": "recorded", "text": ""}]}]}
    with httpx.Client(transport=httpx.MockTransport(lambda _: provider_reply(value))) as client:
        result = generate(bundle(project(identity, [item])), kind="reentry", report_date=DAY,
                          timezone=ZONE, project=identity, api_key="synthetic-key", client=client)
    for section in ("context", "decisions", "intention", "next_step"):
        assert result["report"]["reentry"][0][section][0]["project_id"] == identity
