# Actomasto

Project recommendations, personal briefings, and Mastodon drafts from your development activity. Actomasto does not publish posts.

## Quick reference

Run these commands in an installed environment. From a checkout, prefix them with
`uv run --locked` (for example, `uv run --locked actomasto suggest`).

| I want to… | Command |
| --- | --- |
| Find a project to work on | `actomasto suggest` |
| Review yesterday | `actomasto briefing run daily` |
| Review the past week | `actomasto briefing run weekly` |
| Pick up a specific project | `actomasto briefing run reentry --project my-project` |
| See Mastodon drafts | `actomasto list` |
| Read a draft and its evidence | `actomasto show ID --evidence` |
| Check draft collection | `actomasto status` |
| Pause / resume draft collection | `actomasto off` / `actomasto on` |

## Project suggestions

```sh
actomasto suggest
actomasto suggest --json
actomasto suggest --dry-run
```

Suggests up to three existing projects, each with a reason to work on it and a
next action. Recent work and dormant projects can both qualify. Recorded unfinished
work is preferred; project context can also support a clearly labelled new idea.
Age alone is not a reason to recommend a project, and new ideas are not presented
as your recorded plans.

Uses the [briefing configuration](#briefing-configuration), with bounded recent
Git/conversation history and current working-tree/planning context. It prints
locally and never sends email. `--json` includes the archived result and supporting
evidence; `--dry-run` shows filtered local evidence without calling the model or
writing an archive, and works with hosted processing disabled.

One result is archived per local day. Repeating the command reuses that result
without another model request. Suggestions share the briefing model budget.

## Personal briefings

| Mode | Contents |
| --- | --- |
| `daily` | Previous local calendar day, plus up to two projects worth reopening |
| `weekly` | Seven complete local days, decisions, and work worth reopening |
| `reentry` | One project's last recorded work, decisions, and a supported next action |

```sh
actomasto briefing run daily
actomasto briefing run weekly
actomasto briefing run reentry --project my-project
actomasto briefing run daily --send
actomasto briefing run daily --dry-run
actomasto briefing status
actomasto briefing show daily-2026-09-24
```

- Without `--send`, reports are printed and archived only.
- `--json` returns the complete archived result. `--dry-run` collects local
  evidence only and cannot be combined with `--send`.
- `--project` accepts a name or canonical ID such as
  `git:github.com/owner/repository`; ambiguous names fail.
- `--date YYYY-MM-DD` selects the delivery date, not the recap date. Daily and
  weekly history remains bounded by the live collection window.
- Repeated runs reuse the same mode/date/project archive. Configuration changes
  invalidate reuse. Failed or uncertain generation and delivery attempts are not
  automatically retried; inspect `briefing status` before recovery.

### Email and scheduling

Configure Mailgun, then check transport before enabling scheduled delivery:

```sh
actomasto briefing test-delivery     # Mailgun test mode; no email delivered
actomasto briefing service install
actomasto briefing service disable
```

Installed timers send **daily at 06:00** and **Monday at 06:15**, in the configured
timezone. They skip missed runs while the machine is asleep/offline. Keep the
checkout and its virtual environment available; reinstall after moving them.
Mailgun acceptance does not guarantee inbox delivery.

**`actomasto off` pauses drafts, not briefings or suggestions.** Disable briefing
timers with `briefing service disable`; set `hosted_processing` to `false` to
also prohibit manual model requests. To cancel an in-flight scheduled report,
stop its `actomasto-briefing-daily.service` or `actomasto-briefing-weekly.service`
with `systemctl --user stop`.

## Setup

Requires Linux, Python 3.12+, uv, and Git. Background services additionally use
systemd; conversation sources require the maintained
[audiodude/funes](https://github.com/audiodude/funes) fork with source protocol 1.

```sh
uv sync --locked
```

### Briefing configuration

Create `~/.config/actomasto/briefings.json` (or
`$XDG_CONFIG_HOME/actomasto/briefings.json`):

```json
{
  "version": 1,
  "roots": ["/home/you/code"],
  "author_emails": ["you@example.com"],
  "timezone": "America/Los_Angeles",
  "hosted_processing": true,
  "monthly_usd": "5.00",
  "blocklist": {"repositories": [], "paths": [], "text": []}
}
```

Use your actual absolute project roots and Git author emails. Set configuration
directories to 0700 and credential/configuration files to 0600.
`hosted_processing: true` authorizes sending filtered evidence in this scope to
Anthropic. Filtering cannot guarantee confidentiality; review scope and
blocklists first.

Provide `ANTHROPIC_API_KEY` through the environment or
`~/.config/actomasto/briefing-credentials.env`, using `NAME=value` lines. The
existing collector's `credentials.env` can also supply the Anthropic key.
Never put keys in CLI arguments, JSON configuration, or the repository.

Optional configuration fields:

| Field | Use |
| --- | --- |
| `remote_hosts` | Read-only SSH discovery, e.g. `[{"name":"your-mac.local","root":"~/code"}]`; requires trusted host keys, remote Python 3 and Git |
| `inventory_db` | Absolute path to an existing code-inventory SQLite database, used for discovery context |
| `funes` | `{ "executable": "/absolute/funes", "corpus": "/absolute/corpus", "scope": "/absolute/enrollment.json" }` |
| `conversation_harnesses` | Explicitly opted-in subset of `["claude","codex","omp"]`; requires `funes` configuration and an independently refreshed corpus |
| `mailgun` | `{ "domain": "mail.example.com", "sender": "reports@example.com", "recipient": "you@example.com", "region": "US" }`; region is `US` or `EU` |

Email additionally requires `MAILGUN_API_KEY` in the environment or private
`briefing-credentials.env`. Mailgun configuration is unnecessary for local
suggestions and report previews.

```sh
actomasto briefing validate
actomasto suggest --dry-run
actomasto suggest
```

Suggestions and briefings use Claude Haiku 4.5. Each generation reserves $0.10
against the monthly briefing budget, then charges known token usage; uncertain
usage retains the reservation. Archives and spending records are private files
under `~/.local/share/actomasto/briefings/` (or
`$XDG_DATA_HOME/actomasto/briefings/`).

### Mastodon draft collection

Draft collection uses separate configuration (`config.toml`) and controls.
Set up Funes enrollment and refresh before enabling collection.

The maintained Funes fork must implement [source protocol 1](https://github.com/audiodude/funes/blob/main/docs/local-source.md). The pinned dependency revision is `eee23d57b6a5895b23898792728c880b3fdd25f8`; do not substitute an upstream binary without these capabilities.

Build Funes independently (previous builds used Rust 1.98.0, protoc, and lld on Linux), then use the absolute path to its `target/debug/funes`. Check out the pinned revision above in a separate Funes source worktree before building; a source commit is not a published binary.

Fetch the maintained `audiodude/funes` fork and check out the exact pin before
building. This revision retains the current-source parser repair and refreshes
compatible dependencies. A branch name or previously installed executable does
not establish that its build matches this revision.

```sh
FUNES_SOURCE=/absolute/funes-worktree
git -C "$FUNES_SOURCE" rev-parse HEAD # must match the pinned revision above
CARGO_BUILD_JOBS=2 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_DEV_OPT_LEVEL=1 \
  CARGO_PROFILE_DEV_INCREMENTAL=false RUSTFLAGS="-C link-arg=-fuse-ld=lld" \
  cargo build --locked --manifest-path "$FUNES_SOURCE/Cargo.toml"
```

Independently create an explicitly approved enrollment file (0600, parent directory 0700):

```json
{"version":1,"roots":{"claude":["/absolute/claude-root"],"codex":["/absolute/codex-root"],"omp":["/absolute/omp-root"]}}
```

Empty arrays enroll no sources. The local corpus directory must be private (0700). An independent operator/indexer must send a `refresh` request at least once per minute; the published bridge specification includes a [standalone user-timer example](https://github.com/audiodude/omp-funes-bridge/blob/e50207d/SPEC.md#standalone-multi-harness-source-inventory). After five minutes without refresh, coverage is `lagging` and conversations pause; Git remains independent. Missing enrolled roots report unavailable coverage, not empty activity. No semantic embeddings, provider access, or active OMP session are needed for this inventory.

```sh
printf '%s\n' '{"protocol":1,"op":"refresh","corpus":"/absolute/local-corpus","scope":"/absolute/enrollment.json"}' | /absolute/funes source
uv sync --locked
uv run actomasto init --root /absolute/projects --author-email you@example.com \
  --funes-bin /absolute/funes --funes-corpus /absolute/local-corpus \
  --funes-scope /absolute/enrollment.json
actomasto config validate
actomasto config apply
actomasto service install
actomasto on
```

Setup starts disabled. Configure blocklists and provision `ANTHROPIC_API_KEY` in
the environment or private `~/.config/actomasto/credentials.env` before enabling.
`notify-send` enables desktop notifications.

```sh
actomasto repos
actomasto list --limit 10
actomasto list --repo ID --since 2026-09-01T00:00:00Z
actomasto show ID --evidence --json
actomasto off
actomasto purge --repo ID           # prompts before deletion
actomasto purge --all
```

`purge` removes draft content, not briefing archives, original Git/conversation
history, Funes data, or spending/processing records.

## Maintenance

- **Update a checkout:** run `uv sync --locked`. If replacing the collector's
  checkout or Funes binary, stop `actomasto.service`, change only the intended
  settings in `config.toml`, run `config validate` and `config apply`, then
  `service install`. Do not use `off` merely to restart the process. Keep the
  checkout and `.venv` available for the installed unit.
- **Migrate v1 configuration:** stop the collector, run `config migrate` with the
  three explicit `--funes-bin`, `--funes-corpus`, and `--funes-scope` paths, then
  start the service. Do not rerun `init` or reset state.
- **Limit draft Git history:** add a `[git]` section with `since = "YYYY-MM-DD"` to
  `config.toml` and run `config apply`. The cutoff is inclusive midnight UTC by
  committer date; it does not affect conversations or import otherwise ineligible history.
- **Run tests:** `uv run --locked pytest -q`. Set `FUNES_TEST_BIN` to an absolute
  maintained-fork executable to include real Funes subprocess tests.

[Detailed operation and architecture](mastodon-activity-app-notes.md#14-running-the-implementation)
· [Funes integration](funes-integration-plan.md)
· [Verification records](verification/)

### Replacing the collector dependency
```sh
uv sync --locked
systemctl --user stop actomasto.service
```

Edit **only** `executable` under `[funes]` in `${XDG_CONFIG_HOME:-$HOME/.config}/actomasto/config.toml` to the absolute path of the rebuilt Funes binary. Leave `corpus`, `scope`, discovery roots, identity, blocklists and budgets unchanged; do not rewrite the independent enrollment file. Review the existing controls with `uv run --locked actomasto status --json`, then apply and install from the new checkout:

```sh
uv run --locked actomasto config validate
uv run --locked actomasto config apply
uv run --locked actomasto service install
uv run --locked actomasto status --json
```

`config apply` asks for hosted-processing/scope confirmation; use `--yes` only after reviewing the unchanged scope. Applying a binary-path-only change preserves enabled/off state, collection intervals, spending, history and enrollment, and makes conversation health fail closed until the new dependency is checked. Like any configuration apply, it clears a generation-error pause; review that state before proceeding. It does not implicitly enable collection with `on`. `service install` writes the unit for the new checkout's virtual-environment executable, reloads systemd and enables/starts the service; stopping first is necessary because installing over a running service does not restart its old process. Keep the checkout and its `.venv` available afterward. These commands are operator rollout instructions, not a record of a performed live update.

## Git history cutoff

To limit commit processing, add this to `config.toml`, then run
`uv run --locked actomasto config apply`:

```toml
[git]
since = "2026-03-01"
```

The cutoff is inclusive midnight UTC, using the **committer date**, not the author
date. Omit the section or use `since = ""` for no additional cutoff. Existing
import windows, eligibility, and privacy filters still apply; this setting does
not import otherwise ineligible history, purge queued evidence, reset processing
markers, or limit conversation sources.

Git filters candidate IDs before Actomasto reads commit messages and diffs.
It still walks history metadata (`--since-as-filter`) so an old-dated descendant
cannot hide an eligible newer-dated ancestor. Parent objects needed to compute an
eligible commit's diff may predate the cutoff.

Collection skips already pending or terminally processed commit identities before
reading their objects. Remaining raw commit objects are read in bounded batches,
so large non-author histories do not spawn one Git process per commit. All
reachable IDs are still reconsidered: newly reachable older commits are not lost
behind a timestamp checkpoint. Author matching ignores mailmaps, and existing
eligibility and content filters are unchanged. Collection checks control state
immediately and polls logind at most every 100 ms while draining sources; enqueue
and generation boundaries still recheck login directly.

## Privacy and controls

`off`, login, budget, and revocation gates control Actomasto reads and hosted processing, **not independent Funes indexing**. Actomasto never enrolls, refreshes, semantically indexes, or remotely binds memory. `purge` deletes Actomasto drafts/evidence/pending content while preserving processing markers and spending. It does not delete original transcripts, independent corpus/enrollment/indexes, backups, or provider-held requests. Scope changes require explicit configuration apply; affected queued units are revoked. Conversation health starts closed on every restart until the current dependency and harnesses are validated.

Repository discovery traverses the full configured roots, including ignored
dependency directories, nested repositories, and linked worktrees. It does not
stop after a fixed total number of directories. Streaming traversal is bounded
by a 60-second deadline, 128 directory levels, and 10,000 candidate/error records;
exceeding a bound fails the scan without applying partial enrollment. Directory
symlinks below a configured root are not followed. Disabling collection, losing
the active login, or changing configuration cancels an in-progress daemon scan.

## Verification

The current dependency pin targets OMP consumption through Funes source
protocol 1; it does not change Actomasto's Python dependencies or add an OMP
package dependency. Historical measurements below retain their original
revisions and do not verify this pin. Before activation, run
`FUNES_TEST_BIN=/absolute/rebuilt/funes uv run --locked pytest -q` with an
executable built from the pinned revision, and exercise current native OMP
completed and aborted transcripts through `FunesSource.collect`.

`FUNES_TEST_BIN=/absolute/fork/funes uv run pytest -q` exercises the real source subprocess contract. Without that explicit binary, cross-process cases skip rather than substituting a mock parser. Initial integration evidence records 210 passing tests, 32 exact pre-cutover equivalence cases, restart-safe migration, dependency outage/recovery, and an authorized synthetic Anthropic run costing $0.010623 under a $1 cap. No posts were published or live sources enrolled. Implementation assisted by OpenAI Codex.

Actomasto consumes Funes source protocol 1 and its `omp-session3-schema1` capability, not the OMP extension API or an OMP package-version pin. The bridge's separately recorded [OMP 18.1.17 compatibility evidence](https://github.com/audiodude/omp-funes-bridge/blob/e50207d/verification/omp-18.1.17.json) does not replace the Actomasto measurements above, whose recorded environment used OMP 18.1.15. A native transcript schema rejected by Funes still pauses the affected harness; upgrading the bridge alone does not make that schema supported.

Fresh [OMP 18.1.17 consumer verification](verification/omp-18.1.17.json) passed all 210 tests with the real pinned Funes executable. Native RPC-authored completed turns retained exact text, provenance, and identity; aborted turns remained pending. Restart replay and ineligible-interval exclusion passed. This does not certify every historical transcript: the pinned Funes normalizer rejects `session_init` in older OMP child sessions, and live Actomasto also reports unsupported Codex versions. Those parser limitations are unchanged by this compatibility update.

The [September dependency refresh](verification/dependencies-20260911.json) passed all 210 tests against its recorded Funes revision `65b91893d2ca7be80a18ed578c392c8260559b8f`, plus native OMP 18.1.17 completed/aborted-turn consumption, exact text/provenance, restart deduplication, and interval exclusions. `uv lock --upgrade` found no newer compatible Python dependencies. The local plan commit `0a45a9b` is merged without restoring its superseded “implementation not started” status. No private transcripts, hosted generation, live service changes, publication, or deployment were used; parser limitations remain unchanged.

The September 16 lock refresh (`uv lock --upgrade`) updated only `urllib3` from 2.7.0 to 2.8.0 within the existing declared constraints. Actomasto has no OMP package-version constraint to change for OMP 18.2.1: compatibility remains the source protocol/capability contract above. The older evidence files retain their original versions and are not proof of the new dependency or OMP runtime. Verify the refreshed environment and real Funes consumer before activation:

```sh
uv sync --locked
uv run --locked actomasto --help
FUNES_TEST_BIN=/absolute/rebuilt/funes uv run --locked pytest -q
```

The CLI help command is a no-state CLI smoke check, not a source-compatibility check. The real-binary tests use temporary synthetic corpora; `tests/test_funes_source.py` exercises complete-turn text/provenance, restart replay, incomplete writes and exclusion intervals, and `tests/test_funes_runtime.py` exercises collection and dependency outages. Native OMP 18.2.1 completed and aborted transcripts must additionally be exercised through `FunesSource.collect` before claiming that runtime is verified; the static session-v3 fixture alone does not establish native-authoring compatibility.

The [September 16 verification](verification/dependencies-20260916.json) passed all
210 tests against that rebuilt revision. Native OMP 18.2.1 completed and aborted
turns passed exact text/provenance, restart deduplication and exclusion checks:
one complete turn was emitted, while the aborted turn remained pending. No
hosted generation or release publication was exercised. Existing unsupported
historical transcript formats remain outside this verification.

The September 17 refresh updates `idna` from 3.19 to 3.20. The locked environment,
wheel/source-distribution build, CLI smoke, and all 210 tests passed against the
Funes revision `7519c97c6bcdb23c70417d60d1a4df0c97a15241`. Native OMP 18.2.5 complete/abort consumption preserved
text/provenance, left the aborted turn pending, and did not replay after restart
or alter originals. The bridge's
[rollout record](https://github.com/audiodude/omp-funes-bridge/blob/update-local-20260917/verification/dependencies-20260917.json)
contains the measurements and local activation details. The updated worktree
service reached readiness, but at that rollout live adapters remained unhealthy
with stale source status and unsupported-format errors. `config validate` hit
`discovery_limit`; applying the executable-only change with unchanged scope
succeeded. That dependency update did not repair collection. No Hugging
Face artifacts were deployed. Update and verification assisted by OpenAI Codex.

The subsequent collection repair passed 297 Funes tests and all 220 Actomasto
tests against revision `152307b3e9d34e34cc9ecc422960f2cc9971e797`. Full-root discovery found 221 candidates
in 3.1 seconds, and the real `config validate` command succeeded. A metadata-only
live sweep recognized all 511 present transcripts: 7 Claude, 17 Codex, and 487 OMP
sources. One changing OMP source required a current-revision retry; unfinished
turns remained pending. No transcript text was printed or added to the repository.

The repair supports observed Codex ordinal/native user-confirmation records and
OMP child/auxiliary records without relaxing attribution, completion, or unknown
schema gates. Five image-bearing Codex messages with nonmatching generated
wrappers remain excluded by exact-text confirmation. Separately, 155 missing
Claude originals remain inventory tombstones and block that harness: restore the
originals or explicitly decide on an independent inventory rebuild; a parser
upgrade cannot recover deleted files. Enrollment and processing markers were not
reset. Repair and verification assisted by OpenAI Codex.

Local activation replaced the executable pin and systemd runtime after a private
SQLite/configuration backup. Enrollment, enabled/pause controls, spending,
intervals, queued data, and processing markers were preserved during apply.
The daemon resumed Git collection and queued new evidence. At the end of
activation verification it was still scanning the 30,045-commit Oh My Pi history,
before its conversation phase; the daemon's persisted conversation-health
statuses had not yet refreshed. The live metadata sweep above verifies parser
compatibility, not completion of that full daemon pass. Strict Clippy additionally
reported four pre-existing `chunks_exact` warnings in Funes's inference and
normalization code; build and regression tests passed.

The September 21 refresh retains main's collection repair and UTC Git cutoff.
`uv lock --upgrade --refresh` found no newer compatible dependencies. Locked
environment sync, wheel/source-distribution builds, and all 221 tests passed with
Funes revision `0c443bf8d22ec0a0a1c731681b8efefdeeb3e189`. Native OMP 18.2.8 completed-turn text/provenance,
aborted-turn exclusion, restart deduplication, and unchanged originals passed
through the real source consumer. Bridge integration evidence is recorded in
[`dependencies-20260921.json`](https://github.com/audiodude/omp-funes-bridge/blob/update-local-20260921/verification/dependencies-20260921.json).
No hosted-generation smoke or Hugging Face release publication was performed.
Verification assisted by OpenAI Codex.

The stable OMP 18.2.10 refresh retains the latest fork main, the September 21
verification record, and the later explicit Funes pin/build safeguards.
`uv lock --upgrade` resolved 20 packages without compatible version updates;
existing Python constraints remain unchanged. The new Funes pin changes only its
locked `instability` dependency from 0.3.13 to 0.3.14 relative to the fork's
previous main, retaining source protocol 1 and `actomasto-v1` identities.
All 221 tests passed against the rebuilt pinned Funes executable. Native
OMP 18.2.10-authored synthetic sessions passed exact completed-turn text and
provenance, aborted-turn exclusion, restart deduplication, and ineligible-interval
exclusion through the real consumer. No hosted-generation smoke or Hugging Face
publication was performed. Verification assisted by OpenAI Codex.

The September 23 refresh pins Funes
`2e60cf8795b17379a3630c22c7f3da69679a84cd`, including upstream length-grouped
BLAS batching. `uv lock --upgrade --refresh` found no newer compatible Python
dependencies. The wheel/source-distribution build and CLI smoke passed; all 221
tests passed with `FUNES_TEST_BIN` pointing at that rebuilt revision. The bridge
also exercised OMP 18.2.11 indexing and native MCP live-reader retrieval.
No Hugging Face release artifacts were published. AI-assisted with OpenAI Codex.

The September 24 refresh pins Funes
`c6396271a1beb3f9fb1466d1105c0a87c45db07b`, including upstream deterministic
recall tie-breaking and five compatible Cargo dependency updates.
`uv lock --upgrade --refresh` found no newer compatible Python dependencies.
All 221 tests passed against the rebuilt executable; wheel/source-distribution
builds and CLI smoke passed. Native OMP 18.3.0 completed/aborted turns passed
exact text/provenance, stable identity, restart deduplication, terminal interval
exclusion, and unchanged-original checks through the real Funes consumer.
No hosted-generation smoke or Hugging Face publication was performed.
Verification assisted by OpenAI Codex.

### Collector progress and current-source repair

The Git collector now skips durable identities before object reads and batches
unseen raw commits. A real 25,627-commit history scan with no eligible content
completed in 1.62 seconds; the previous implementation was still reading its
644th commit after 15 seconds. A full collector pass on a disposable copy of
local state completed in 37.68 seconds without invoking generation.

The pinned Funes repair supports Claude 2.1.280, Codex image-view and
function-call-output bookkeeping, and OMP upstream-model/credential metadata.
These records do not become evidence or weaken provenance gates. `status --json`
reports current conversation diagnostics under `funes`; the obsolete `sources`
summary of pre-migration adapter checkpoints has been removed. Historical events
and durable processing markers remain intact.

Missing-original tombstones still require restored originals or an explicitly
authorized, backed-up independent inventory rebuild. An inventory rebuild does
not reset Actomasto's history, spending, exclusions, drafts, or deduplication.
Changing originals can still produce transient `source_changed` errors rather
than mixed-revision evidence. Repair and verification assisted by OpenAI Codex.

Verification against the committed parser build passed all 226 Actomasto tests
and 314 Funes library/binary/normalizer tests. Live activation after the authorized
inventory rebuild recovered all three adapters with current coverage: 9 Claude
and 22 Codex streams complete, plus 495 complete and 46 incomplete OMP streams.
Incomplete turns remain pending. Cutover checks preserved controls, enrollment,
collection intervals, spending, drafts, evidence, and existing terminal markers.
No manual hosted-generation smoke or release publication was performed.

### September 25 dependency and runtime refresh

The updated runtime combines current personal briefings with the deployed
collector-progress repair and pins Funes
`c27917ac833c34b9f5b7f39e397efc56f6d59899`. Twelve compatible Cargo dependencies
were refreshed; `uv lock --upgrade --refresh` found no newer Python versions
within the existing constraints. All 379 tests passed against the rebuilt Funes
binary, and wheel/source-distribution builds and CLI smoke passed.

Local activation preserved enrollment, collection intervals, spending, attempts,
suggestions, evidence, terminal markers, and pending identities/expiries.
Configuration apply re-filtered pending payloads under the merged policy.
The collector and source-refresh runtime were restarted; daily and weekly
briefing units now use the same updated environment, without sending an
unscheduled briefing. No Hugging Face release artifacts were published.
Verification assisted by OpenAI Codex.

The final Funes pin also accepts strictly validated assistant `requestControls`
replay metadata without exposing it as evidence. Live activation exposed this
pre-existing parser gap; the previously rejected source returned complete after
repair. All 379 consumer tests and the bridge's native MCP live-reader probe
passed again against the repaired executable.

### September 29 dependency and runtime refresh

The update preserves the deployed collector repairs and personal briefings and
pins Funes `eb7babe7ee251e232c5088dd0b99953c72efe43b`.
`uv lock --upgrade --refresh` found no newer Python dependency versions within
the existing constraints. The locked Python 3.13.12 environment, wheel and
source-distribution builds, and CLI help smoke passed. All 379 tests passed
against the exact pinned executable.

The [September 29 verification](verification/dependencies-20260929.json) also
records a real isolated daemon reaching readiness, answering CLI status while
disabled, and exiting cleanly. Native OMP 18.4.3 completed and aborted transcripts
passed `FunesSource.collect`: complete-turn text, provenance, stable identity,
restart deduplication and terminal interval exclusion were preserved; the
aborted turn remained pending. Neither original transcript changed.
These checks did not mutate live enrollment or services, invoke hosted
generation, or publish Hugging Face artifacts. Update and verification assisted
by OpenAI Codex.

### September 30 dependency refresh

The new pin preserves the collector repairs, Git cutoff and personal briefings
from the deployed `update-all-20260929` branch. Funes merges upstream integration
refresh behavior through `86fb8e1` and advances two compatible compression
dependencies without changing source protocol 1 or native OMP provenance gates.
`uv lock --upgrade --refresh` found no newer compatible Python dependencies.
Build and activate the exact committed fork, preserving the existing corpus,
enrollment, pending work, history, and controls; do not use `funes update` as a
substitute for the pinned build.

The [September 30 verification](verification/dependencies-20260930.json) records
379 passing consumer tests against the exact pin, successful wheel/source builds,
and a real isolated daemon readiness/status/shutdown probe. Native OMP 18.4.4
completed and aborted transcripts preserved exact ordinary text, provenance,
identity, deduplication and exclusion behavior; the aborted turn remained
pending. Funes passed both backend Clippy checks and reported 350 Rust test
passes, including five live-Hub cases that returned early with their gate unset.
No remote publication coverage is claimed. Assisted by OpenAI Codex.

### October 1 dependency refresh

This refresh starts from deployed `update-all-20260930`, preserving the collector
repair, personal briefings, Git cutoff, history, and controls. Current
`origin/main` is already an ancestor of that branch. The compatible Python
dependency refresh (`uv lock --upgrade --refresh`) updates only
`charset-normalizer` from 3.5.1 to 3.5.2; `uv sync --locked` creates an isolated
Python 3.13.12 environment. AI-assisted with OpenAI Codex.

Build the maintained Funes fork at
`c517d868d4d36856f992682045d3610ad0f55762`, rather than using `funes update`.
Source protocol 1 and the `omp-session3-schema1` consumer contract remain the
compatibility boundary; Actomasto has no OMP package-version pin to update.

The [October 1 verification](verification/dependencies-20261001.json) records
379 passing tests with the exact rebuilt Funes executable, successful wheel and
source-distribution builds, CLI help, and a real isolated daemon reaching
readiness, answering status while disabled, and shutting down cleanly. The
daemon used temporary HOME/XDG paths, empty enrollment, and an isolated
no-session `loginctl` shim; this is not a real logind or enabled-generation check.

Native OMP 18.4.6 completed and aborted lifecycle transcripts passed the real
consumer probe: exact complete-turn text, provenance and identity, restart
deduplication, terminal interval exclusion, and unchanged originals were checked.
The aborted turn produced no unit and remained pending. No live enrollment,
service changes, hosted generation, email sends or Hugging Face publication
were performed by this dependency worker; live activation belongs to the
integration owner and must preserve existing state and controls.

### October 1 stable 18.4.9 integration

This update merges `origin/main` at
`2cb9b410d1073eb607acad60fa8d791532dfab66` (including `actomasto suggest`)
into the deployed `update-all-20261001` lineage at
`4fcd7a4645787e6194bc4c08845a3ee6d2f4d503`, preserving the custom collector
repairs and existing source-protocol/provenance contract.
`uv lock --upgrade --refresh` found no newer compatible Python dependency
versions; `uv sync --locked` created a fresh Python 3.13.12 virtual environment.
The maintained Funes revision for this integration is
`eee23d57b6a5895b23898792728c880b3fdd25f8`. Older verification records below
their respective historical headings do not verify this dependency revision.

The [stable 18.4.9 integration verification](verification/dependencies-20261001-stable1849.json)
records 415 passing tests with that exact rebuilt Funes executable, successful
wheel/source-distribution builds, and actual CLI and isolated daemon probes.
The daemon reached readiness, answered status while disabled, and shut down
cleanly using temporary HOME/XDG paths, empty enrollment, no credentials, and
a no-session `loginctl` shim. This does not verify real logind sessions or
enabled hosted generation. `suggest --dry-run --json` selected a synthetic
project with cited commit/planning evidence while hosted processing was disabled
and created no briefing archive.

Native OMP 18.4.9 completed and aborted transcripts passed the real
`FunesSource.collect` consumer probe: exact ordinary text, provenance, identity,
restart deduplication, interval exclusion, and unchanged originals. The aborted
turn emitted no unit and remained pending. No live state, service configuration,
Mastodon posting, email sends, hosted model requests, uploads, remote binding,
or deployment were changed by this worker. Assisted by OpenAI Codex.
