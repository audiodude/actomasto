# Funes integration: agreed design and implementation plan

Status: design agreed; implementation not started. This document authorizes neither fork creation/publication nor installation, indexing, migration, deployment, or transmission of real conversations. Prepared with AI assistance from OpenAI Codex.

The existing [milestone 1 specification](mastodon-activity-app-notes.md) describes the currently implemented system. This plan replaces its conversation-ingestion architecture only when the acceptance gates below pass. Existing operational commands remain unchanged until implementation updates them. Historical test results in that specification do not verify this integration.

## 1. Decisions and scope

The goal is to remove Actomasto's Claude Code, Codex, and Oh My Pi transcript parsers by consuming a maintained Funes fork. Adding semantic historical retrieval is not the goal.

| Decision | Agreed contract |
| --- | --- |
| Dependency ownership | A maintained Funes fork, not an upstream checkout with patches applied during builds. Required changes live in the fork's source and commit history. |
| Release | Pin a compatible fork revision; upstream acceptance is not a prerequisite. Fork publication requires separate authorization. |
| Deployment | Local corpus and local source access only. No remote memory binding, transcript uploads, or new hosted retrieval dependency. |
| Lifecycle | Independently managed Funes installation and indexing. Actomasto does not start, install, upgrade, or take over the indexer. No running OMP agent session is required. |
| Interface | Versioned, machine-readable local CLI interface. Do not parse existing MCP prose or couple Actomasto to private Lance tables or sidecars. |
| Coverage | Claude Code, Codex, and OMP must all satisfy the source contract before replacing the existing adapters. |
| Privacy | Preserve repository authorization, eligible-time intervals, provenance, whole-turn filtering, redaction, and existing source-content limits. |
| Source text | Funes reads complete original turns on demand. No additional durable raw-transcript copy for this integration. |
| Migration | Exact identity and exclusion continuity; preserve eligible unseen backlog. No history reset, replay, or forward-only cutoff. |
| Failure | Pause affected conversation work and report degradation. Eligible Git processing continues. No fallback to removed transcript parsers. |

Out of scope: semantic recall for draft enrichment, changes to publishing, remote memories, autonomous fork publication, and changes to the existing Anthropic generation consent. Local-only ingestion does not make generation local: eligible filtered content still follows the existing hosted-processing policy.

## 2. Ownership boundary

```text
Independently managed Funes indexing
    -> source discovery, harness parsing, metadata and coverage
    -> versioned local CLI: capabilities, enumeration, original-turn reads
    -> Actomasto eligibility and whole-turn filtering
    -> existing SQLite queue, generation, evidence and export

Existing Git collection -> same Actomasto policy and generation pipeline
```

### Funes fork

Own format-specific parsing, original record identities, source revisions, human/agent origin, completion signals, branch lineage, and metadata on excluded/control records. Expose enough information to form complete genuine-user turns without Actomasto interpreting Claude/Codex/OMP JSONL schemas. A normalized turn may be structurally complete without being authorized for Actomasto.

Keep the ingestion interface independent of semantic ranking and search-index text transformations. Existing search functionality and bridge provenance behavior must remain usable. The source interface must not inherit search redaction, elision, chunk limits, or ranked omission as if they were complete original evidence.

### Actomasto

Own repository discovery and verified-public checks; exact checkout/worktree association; enabled/login/budget gates; off/revoked intervals; whole-turn blocklists and secret filtering; durable identities and terminal markers; queue expiry; provider dispatch and settlement; drafts/evidence; and purge.

Actomasto may invoke read-only CLI requests against its configured local Funes installation. This is not ownership of the independent indexing lifecycle. It must not read transcript files itself, reconstruct harness-native events, or use the old adapters as fallback paths.

### Existing OMP bridge

Migrate the bridge's build/install integration to pin the maintained fork. Preserve its existing supported functions while removing superseded build-time patch application. Do not make multi-harness ingestion depend on an OMP session being active, or mistake the bridge's current OMP-only scheduler for Claude/Codex coverage.

## 3. Current evidence and feasibility gaps

These are source-inspection findings, not runtime verification:

- `src/actomasto/adapters/__init__.py` constructs complete turns, follows ancestry, checks directory/time boundaries, and stores terminal exclusions independently of the main source-marker table. `schemas.py` enforces harness-specific authorship and completion rules.
- The neighboring `omp-funes-bridge/src/config.ts` pins upstream Funes revision `90507de6bf4a8bedd32aa8acfc0502483d82fbdf`. Its `scripts/build-funes.ts` currently clones upstream and applies `patches/funes.patch`. That mechanism must become a pinned fork build, not merely a renamed patch workflow.
- The bridge scheduler currently invokes OMP indexing. Upstream parser availability is not evidence that the installed bridge indexes all three required harnesses.
- Existing Funes session enumeration is not a durable changed-source feed. Session start time is not last-update time, mutable pagination is not a snapshot, and partial indexing need not represent a complete prefix.
- Normalized retrieval does not preserve all required original cwd, authorship, completion, and excluded-record boundary metadata. OMP entry timestamps are not substitutes for message timestamps and completion times.
- Transformed indexed text cannot reproduce original raw-record fallback identities or guarantee Actomasto's whole-turn filtering semantics.

Reference upstream source at the inspected revision:

https://github.com/huggingface/funes/tree/90507de6bf4a8bedd32aa8acfc0502483d82fbdf/src

Recheck these implementation facts against the actual fork base before editing. No fork URL or new executable/command name is asserted here; resolve the repository identity from the authorized fork setup, and document the implemented CLI schema before either consumer is migrated.

## 4. Required local source interface

This section specifies behavior, not already available commands. The fork must document concrete request/response names, types, limits, errors, and compatibility rules before Actomasto consumes them.

### 4.1 Capabilities and local scope

- Identify protocol/schema version, fork build revision, supported harness schemas, identity algorithm versions, and coverage capabilities. Check those capabilities explicitly; executable name or a generic upstream version is insufficient.
- Require an explicit local corpus and enrolled source scope. Reject remote-memory selectors in this interface. Do not upload, bind, or fetch a remote memory as a fallback.
- Distinguish unsupported protocol, incompatible harness, unavailable dependency, incomplete coverage, and no eligible activity using structured errors/status, not prose parsing.
- Keep status/enumeration metadata-only: no first-prompt excerpts or source text. Original directories and locators are sensitive metadata; restrict permissions and keep them out of routine logs and exported evidence.

### 4.2 Exhaustive enumeration and checkpoints

- Enumerate discovered sources and updates independently of whether searchable chunks already exist. Represent empty, incomplete, malformed, and incompatible sources rather than silently omitting them.
- Provide stable snapshot/checkpoint semantics. Appends to old sessions, late-discovered sources, out-of-order backfill, and interrupted indexing must remain discoverable.
- Describe source rewrites, truncation, rotation, disappearance, and index rebuilds explicitly. An invalid cursor must be reported; recovery may re-enumerate with durable identity deduplication, never silently skip to the present.
- Bind reads to a source revision and communicate coverage separately from conversation completion. A current index does not prove the final turn is complete.
- Actomasto advances its checkpoint only after durable processing or durable exclusion. Cancellation/crash before that point leaves the item retryable; retry must not create duplicate drafts or reset expiry.
- Bound memory, page sizes, and response sizes. Do not use maximum observed sequence or timestamp as proof all earlier activity was consumed.

### 4.3 Turn identity, provenance, and boundaries

Supply, with explicit validity/completeness:

- Harness and schema identity; native session and message IDs; source identity/revision; original ordering and identity inputs where native IDs are absent.
- Exact recorded cwd and relevant changes, not a normalized workdir slug or topic-derived repository label.
- Original message times, completion times, and boundary metadata from excluded/control records. Missing, malformed, future, or nonmonotonic times must remain distinguishable.
- Positive human authorship versus agent/synthetic/injected input, ordinary assistant text, and the provenance needed to preserve `user_reported` versus `assistant_reported` evidence.
- Branch/parent relationships, resets, compaction and synthetic-summary boundaries, and reliable completion signals. Neither inactivity nor EOF alone establishes completion; retained archived branches are not proof of an active branch.
- Complete ordinary-text turn membership. Reasoning/tool payloads remain excluded from evidence, but their relevant time/project/control boundaries cannot vanish.

Actomasto continues to map directories to the unique deepest discovered repository, including ineligible nested repositories, and to reject mixed/ambiguous turns. Repository search facets are not authorization. Apply whole-interval and per-message eligibility before accepting a turn. Missing required metadata blocks affected material rather than being inferred from its text.

### 4.4 Original content on demand

Read originals through Funes only after Actomasto's collection gates permit content access. Validate the source revision and dependency/lineage inputs consistently across the read. If the source changes during a read, do not emit a mixture of revisions.

Return complete original ordinary-text items with their verified boundaries, before search-specific transformations. Do not expose reasoning/tools as draft evidence. Preserve the existing whole-unit size and filtering semantics; do not truncate or select attractive excerpts to make a turn fit.

Missing originals, changed revisions, incomplete trailing writes, malformed complete records, unknown schemas, and incomplete turns need distinct outcomes. Do not substitute indexed excerpts or mark unavailable content successfully consumed. Preserve established terminal exclusions and per-stream versus per-harness failure behavior unless an explicit contract revision is agreed.

Do not add a durable raw-source cache. Transient read buffers, diagnostics, fixtures, and temporary files must not become an undeclared second corpus. Funes's existing search storage remains separate from Actomasto retention and purge guarantees.

## 5. Exact migration continuity

### Identity compatibility

Current unit identity is SHA256 of `client:session:user-message-id`; item identity is SHA256 of `client:session:message-id`. Conversation units currently have no equivalent-change identity. The hash input is the original identity, not a Funes chunk ID or displayed sequence number.

For missing native IDs, the current adapter hashes `session:turn-context:record-index:raw-record-digest`, where `raw-record-digest` hashes the hex representation of the raw record bytes. Record ordering, byte representation, and parser context matter. Reconstructing JSON from normalized text is not equivalent. Use the current source implementation as the authoritative algorithm and preserve its inputs through the fork contract.

Preserve both:

1. `source_markers` and pending-unit identities used by SQLite deduplication.
2. `adapter-unit:*` cursors, including ambiguous/ineligible/oversized/unknown-provenance exclusions that were never yielded into `source_markers`.

Existing stream byte offsets/inodes are not portable Funes cursors. Re-enumeration is acceptable only with proven identity continuity and existing history/interval rules. Do not invent a mapping from irreversible old hashes. If compatibility cannot be established, block cutover for resolution; do not replay history or discard unseen backlog.

### Durable state and pending work

- Preserve drafts, evidence references, enrollment/import windows, off/revoked intervals, budget history, terminal markers, and purged/expired exclusions.
- Preserve valid pending work with its original collection time and expiry. Revalidate policy, eligibility, and appropriate harness health; introducing a new backend label must not bypass existing health restrictions.
- Never reset enrollment or import history, revive a terminal unit, or extend expiry during migration/retry/restart.
- Make the local schema/config/checkpoint migration restart-safe and explicit. A failed migration must not enable a half-migrated collector or allow an older binary to misinterpret new state.
- Keep old parsers only as pre-cutover implementation/test comparison material. Remove them from the shipped runtime after all gates pass; no compatibility aliases or selectable fallback backend.

## 6. Failure and operational behavior

Preserve independent harness health. An incompatible Claude source must not stop eligible Codex, OMP, or Git activity; loss of the whole Funes dependency affects all conversation sources but not Git. Recheck affected pending work at dispatch and settlement, not just collection.

Status must distinguish dependency availability, protocol compatibility, per-harness compatibility, source coverage/lag, and pending incomplete turns. Coverage lag is not proof of lost data or zero activity. Keep logs and notifications content-free and retain existing notification coalescing.

Actomasto `off`, login absence, budget pause, and revoked repository scope continue to prohibit its own source use as specified. Independently managed Funes indexing may continue; Actomasto controls do not imply it has stopped. Likewise, Actomasto purge does not delete original transcripts or the independent Funes corpus. Make these boundaries explicit in setup and consent documentation.

Existing generation cadence, queue expiry, secret-detector behavior, public visibility checks, spending rules, and hosted generation remain unchanged. Configure an absolute executable and explicit local corpus/source scope suitable for the systemd user environment; do not depend on an interactive shell or an OMP process. Document actual configuration keys and run/install commands when implemented, rather than shipping guessed commands now.

## 7. Implementation sequence and ownership

Each phase has a deliverable and an exit gate. Do not describe a phase as working until its evidence exists.

### A. Fork and source-contract definition

1. Resolve the authorized fork repository and inspect its contribution/disclosure policy. Establish the fork from a deliberate upstream base and preserve required existing OMP patch functionality as committed source changes, not a build-time overlay.
2. Define the structured CLI schema and versioning for capabilities, metadata enumeration, stable checkpoints, revision-bound original reads, coverage, and errors.
3. Specify each harness's normalization/completion/provenance and legacy identity mapping using the existing Actomasto contract. Identify source information that must survive before normalization.
4. Define independent multi-harness indexing setup and the boundary between the fork and OMP bridge; ensure no active OMP session is necessary.

Exit gate: reviewed wire schema, per-harness mapping, cursor/revision semantics, and migration identity specification. Any unmet privacy or continuity requirement is a blocker, not a reason to narrow scope silently.

### B. Implement the Funes source interface

1. Implement the metadata/identity preservation and complete-turn original reads for all three harnesses.
2. Implement exhaustive consumption, source-revision validation, cursor recovery, and typed coverage/errors independent of ranked search chunks.
3. Enforce local-only source scope and bounded content reads. Preserve existing search and bridge behavior.
4. Exercise the interface against authored synthetic transcripts, including the adversarial scenarios in §8. Document the actual supported versions and CLI behavior in the fork.

Exit gate: runnable compatible fork interface with demonstrated source-contract behavior. No Actomasto cutover based only on a compiling API or synthetic happy path.

### C. Pin consumers to the fork

1. Update `omp-funes-bridge/src/config.ts`, `scripts/build-funes.ts`, and relevant installation/verification paths to build and verify the selected fork revision.
2. Remove superseded `patches/funes.patch` application and validation paths after their required behavior is present in the fork. Preserve binary provenance checks and reject stale incompatible builds.
3. Update bridge setup/run documentation and verify its existing retrieval/indexing behavior plus standalone multi-harness setup. Do not install into or mutate the user's live memory without authorization.

Exit gate: reproducible pinned fork build, explicit capabilities, and documented independent operation. This phase can overlap Actomasto implementation only after the shared interface is fixed; one owner controls fork version/schema changes.

### D. Migrate Actomasto

1. Add a local structured-CLI consumer and validated configuration using existing process, cancellation, error, and policy conventions. Do not add a general plugin system or MCP prose parser.
2. Replace the conversation path in `src/actomasto/daemon.py`; preserve client-specific health in `store.py` and source provenance through filtering/generation.
3. Implement restart-safe configuration/checkpoint migration with exact marker continuity and unchanged pending expiry. Update `config.py`, CLI setup/status, and service-facing documentation together.
4. Migrate useful adapter tests to observable source-contract tests and cross-process synthetic fixtures. Remove parser-specific implementation assertions and tests that no longer defend a consumer contract.
5. Remove `src/actomasto/adapters/` runtime parsing, obsolete root options, and callers only after all three harnesses and migration pass. Keep repository eligibility, filtering, generation, and storage responsibilities in their existing modules.

Exit gate: Actomasto uses only the fork for transcript access, retains its existing policy and lifecycle guarantees, and has no old-parser fallback.

### E. End-to-end acceptance and operational documentation

1. Run the relevant fork, bridge, and Actomasto regression suites after integration. Validate the changed CLI/config contract and real subprocess boundary, not only mocks.
2. Exercise an isolated Actomasto daemon with synthetic originals and the actual fork executable; demonstrate migration, restart, source updates, outages, and independent Git processing.
3. Demonstrate an evidence-backed synthetic conversation draft through the real generation path. Obtain separate authorization for any provider spend/content transmission; historical smoke-test authorization does not carry forward.
4. Update current installation, configuration, service, status, compatibility, purge-boundary, and migration instructions with tested commands. Mark this plan implemented only with links to actual verification evidence and limitations.

Exit gate: every acceptance row below has current evidence. No real-source enrollment, fork publication, push, or deployment is implied by completing local work.

## 8. Acceptance matrix

| Area | Required observable proof |
| --- | --- |
| Three-harness equivalence | Claude/Codex/OMP synthetic sources yield the same eligible complete turns and exclusions as the current contract; IDs, provenance, and boundaries agree. |
| Human provenance | Agent-attributed, synthetic, injected, reasoning, tool and summary content cannot become human draft evidence; ordinary assistant claims retain reported provenance. |
| Completion and branches | Genuine completion versus EOF/inactivity, future/completion timestamps, missing parents, shared ancestors, reset/compaction, and partial final writes produce correct completion/exclusion outcomes without duplicates. |
| Authorization boundaries | Exact cwd/worktree mapping, ineligible nested repositories, mixed turns, excluded-record boundaries, and off/revoked intervals prevent unauthorized material from entering the queue or provider requests. |
| Complete filtering | A blocked phrase anywhere in the original turn excludes the whole turn. Secrets are filtered before persistence/transmission; detector failure has no raw fallback; size limits cannot be bypassed with excerpts. |
| Incremental coverage | Old-session appends, late backfill, unstable discovery order, partial indexing, and interrupted enumeration are eventually processed exactly under existing eligibility rules, without missed or repeated accepted units. |
| Source mutation | Rewrite, truncation, rotation, disappearance, and change-during-read never combine revisions or substitute stale indexed text. Cursor invalidation and missing-source conditions are visible. |
| Exact migration | A pre-migration database containing accepted, excluded, purged, expired, pending, and unseen activity preserves outcomes, IDs, windows and expiry across cutover and restart. Include raw-record fallback IDs and adapter-only exclusions. |
| Failure isolation | Harness incompatibility blocks only its affected conversation work. Whole-Funes outage blocks conversation work but permits eligible Git processing; no old-parser fallback runs. |
| Dispatch and lifecycle | Off/revoke/purge during a read or request cannot resurrect content; affected health is rechecked at dispatch and settlement; restart does not reset expiry or history. |
| Local-only operation | Actual CLI requests reject remote selectors and do not require a running OMP agent session. Metadata/status and logs do not emit transcript excerpts. |
| Fork and bridge | Reproducible build uses the pinned maintained fork without applying the retired patch; existing bridge functions remain usable and required capabilities are detected explicitly. |
| Real generation path | Actual fork-to-daemon synthetic conversation produces a valid evidence-backed draft. Git continues during a Funes outage. Any live provider run records its separately authorized spend and scope. |

Keep regression tests for plausible behavioral failures, not source-text wiring assertions. Use synthetic fixtures; do not copy private session bodies into repositories. Record which scenarios ran and which remain unverified. Existing historical test counts are not acceptance evidence for this change.

## 9. Completion record

Documentation of the agreed design is complete. All implementation phases above remain unstarted. No Funes fork has been created or modified by this documentation task, no runtime integration has been changed, and no live-memory or model-generation verification has been performed for this plan.
