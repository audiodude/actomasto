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
- Daily/weekly collection prioritizes conversations overlapping the recap window
  before older context, within the same read bounds. Git metadata budgets prefer
  recent distinct facts over duplicate worktree history.
- An unreadable or unsupported conversation source excludes that original, not
  every conversation from its harness. Coverage lists the source error codes;
  protocol, enrollment and stale-inventory failures still exclude the affected
  harness. Unsupported originals are not reinterpreted, and incomplete coverage
  is not evidence of inactivity. Draft collection retains its stricter behavior.

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

Remote collection uses Python's standard library; no Actomasto package or
`detect-secrets` installation is required on the remote host. Repository/path
exclusions run there before content reads, and full secret filtering runs locally
before evidence is used. Coverage distinguishes SSH failures, remote-command
failures, timeouts, and invalid or oversized responses.

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
Set up Funes enrollment and refresh first; see the
[installation guide](mastodon-activity-app-notes.md#installation-and-consent).

```sh
actomasto init --root /absolute/projects --author-email you@example.com \
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

Before model requests and after responses, draft generation rechecks only the
original conversation sources supporting the queued evidence. An unrelated active,
missing, or unsupported session does not block already-collected drafts. Supporting
originals still fail closed on revision changes, missing files, unsupported schemas,
stale inventory, or enrollment changes; collection itself retains harness-wide checks.
Source authorization is cleared on restart and invalidated by control/configuration
changes, independently of collection adapter health.

Older queued conversations acquire source IDs automatically through exact
session/message metadata matches. Collection times, expiry, retries, and existing
processing markers are preserved. Missing or ambiguous matches remain blocked;
the upgrade requests metadata only, never conversation text.

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

### October 2 stable 18.4.12 dependency refresh

The `update-20261002-stable18412` worktree starts from current main
`6586f59`, preserving the briefing activity-coverage and draft-source-health
fixes rather than reverting to the previous upgrade's older source lineage.
`git pull --ff-only origin main` was already up to date. A compatible
`uv lock --upgrade --refresh` updates only `charset-normalizer` from 3.5.1
to 3.5.2; `uv sync --locked` prepares the worktree's `.venv` on Python 3.13.12.
See the [verification record](verification/dependencies-20261002-stable18412.json)
for full-suite and isolated CLI/Funes consumer evidence.

During service cutover, use this checkout's `.venv/bin/actomasto` for the
collector and both daily/weekly briefing units; the briefing commands retain
their existing `--send` option. The independent `funes-source-refresh.service`
uses `~/.local/lib/funes-source/refresh-actomasto`, whose pinned Funes executable
must stay consistent with the collector and briefing configurations. Preparing
this environment does not rewrite units, restart services, post to Mastodon,
send email, or upload/publish any corpus.

[Detailed operation and architecture](mastodon-activity-app-notes.md#14-running-the-implementation)
· [Funes integration](funes-integration-plan.md)
· [Verification records](verification/)

Development assisted by AI (OpenAI Codex).
