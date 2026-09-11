# Actomasto — milestone 1 specification

Status: milestone 1 implemented with deterministic acceptance coverage and an authorized synthetic-only Anthropic smoke run. Operational instructions and verification boundaries are in §14. AI-assisted specification, implementation, and review prepared with OpenAI Codex.

Approved next change: [Funes integration design and implementation plan](funes-integration-plan.md). It replaces the three conversation adapters with a maintained Funes fork while preserving the policy and lifecycle contracts below. The integration is not implemented; §14 remains the current operational guide.

## 1. Outcome and scope

A single-user Linux daemon discovers eligible public development repositories, collects the user's committed activity and eligible AI conversations, and produces grounded first-person Mastodon draft suggestions. A CLI lists drafts and their supporting evidence. Desktop notifications announce new drafts and actionable failures without displaying source content.

Prioritize coverage of meaningful shareable moments over minimizing editing. A nonempty batch may produce zero suggestions. Conversation-only discoveries and design decisions qualify. Do not invent feelings, learning, measurements, implementation, or release status; explicitly reported assistant outcomes are accepted evidence, not independently verified facts.

Milestone 1 includes no publishing, Mastodon credentials, web UI, editing, approval, archiving, scheduling, screen capture, or browser/desktop activity collection. Purging stored content is included. All three conversation adapters—Claude Code, Codex, and Oh My Pi—are required for initial delivery; ambiguous individual sessions may be excluded.

## 2. Architecture and runtime

- Python 3.12+, managed through uv; no system-Python package installation. Package the `actomasto` CLI with locked dependencies and an absolute executable path in its user-service unit.
- SQLite is authoritative for configuration revisions, lifecycle, checkpoints, pending context, spending, suggestions, and evidence. Enable foreign keys, WAL, and full synchronous commits. One daemon writer owns a process lock; offline mutating CLI operations acquire the same lock.
- Versioned JSONL is a derived suggestion export, appended during normal operation and atomically rebuilt after purge or recovery. CLI reads authoritative SQLite, not an export that may lag.
- Separate modules for discovery/visibility, Git collection, each transcript adapter, policy filtering, queue/generation, persistence/export, and CLI/service integration. No network listener, plugin system, or distributed worker.
- Systemd user service runs while logged in; do not enable lingering. Persist logical on/off independently of process liveness. Logout, crash, and reboot are downtime, not explicit off.
- A local Unix control socket under `$XDG_RUNTIME_DIR/actomasto/` serializes commands with dispatch. Socket and directory are user-only. No TCP control endpoint.
- Service remains available for status, control, and expiry maintenance while logically off; it does not inspect source content or make model requests. At login it resumes collection only if enabled.
- Existing systemd lingering must not cause collection without an active user login session. Detect session absence, suspend source work, and treat it as downtime; do not alter the user's global lingering setting.

### Files and permissions

Use XDG locations, with normal home-directory fallbacks:

| Location | Content |
| --- | --- |
| `$XDG_CONFIG_HOME/actomasto/config.toml` | Non-secret settings and policy |
| `$XDG_DATA_HOME/actomasto/state.sqlite3` | Authoritative state |
| `$XDG_DATA_HOME/actomasto/suggestions.jsonl` | Derived export |
| `$XDG_CONFIG_HOME/actomasto/credentials.env` | Explicitly provisioned Anthropic credential, service-readable |
| systemd user journal | Content-free operational logs |

Directories mode 0700; files, SQLite sidecars, and export temporaries mode 0600; process umask 0077. Credentials come from `ANTHROPIC_API_KEY` or the explicit service environment file; never discover credentials in repositories, transcripts, shell history, or unrelated secret stores. Never include credentials in CLI arguments, configuration output, logs, or exports. Installation is disabled by default and does not enroll or transmit until configured and explicitly switched on.

## 3. Configuration and consent

Configuration version 1 has these settings. Unknown keys and invalid values fail validation; a running daemon retains its last valid revision and reports rejection rather than partly applying changes.

| Setting | Default / contract |
| --- | --- |
| `discovery.roots` | Required explicit absolute parent directories; no default home scan |
| `identity.author_emails` | Required explicit nonempty list of Git author emails |
| `blocklist.repositories` | Empty list; globs over canonical `host/namespace/project` |
| `blocklist.paths` | Empty list; globs over repository-relative POSIX paths |
| `blocklist.text` | Empty list; case-insensitive literal strings |
| `sources.claude_root` | `~/.claude/projects` |
| `blocklist.scoped` | Empty list of tables with required `repository` glob and optional `paths`/`text` lists; same matching rules as global lists |
| `sources.codex_root` | `~/.codex/sessions` |
| `sources.omp_root` | `~/.omp/agent/sessions` |
| `generation.model` | `claude-haiku-4-5-20251001` |
| `generation.character_limit` | 500 Unicode code points; configurable 1–5000 |
| `generation.interval_minutes` | 30; positive integer |
| `budget.monthly_usd` | 20.00; nonnegative decimal |
| `budget.timezone` | System IANA timezone captured at setup; explicit UTC fallback |
| `notifications.enabled` | true |

Setup may suggest Git identities from configuration but requires explicit acceptance; do not attribute commits by display name or committer identity. Email matching is trimmed, case-insensitive, and not expanded via mailmap aliases unless the resulting addresses are explicitly configured.

Turning on authorizes automatic enrollment of verified-public repositories under configured roots and hosted processing of both eligible committed content and conversations. The consent screen must state: new repositories are included automatically, each receives a seven-day import, unpushed commits qualify, conversations need not be public, and filtering cannot guarantee confidentiality. Changing roots expands consent only through an explicit configuration apply operation with that scope displayed. Configure blocklists before first enablement.

There is no blanket ban on client names, internal URLs, issue IDs, or unreleased details within eligible content. User blocklists and mandatory secret filtering replace that earlier policy. No per-repository approval is required.

## 4. Discovery, identity, and public verification

Scan roots every five minutes while collection is allowed; run an initial scan on enable/resume. Traverse without following directory symlinks, skip `.git` internals, and recognize `.git` directories and linked-worktree files. Do not run project hooks, external diff tools, textconv, arbitrary remote helpers, builds, or Git fetch. Read Git objects rather than worktree file contents. Nested repositories and submodules are separate discovery candidates, never recursively folded into their parent diff.

Resolve paths canonically and require containment in a configured root. Worktrees and duplicate clones of the same verified host project share repository identity and deduplication; retain separate local-path/checkpoint records. A blocked local path cannot grant eligibility through another path. Bare repositories are excluded with a visible reason in milestone 1.

`origin` is authoritative. No origin, multiple conflicting origin fetch URLs, unsupported host, or unverifiable visibility means no enrollment. Other remotes cannot grant eligibility. Normalize standard HTTPS, scp-style SSH, and `ssh://` URLs; remove `.git` and userinfo, reject credentials in URLs, and never execute URLs. Host allowlist:

| Git host | Public check |
| --- | --- |
| `github.com` | HTTPS `api.github.com/repos/{owner}/{repo}`; require explicit `private: false` |
| `gitlab.com` | HTTPS `/api/v4/projects/{URL-encoded namespace/project}`; require `visibility: public` |
| `gitlab.wikimedia.org` | Same GitLab endpoint and visibility field |

Wikimedia SSH origins at `gitlab-ssh.wikimedia.org` map to `gitlab.wikimedia.org`; the live API advertises this hostname. Host project numeric IDs, namespaced by API host, define durable identity; normalized names are display fields. A confirmed origin identity change invalidates its cache and pending context before any new transmission. Do not follow cross-host redirects or infer visibility from HTML or successful cloning. Anonymous checks are sufficient for milestone 1; rate limits fail closed.

Cache successful checks for at most 24 hours, keyed by origin identity. Recheck before collection and every external content-bearing request. A fresh result explicitly reporting private/internal invalidates a still-unexpired public result and immediately discards that repository's pending context. Temporary network failures, 401/403/404, rate limits, or malformed responses are uncertainty, not proof of privacy: after cache expiry pause collection/transmission and hold context only until its original expiry. Retry checks at 1, 5, then 30-minute intervals, respecting longer Retry-After values. A repository may become private during the accepted cache window; this is an acknowledged tradeoff.

Blocklisting a repository, removing its root, or changing its origin revokes eligibility immediately: stop collection/new transmission, cancel local work where possible, discard queued context, retain existing suggestions. Re-enrollment never resets history or reimports revoked intervals. Narrower blocklist changes invalidate affected queued source units and revalidate all queued work before dispatch.

## 5. Time, history, and collection

Persist UTC event timestamps and half-open intervals `[start, end)`. Budget months use the configured timezone; collection comparisons use UTC. Missing/malformed event timestamps exclude the item with a reason. Future timestamps are deferred until their time, never advanced into the processing checkpoint as completed. A clock moving backwards must not reopen off/skipped intervals; pause dispatch and report clock uncertainty until a consistent wall-clock boundary can be established.

Each repository's first successful enrollment fixes a single seven-day import window ending at enrollment time. Persist the window before reading content. Apply any already-recorded global off, budget-paused, and repository-revoked intervals inside that window. Before first application enablement there is no artificial off interval excluding the expressly authorized backfill. Newly discovered repositories get their own one-time import; reapproval, duplicate clones, purge, and restart do not reset it.

After import, recover unseen eligible activity from enrollment onward when logically enabled, including downtime. Explicit off and budget-paused intervals are permanently excluded. Visibility uncertainty and adapter outages permit later collection of previously uncollected eligible activity once resolved; expiry tombstones still forbid reloading expired items. Filesystem modification time and discovery time are not event time.

### Git

Enumerate commits reachable from all local `refs/heads`, `refs/remotes`, and `refs/tags`; peel tags. Exclude dangling/reflog-only history. Filter by configured author emails and **committer timestamp**. This intentionally follows Git's recorded commit time, which rebases can change, rather than proving when work occurred.

Read commit messages and committed textual diffs. Root commits compare with the empty tree; ordinary commits with their parent; merge commits use combined diffs to capture merge-specific resolutions, not replay all merged changes. An authored message-only commit can qualify. Binary payloads, LFS object downloads, and submodule contents are never sent; a safe path/change-kind summary may describe them.

Exclude environment/credential files and ignored paths even when committed. Apply committed `.gitignore` rules at the relevant commit plus local exclusion metadata conservatively; do not read uncommitted ignore-file contents as source. Mandatory excluded basenames/patterns include `.env`, `.env.*`, `.dev.vars`, `.secrets`, `*.pem`, `*.key`, `credentials*`, and recognized service-account credential JSON. `.example`/`.template` placeholders may be eligible only after normal secret scanning. Sensitive paths never appear in generated context. A commit touching any blocked or mandatory-sensitive path is excluded in full, including its message.

### Conversations

Use recorded directory metadata to map each message/turn to the deepest enclosing discovered eligible repository. A parent directory containing multiple repositories is insufficient. Never infer association from topics, referenced paths, or tool commands. Exclude mixed/ambiguous turns rather than attaching them to one repository.

A turn is a genuine user message followed by its ordinary assistant progress/final text until the next genuine user message or a verified completion event. Include text only; exclude reasoning/thinking, tool calls/results, images, attachments, system/developer instructions, synthetic summaries, subagent forwarding, and harness-injected content identifiable by adapter metadata or known framing. Do not assume every user-role record was user-authored. Unknown provenance is excluded, not guessed.

Evaluate each message timestamp against eligible intervals; if any part of a turn crosses an off/revoked boundary or changes project association, exclude the whole turn. Never collect a partially off-period turn on resume. Use completion events where available; otherwise wait for the next genuine user message or session termination. An idle session without a reliable completion marker can leave its final turn pending at the source; status reports this instead of guessing completion from inactivity.

Ordinary conversation text may quote tool results, private material, or inaccurate assistant outcomes. Secret/blocklist filtering still applies, but does not prove provenance or confidentiality. Accepted assistant-only factual claims are tagged `assistant_reported` in evidence; drafts may state them without extra corroboration, as explicitly chosen in the interview.

### Adapter feasibility evidence and delivery gate

Metadata-only inspection of local JSONL samples found:

| Client | Observed boundaries | Parser contract |
| --- | --- | --- |
| Claude Code | `user`/`assistant` records have `cwd`, IDs, timestamps; content includes `text`, `thinking`, `tool_use`, `tool_result` | Include genuine text blocks only; apply per-record cwd and synthetic/sidechain flags |
| Codex | `session_meta.payload.cwd`, `turn_context.payload.cwd`; response items distinguish user/assistant/developer | Track turn context; use canonical response messages, not duplicate event-stream copies; exclude function output and injected context |
| Oh My Pi | Session header `cwd`; message records distinguish user/assistant/toolResult and text/thinking/toolCall | Use session metadata only where unambiguous; respect parent/message IDs and known context-injection framing |

This verifies necessary fields exist, not complete format support. Before accepting each adapter, produce sanitized fixtures for current local versions and prove role/provenance filtering, turn completion, timestamps, branches, truncation/rotation, and project association. Record supported version/schema fingerprints in code. Unknown content-bearing schema pauses only that adapter, sends a failure notification, and leaves other verified sources running. Unknown harmless metadata can be ignored explicitly. All three must pass this gate before milestone 1 is considered delivered; never claim arbitrary future client compatibility.

Read streams incrementally by file identity, byte offset, and stable record IDs. Incomplete trailing JSON waits for completion; malformed complete records quarantine that stream and report an error. Replacement/truncation rescans safely through deduplication. Missing native record IDs use a stable session/branch/index/content digest, not file mtime. Follow explicit branch lineage; do not emit duplicated shared ancestors or synthetic branch summaries.

## 6. Blocklists and secret filtering

Apply rules before persisting pending content and before any Anthropic request, including token-count requests. Revalidate against current policy at dispatch. Order: repository/path eligibility, literal text exclusion, secret redaction, bounded packaging. Blocklist matching occurs on original eligible text, before redaction can hide a match.

Repository/path globs use case-sensitive POSIX semantics: `*` matches within one path component, `**` spans components, `?` one non-separator character. Match paths from the repository root, including both sides of a rename. Literal text rules use Unicode casefolded substring matching; reject empty rules. Rules may be global or scoped to canonical repository identity. CLI validation must preview matched metadata locally without printing matched content.

A path/text match anywhere in a commit excludes the whole commit, including its message. A text match anywhere in a conversation turn excludes the whole turn. Adjacent turns remain eligible: this is not a guarantee against topic discussion without repeating the blocked phrase. Detection must cover the entire unit before splitting; a unit too large to safely inspect is excluded, not partially transmitted.

Mandatory local secret detection covers private-key blocks, known provider token formats, credential-bearing URLs, and quoted/unquoted assignments to password/token/secret/API-key fields. Use a versioned maintained detector/rule set, pinned in the lockfile, with network verification disabled. High-entropy detection supplements rather than replaces known-format rules. Replace detected values with `[REDACTED_SECRET]` and keep the remaining eligible context. Remove sensitive absolute paths from conversation text with `[REDACTED_PATH]`. Store only redacted excerpts. Detector failure blocks the unit; no raw fallback. Recheck generated text for secrets and user text-blocklist matches before saving it.

Source text is untrusted data, never instructions. Generation receives a fixed system contract, labeled evidence, no execution tools, and no external browsing. Filtering is best-effort; accepted public-source and conversation policies can still disclose private content to Anthropic. Already-transmitted requests cannot be recalled.

## 7. Queue, scheduling, and lifecycle

Discovery/collection run every five minutes; generation batches new eligible activity every 30 minutes. Use one repository per request/draft, one external generation request in flight globally, oldest-expiring ready context first, round-robin across repositories for equal expiry. No eligible content means no model or token-count request. A delayed cycle runs once, not once per missed timer tick.

Pending context is redacted, bounded to 100 MiB globally and 10 MiB per repository, with at most 10,000 units globally. Expire oldest units first by deadline; when still full, reject new units with `queue_full` terminal markers and notify rather than silently evicting retained context. Limits count UTF-8 payload bytes. Stream source reads; maximum individual raw commit/turn inspection size is 32 MiB, exceeding which produces `oversized_source` without transmission.

Each unit expires 24 hours after first collection; retries, splitting, stop/resume, and policy changes do not extend it. Expiry deletes content and preserves only content-free IDs/reasons/counters. Expired activity is permanently skipped; no replay command exists. Perform expiry before every read/dispatch and at least once per minute while the service runs, even logically off.

**Retention limitation:** a powered-off/logged-out machine cannot execute deletion. Context becomes logically unusable at its deadline and is physically removed at the next service start before any source use or transmission. This is not a guaranteed wall-clock physical-erasure service. SQLite secure deletion and WAL checkpointing reduce local residue; filesystem snapshots, backups, SSD remnants, and original client transcripts are outside purge guarantees.

| Event/state | Collection | New model requests | Pending context |
| --- | --- | --- | --- |
| Enabled and eligible | Allowed | Allowed if budget permits | Process normally |
| Explicit `off` | Stopped; interval excluded | Stopped | Preserve to original expiry |
| Crash/logout while enabled | Process absent | None | Expire on next execution; catch up unseen eligible activity |
| Estimated budget pause | Stopped; interval excluded | Stopped | Expire normally; resume unexpired units at reset |
| Visibility uncertainty after cache expiry | Affected repository paused | Affected repository stopped | Hold to original expiry |
| Confirmed private / revoked repository | Affected repository stopped | Affected repository stopped | Discard immediately |
| Adapter incompatibility | Affected adapter stopped | Other eligible sources allowed | Hold affected pending units without dispatch until compatible or expired |

`off` commits its interval and closes the dispatch gate before acknowledging success; cancels local in-flight work where possible. Requests already accepted by Anthropic may finish and incur cost, but responses arriving after stop/revocation/purge are not saved as suggestions. Record cost and keep still-eligible pending context only under its original expiry. A control-generation epoch prevents late responses from resurrecting deleted or disallowed data.

Budget pause ends at month reset only if logically enabled; reset while explicitly off does not enable the daemon. Root removal/repository blocking closes a repository eligibility interval; unblocking starts a new one, not historical catch-up across the blocked interval.

## 8. Generation and spending

Use Anthropic's synchronous Messages API with configurable pinned model, initially Haiku 4.5 for coverage/cost. No automatic downgrade, multi-model selector, extended thinking, server-side tools, prompt caching, or remote Batch API in milestone 1. “Batch” means local grouping, not Anthropic asynchronous batches.

Split input at committed-file-diff and conversation-message boundaries after filtering the complete source unit. Never split a single text item arbitrarily or silently truncate evidence. An item that exceeds the request limit is skipped with `oversized_item`; remaining safe pieces may proceed and carry `partial_source: true`. Each piece inherits the original unit's expiry and stable identity. Package at most 12,000 counted input tokens including instructions and 2,000 maximum output tokens per request. Count only filtered content through Anthropic token counting; a count failure leaves the batch pending, never sends unbounded input.

Ask for strict JSON containing `candidates`, an array of zero to three objects with `text` and `evidence_ids`. Each candidate is one standalone first-person post, within configured Unicode-code-point length, with no automatic hashtags, links, or thread. This local limit is not a promise of every Mastodon instance's eventual length accounting. Evidence stays outside post text. The model may describe progress, tradeoffs, discoveries, or lessons actually supported by supplied material, including accepted assistant reports; do not fabricate connective claims between unrelated changes.

Validate JSON, field types, candidate count, character limit, nonempty text, and evidence IDs belonging to the request. Reject the complete response on malformed output, unknown evidence, overlength, or output-policy failure. Do not silently clip or retain an arbitrary subset. Zero candidates is successful processing, not a retry trigger. Persist model ID, prompt version, source policy revision, and generation timestamp with accepted results.

### Budget ledger

Initial documented rates for Haiku 4.5 are $1 per million input tokens and $5 per million output tokens. Rates are configuration metadata tied to a specific model and checked against official pricing at implementation; an unknown model/rate pauses dispatch. No provider billing guarantee is claimed.

Before dispatch, transactionally reserve `input_token_count * input_rate + max_output_tokens * output_rate` in integer micro-USD, rounding up. Spent plus reservations must remain within the configured $20 calendar-month cap. No request if its reservation will not fit: enter budget pause and skip collection until reset, rather than quietly changing the model. Account for initial import and every retry. Use actual returned usage to settle a reservation; if usage is unknown after possible provider acceptance, retain its full amount as estimated spend. Disable SDK automatic retries; the application owns every billable attempt.

Persist calendar periods using the configured timezone and charge an attempt to its dispatch period, including a request crossing midnight/month-end. Do not reset the ledger on restart, model change, purge, or timezone change. Timezone changes take effect after the currently opened period ends. A raised configured cap may clear a budget pause prospectively; its previously skipped interval remains excluded.

Retry network failures, 429, and 5xx after 1, 5, then 30 minutes, with at most four generation attempts total and no retry after expiry, disablement, or loss of eligibility. Honor longer Retry-After. Authentication/permission/model errors pause generation and notify until configuration is repaired. Malformed output gets one additional generation attempt within the same four-attempt ceiling. Exhaustion records `generation_failed`, deletes context, and preserves a terminal marker. Potentially billed failed attempts remain charged. Retries can incur duplicate provider cost; local transactions guarantee no duplicate accepted suggestions, not exactly-once remote execution.

## 9. Deduplication and storage contracts

Deduplicate underlying activity, not topics. Reprocessing the same commit/message, crash recovery, duplicate clones, and equivalent rebased commits must not regenerate suggestions; genuinely new work on an old topic remains eligible.

Commit exact identity is repository ID plus commit hash. Equivalent-change identity uses repository ID, configured author identity, stable Git patch ID, and normalized commit message; changed messages or changed patches count as new evidence. For empty/merge diffs use canonical message plus canonical combined-diff digest. Git patch identity is a defined approximation, not semantic equivalence for squash/split or conflict-altered rebases. Conversation identity is client/session/branch/native record identity; deduplicate shared ancestors. Persist hashes of processed source IDs rather than raw content.

A queue piece is pending → reserved/in-flight → accepted/no-candidate, or terminal expired/blocked/purged/failed/oversized/queue-full. Return to pending only on an allowed retry; terminal states never auto-requeue. Commit source markers, suggestions/evidence, attempt settlement, and export-outbox entries atomically. A source may support several candidates from the same accepted response but cannot be fed into a new generation later.

### SQLite logical schema

Implement versioned migrations and these required entities; names may vary but invariants may not:

| Entity | Required state and constraints |
| --- | --- |
| `settings` | schema/config revision, enabled flag, control epoch, timezone/current budget period |
| `intervals` | scope (global/repository), kind, UTC start/end; non-overlapping same-kind intervals |
| `repositories` | host/project ID unique, display path, local paths, current origin identity, public check/expiry, import window/progress, eligibility state |
| `source_cursors` | adapter/version, local file/object identity, byte offset/branch position, last success/error |
| `source_markers` | repository + source digest unique, equivalent-change digest, terminal reason, event time; no excerpts |
| `pending_units` | stable unit/piece IDs unique, repository, redacted payload, policy/adapter revision, collected/expiry, state, attempt count |
| `attempts` | request ID unique, unit IDs, model/rates, period, reservation, usage/estimated charge, result state |
| `suggestions` | UUID unique, monotonically increasing export sequence unique, repository, draft, created time, model/prompt/policy versions |
| `evidence` | UUID, suggestion FK, source kind/reference, event time, redacted excerpt, provenance, partial flag |
| `export_outbox` | suggestion/sequence unique, exported state |
| `events` | bounded content-free operational events and notification delivery state |

Retain operational event details for 30 days and aggregate counters thereafter; retain budget totals and processing markers across purge. No full processed transcripts or prompts in successful attempt rows. Evidence is up to eight supporting excerpts of at most 1,000 Unicode code points each per suggestion, with exact source references; select contiguous redacted excerpts rather than model-invented quotations.

### JSONL version 1

One UTF-8 JSON object per line, ordered by `sequence`. Required keys:

```json
{
  "schema_version": 1,
  "id": "UUID",
  "sequence": 1,
  "created_at": "RFC3339 UTC timestamp",
  "repository": {"id": "host:project-id", "host": "github.com", "path": "owner/project"},
  "text": "Standalone draft text",
  "character_limit": 500,
  "generation": {"model": "claude-haiku-4-5-20251001", "prompt_version": 1, "policy_revision": 1},
  "evidence": [{
    "id": "UUID",
    "kind": "commit",
    "source_ref": {"commit": "full object ID"},
    "occurred_at": "RFC3339 UTC timestamp",
    "provenance": "committed",
    "partial_source": false,
    "excerpt": "Redacted supporting excerpt"
  }]
}
```

For conversation evidence, `kind` is `conversation`; `source_ref` contains `client`, `session_id`, and `message_ids`, without absolute transcript paths. Provenance is `user_reported` or `assistant_reported`; split evidence entries when roles differ. Evidence IDs are local, not user-visible URLs. No approval, account, or publication state is implied by this schema.

Exporter fsyncs complete lines, then acknowledges outbox entries. On recovery validate exported IDs/sequences against SQLite, discard a partial final line, and rebuild atomically if duplicates/divergence exist before resuming append. Do not assume append and SQLite commit can be one atomic operation. CLI/export status reports lag or rebuild failures. Consumers upsert by UUID, reject unsupported major schemas, and tolerate sequence gaps after purge.

## 10. CLI, notifications, and purge

Required commands:

| Command | Behavior |
| --- | --- |
| `actomasto init` | Configure roots, identities, timezone, hosted-processing consent; starts disabled |
| `actomasto config validate` | Validate file without applying or transmitting |
| `actomasto config apply` | Atomically apply explicit changes and revoke affected pending work |
| `actomasto service install` / `uninstall` | Install/remove user unit only; uninstall preserves data and records off |
| `actomasto daemon` | Foreground service entry point, singleton lock enforced |
| `actomasto on` / `off` | Persist logical state with acknowledged collection/dispatch barrier |
| `actomasto status [--json]` | Process vs logical state, eligibility, source health, queue expiry, budget/resume time, skips/errors, export/notification health |
| `actomasto repos [--json]` | Discovered IDs/paths, inclusion/exclusion reason, origin/public-check age, import state |
| `actomasto list [--repo ID] [--since RFC3339] [--limit N] [--json]` | Newest first, default 20; stable IDs, draft text and metadata |
| `actomasto show ID [--evidence] [--json]` | One draft; evidence printed only when requested (JSON follows the same flag) |
| `actomasto purge (--repo ID | --all) [--yes]` | Destructive preview/confirmation, content deletion, no history reset |

Normal exit 0, operational failure 1, invalid arguments/configuration 2, missing requested ID 3. JSON mode produces valid machine-readable objects without ANSI codes; human output escapes terminal control characters from all source-derived strings. Status is observational; it never enables, enrolls, or generates.

Use freedesktop desktop notifications via the user session bus. Coalesce draft notifications per generation cycle with counts only. Notify once per transition into an actionable failure or pause, and once on recovery; do not repeat identical failures each poll. Missing notification service degrades visibly to CLI/journal, never prevents generation and never triggers a notification-error loop. No notifications include draft text, excerpts, credentials, blocked matches, or private paths.

Purge first increments the control epoch and cancels matching in-flight work, then deletes selected suggestions/evidence and pending context transactionally, marks queued units purged, and atomically rebuilds the export. It retains processing markers, import state, collection checkpoints, budget ledger, and content-free counters. Future eligible activity remains enabled. Neither purge nor removing the export triggers backfill. Report success only after database deletion and export replacement; expose/retry an interrupted rebuild without regenerating deleted content. Purge does not delete original Git/client histories, copies already imported elsewhere, backups, or provider-held requests.

## 11. Acceptance criteria

Implementation completion requires every scenario below, not merely a running daemon. Use deterministic fixtures for policy/state boundaries and an explicitly authorized live Anthropic smoke run for real integration. Never send real private transcripts to test adapter correctness.

1. Initial setup stays disabled; configured roots and combined hosted-processing consent are required before automatic enrollment.
2. Discover public repositories on all three supported hosts, including Wikimedia SSH hostname normalization; private, missing-origin, unsupported, and uncertain repositories cannot transmit without a valid cache result.
3. A public upstream does not authorize a private origin; origin/root/blocklist changes invalidate queued work before dispatch.
4. Collect only configured authors across local branches, remote-tracking branches, and tags; include unpushed work, exclude reflog-only history and unrelated authors.
5. Each repository imports seven days once; duplicate clones, purge, disable/re-enable, and rediscovery do not restart import.
6. Committer-time and message-time off-period exclusions survive delayed discovery, restart, and timezone/month boundaries; enabled downtime catches up without reviving terminal items.
7. All three adapters extract genuine user/progress/final text, exclude raw tool/reasoning/injected records, and handle project metadata, duplicate event copies, branch lineage, rotation, and incomplete writes. Ambiguous turns are excluded.
8. Adapter incompatibility pauses only the affected adapter and notifies; all three verified adapters are nevertheless mandatory for initial release.
9. Repository/path/literal blocklists exclude complete commits or turns before persistence/transmission; secret matches are redacted, detector failure has no raw fallback, and quoted tool output receives the same filtering.
10. A conversation-only discovery and an eligible committed change can each produce a grounded draft; assistant-only claims are accepted with recorded provenance. Routine activity can validly return zero candidates.
11. Drafts remain single-repository, within configured length, with valid evidence references; malformed/overlength/blocked responses never become suggestions.
12. Empty batches make zero content-bearing provider calls; split inputs retain attribution/expiry, report partial evidence and unfit items, and stay within limits.
13. Off acknowledgment prevents subsequent dispatch; late responses after stop/revocation/purge cannot create suggestions. Already-accepted remote requests remain accounted for.
14. Context expires logically after 24 hours even while off, is removed before use on restart, and never reappears through catch-up. Queue pressure and terminal failures are visible.
15. Spending reservations account for concurrent state, retries, unknown usage, backfill, restart, and month-crossing requests. Cap exhaustion pauses both collection and generation; paused intervals remain skipped after reset.
16. Exact replay, equivalent rebase, duplicate clone, zero-candidate replay, and crash retry cannot create duplicate accepted suggestions; new activity on a previously covered topic remains eligible.
17. Crash between SQLite commit and JSONL append/acknowledgment recovers an exact export without lost/duplicated IDs. Corruption is reported, never silently accepted.
18. CLI lists/shows/filter drafts and evidence, distinguishes process state from enabled state, and emits valid JSON. Rendered text cannot inject terminal control sequences.
19. Purge removes selected drafts/evidence/queue/export content without budget or processing-history reset; in-flight completion cannot resurrect it; unrelated repositories remain intact.
20. Login starts the user service, logout suspends collection, and next login respects logical enabled state. No lingering is enabled by installation.
21. Desktop notifications report draft availability and failure/recovery transitions without sensitive text; absent notification support degrades visibly without blocking work.
22. No publication path, Mastodon token handling, tool execution by the model, or unapproved desktop/browser capture exists.

## 12. Later milestone boundaries

Milestone 2: localhost-only single-user web review; import versioned records by UUID. Edit, approve, archive, and individually delete suggestions. Archive retains a suggestion without publishing; delete removes draft and evidence. Detailed UI is deferred. Purge affects only local milestone-1 storage, not independently imported copies.

Milestone 3: publish approved drafts to one configured test or real Mastodon account at a time. Approval is destination-bound and does not transfer on account switch. Publishing failure, scheduling, and instance-specific length rules are deferred. Unattended draft selection is not authorized.

Desktop/browser metadata and selected screen captures require a separate future privacy/permissions decision; none is enabled initially.

## 13. Evidence and implementation entry conditions

At specification time, no application or Git repository was present. Implementation now lives in the public `audiodude/actomasto` repository. Real runtime setup still requires user-chosen discovery roots, author identities, and explicit Anthropic credential provisioning. Desktop notification availability affects notification delivery, not collection correctness. Implementation and testing did not enroll the developer's real repositories or transmit real conversations.

Verified during specification: structural fields in local Claude Code, Codex, and Oh My Pi transcript samples; Wikimedia's public GitLab API and advertised SSH host; Anthropic's documented Haiku model ID and base rates. Implementation verification is recorded below; it does not establish compatibility with arbitrary future client schemas.

References:

- Anthropic model IDs and limits: https://platform.claude.com/docs/en/models/overview
- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing
- GitLab project visibility API: https://docs.gitlab.com/api/projects/
- Wikimedia public project API: https://gitlab.wikimedia.org/api/v4/projects?visibility=public&per_page=1

Implement milestone 1 against this specification. If a required adapter cannot meet the provenance/project boundaries, or an implementation fact requires changing a product/privacy decision, report the exact conflict for resolution rather than reducing scope or claiming compatibility. Later milestones remain deliberately undesigned beyond their stated contracts.

## 14. Running the implementation

### Installation and consent

Requires Linux, Python 3.12+, uv, Git, systemd/logind, and `notify-send` for desktop notifications. Python dependencies are pinned in `uv.lock`; no system-Python installation is needed.

```sh
uv sync --locked
uv run actomasto init --root /absolute/path/to/projects --author-email you@example.com
uv run actomasto config validate
uv run actomasto service install
```

Repeat `--root` and `--author-email` for multiple explicit values. `init` displays hosted-processing consent and requires confirmation; noninteractive use requires `--accept-hosted-processing`. Setup and service installation do not turn collection on. The installed unit contains the absolute CLI executable path, so retain this checkout and its `.venv`, or reinstall the unit after moving them. Installation preserves XDG locations and does not enable lingering.

Provision `ANTHROPIC_API_KEY` in the launch environment or put a single `ANTHROPIC_API_KEY=...` assignment in `$XDG_CONFIG_HOME/actomasto/credentials.env` (default `~/.config/actomasto/credentials.env`), mode 0600. The daemon itself reads this explicit file and checks ownership/permissions; it does not source a shell or inspect unrelated credential stores. A systemd user service does not automatically inherit a key from an interactive shell.

Edit `config.toml` to set blocklists before enabling. Configuration file edits remain unapplied until:

```sh
uv run actomasto config validate
uv run actomasto config apply
uv run actomasto on
```

`config validate` previews matching repository/path metadata without enrolling repositories or reading matching text. Sensitive filenames are counted rather than displayed. `config apply` displays the proposed root scope and requires confirmation (`--yes` for automation). `on` displays consent again (`--accept-hosted-processing` for automation). Scoped rule syntax:

```toml
[[blocklist.scoped]]
repository = "github.com/example/**"
paths = ["private/**"]
text = ["literal phrase to exclude"]
```

### Operation

```sh
uv run actomasto status --json
uv run actomasto repos --json
uv run actomasto list --limit 20
uv run actomasto list --repo github.com:123 --since 2026-09-01T00:00:00Z --json
uv run actomasto show DRAFT_UUID --evidence --json
uv run actomasto off
uv run actomasto purge --all
uv run actomasto service uninstall
```

Use IDs returned by `repos` and `list`, not the illustrative IDs above. `show` omits evidence unless explicitly requested, including in JSON mode. Purge requires confirmation (`--yes` for automation); `--repo ID` limits it to one repository. Uninstall records off and preserves data. Foreground operation is `uv run actomasto daemon`. Logical on/off and process liveness are separate.

Control commands close the authorization gate before waiting for existing readers/requests to finish. An already-started synchronous HTTP request can delay acknowledgment until its finite timeout; expiry maintenance continues during this wait. Already-sent content cannot be recalled. Failed usage-unknown attempts remain conservatively charged.

Unknown adapters fail closed independently. Supported observed fingerprints are Claude Code 2.1.220, 2.1.221, 2.1.223, 2.1.224, 2.1.226–2.1.229, 2.1.231–2.1.233, 2.1.260 and 2.1.263; Codex 0.144.1; and Oh My Pi session schema 3. Unlisted Claude releases are not assumed compatible. Claude's schema-2 checkpoint fingerprint retries previously unsupported streams while retaining deduplication markers. Known history/UI/tool metadata is excluded from evidence. Explicit human provenance remains required, including in background (`sessionKind=bg`) sessions; sidechains, transcript-only messages, interrupted/aborted turns and ambiguous supersession are excluded. Explicit Claude/OMP parent branches and identical-session replay are covered. No inspected Codex sample demonstrated a fork-bearing schema: unrecognized lineage metadata pauses that adapter rather than inventing ancestry. No arbitrary future-version compatibility is claimed.

### Review decisions and verification

The specification review made scoped-rule configuration explicit, separated local-path authorization from shared project identity, required fixture-backed completion/provenance, and conservatively settled crash-interrupted attempts. Independent implementation review additionally caught and corrected dispatch/acknowledgment races, stale collection after policy changes, retry scheduling, transient Git failures becoming terminal, health-event mismatches, and systemd target ordering.

Validation command:

```sh
uv run pytest -q
```

Recorded 2026-09-06:

- **192 deterministic tests passed**, covering the §11 policy, adapter, storage, budget, lifecycle, export, and CLI boundaries. Fixtures contain authored synthetic content only.
- Actual isolated CLI/Unix-socket daemon exercised disabled initialization, apply, on/off, status, filtering/list/show, evidence opt-in, purge, clean shutdown, and restart preserving off. The developer's real source roots were never enabled.
- The generated user unit passed `systemd-analyze --user verify` with no diagnostics. Active-login detection and desktop notification transport were exercised locally; logout/lingering transitions were covered by deterministic logind fixtures, not by logging out the developer.
- Authorized live Haiku requests produced one draft from an unpushed synthetic commit and one from a synthetic conversation-only design discovery, with valid evidence and an empty processed queue. All live attempts, including diagnostic failures, totaled **$0.008310 in usage-based estimated cost**, below the authorized $0.10 ceiling.
- The live run exposed Markdown-fenced responses despite prompt instructions. Generation now uses Anthropic's supported `output_config.format` JSON schema for both counting and generation, while retaining complete local validation. Invalid responses remain rejected, never stripped or silently repaired.

Recorded 2026-09-07:

- Fixed adapter failure isolation: marking one adapter unhealthy no longer changes the global cancellation epoch. Per-unit adapter health still blocks that source at dispatch and settlement; unrelated sources and in-flight results remain usable.
- **195 tests passed**, including regressions that reproduced cross-adapter collection cancellation and unrelated-result rejection before the fix.
- An isolated real CLI/daemon run with an unsupported synthetic Claude record continued through Codex and OMP to generation's authentication gate. Credentials were deliberately absent; no model request was sent. This does not verify live draft generation or add support for older Claude versions.
- Subsequently added compatibility for all thirteen Claude versions observed in the local archive, including older metadata and exclusion flags, without relaxing human-provenance checks. **203 tests passed**; synthetic collection passed for every supported version, including human-authored background sessions.
- A local, non-transmitting scan completed all **162 Claude streams** with no adapter errors or quarantines. The scan used no eligible repositories, emitted no source units and persisted no source content. Synthetic fixtures separately verified evidence extraction, exclusions and checkpoint-upgrade deduplication. This does not verify live model generation.

Structured-output API reference:
https://platform.claude.com/docs/en/build-with-claude/structured-outputs
