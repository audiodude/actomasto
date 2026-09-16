# Actomasto

A single-user Linux service that makes evidence-grounded Mastodon **drafts**, never posts. It combines eligible committed Git activity with complete conversation turns from an independently managed local [audiodude/funes](https://github.com/audiodude/funes) corpus. Hosted generation sends eligible filtered evidence to Anthropic; filtering cannot guarantee confidentiality.

## Installation

Requires Python 3.12+, uv, Git, systemd/logind, and the maintained Funes fork implementing [source protocol 1](https://github.com/audiodude/funes/blob/main/docs/local-source.md). The tested dependency revision is `65b91893d2ca7be80a18ed578c392c8260559b8f`; do not substitute an upstream binary without these capabilities. `notify-send` enables desktop notifications.

Build Funes independently (tested with Rust 1.98.0, protoc, and lld on Linux), then use the absolute path to its `target/debug/funes`. Check out the tested revision above in a separate Funes source worktree before building; a source commit is not a published binary.

```sh
FUNES_SOURCE=/absolute/funes-worktree
git -C "$FUNES_SOURCE" rev-parse HEAD # must match the tested revision above
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
uv run actomasto config validate
uv run actomasto service install
```

The CLI/source/migration and foreground-daemon paths were exercised with synthetic originals and the actual fork; see [verification evidence](verification/funes-integration.json). No live service installation was performed. Replace all example paths. `init` checks metadata capabilities and requests hosted-processing consent, but starts disabled. Configure blocklists and provision `ANTHROPIC_API_KEY` in the launch environment or private `$XDG_CONFIG_HOME/actomasto/credentials.env`, then explicitly run `config apply` and `on`. Do not put secrets in CLI arguments. See [full operation and consent instructions](mastodon-activity-app-notes.md#14-running-the-implementation).

## Existing v1 installations

Stop the service process without issuing `off` (which records an actual exclusion interval), then migrate with the same three explicit Funes flags:

```sh
systemctl --user stop actomasto.service
uv run actomasto config migrate --funes-bin /absolute/funes \
  --funes-corpus /absolute/local-corpus --funes-scope /absolute/enrollment.json
systemctl --user start actomasto.service
```

Every enrollment array must exactly match its expanded canonical legacy root. Migration preserves enabled state, import/off intervals, history, drafts, spending, pending collection times and original expiries. It upgrades the database so old binaries refuse it. Unsupported capabilities or mismatched roots leave conversations closed; the new runtime can continue Git after the explicit migration attempt. If interrupted after the SQLite commit, rerun the same migration to repair the configuration file. Never reset state or substitute a historical cutoff.

## Updating an existing v2 installation

Do not rerun `init` or use `config migrate` to change the Funes executable in an already migrated installation. Migration accepts an identical v2 configuration only for crash recovery. Use `config apply` instead, keeping the existing configuration, database, corpus and enrollment file.

From the new Actomasto checkout, first synchronize its environment. Stop the old service process without running `off` or `service uninstall`; those commands intentionally change collection controls:

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

## Privacy and controls

`off`, login, budget, and revocation gates control Actomasto reads and hosted processing, **not independent Funes indexing**. Actomasto never enrolls, refreshes, semantically indexes, or remotely binds memory. `purge` deletes Actomasto drafts/evidence/pending content while preserving processing markers and spending. It does not delete original transcripts, independent corpus/enrollment/indexes, backups, or provider-held requests. Scope changes require explicit configuration apply; affected queued units are revoked. Conversation health starts closed on every restart until the current dependency and harnesses are validated.

## Verification

`FUNES_TEST_BIN=/absolute/fork/funes uv run pytest -q` exercises the real source subprocess contract. Without that explicit binary, cross-process cases skip rather than substituting a mock parser. Initial integration evidence records 210 passing tests, 32 exact pre-cutover equivalence cases, restart-safe migration, dependency outage/recovery, and an authorized synthetic Anthropic run costing $0.010623 under a $1 cap. No posts were published or live sources enrolled. Implementation assisted by OpenAI Codex.

Actomasto consumes Funes source protocol 1 and its `omp-session3-schema1` capability, not the OMP extension API or an OMP package-version pin. The bridge's separately recorded [OMP 18.1.17 compatibility evidence](https://github.com/audiodude/omp-funes-bridge/blob/e50207d/verification/omp-18.1.17.json) does not replace the Actomasto measurements above, whose recorded environment used OMP 18.1.15. A native transcript schema rejected by Funes still pauses the affected harness; upgrading the bridge alone does not make that schema supported.

Fresh [OMP 18.1.17 consumer verification](verification/omp-18.1.17.json) passed all 210 tests with the real pinned Funes executable. Native RPC-authored completed turns retained exact text, provenance, and identity; aborted turns remained pending. Restart replay and ineligible-interval exclusion passed. This does not certify every historical transcript: the pinned Funes normalizer rejects `session_init` in older OMP child sessions, and live Actomasto also reports unsupported Codex versions. Those parser limitations are unchanged by this compatibility update.

The [September dependency refresh](verification/dependencies-20260911.json) passed all 210 tests against the rebuilt committed Funes revision above, plus native OMP 18.1.17 completed/aborted-turn consumption, exact text/provenance, restart deduplication, and interval exclusions. `uv lock --upgrade` found no newer compatible Python dependencies. The local plan commit `0a45a9b` is merged without restoring its superseded “implementation not started” status. No private transcripts, hosted generation, live service changes, publication, or deployment were used; parser limitations remain unchanged.

The September 16 lock refresh (`uv lock --upgrade`) updated only `urllib3` from 2.7.0 to 2.8.0 within the existing declared constraints. Actomasto has no OMP package-version constraint to change for OMP 18.2.1: compatibility remains the source protocol/capability contract above. The older evidence files retain their original versions and are not proof of the new dependency or OMP runtime. Verify the refreshed environment and real Funes consumer before activation:

```sh
uv sync --locked
uv run --locked actomasto --help
FUNES_TEST_BIN=/absolute/rebuilt/funes uv run --locked pytest -q
```

The CLI help command is a no-state CLI smoke check, not a source-compatibility check. The real-binary tests use temporary synthetic corpora; `tests/test_funes_source.py` exercises complete-turn text/provenance, restart replay, incomplete writes and exclusion intervals, and `tests/test_funes_runtime.py` exercises collection and dependency outages. Native OMP 18.2.1 completed and aborted transcripts must additionally be exercised through `FunesSource.collect` before claiming that runtime is verified; the static session-v3 fixture alone does not establish native-authoring compatibility.
