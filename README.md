# Actomasto

A single-user Linux service that makes evidence-grounded Mastodon **drafts**, never posts. It combines eligible committed Git activity with complete conversation turns from an independently managed local [audiodude/funes](https://github.com/audiodude/funes) corpus. Hosted generation sends eligible filtered evidence to Anthropic; filtering cannot guarantee confidentiality.

## Installation

Requires Python 3.12+, uv, Git, systemd/logind, and the maintained Funes fork implementing [source protocol 1](https://github.com/audiodude/funes/blob/69387f12dca29c2c8e939b0d9890e5768cc2067c/docs/local-source.md). The tested fork revision is `69387f12dca29c2c8e939b0d9890e5768cc2067c`; do not substitute an upstream binary without these capabilities. `notify-send` enables desktop notifications.

Build Funes independently (tested with Rust 1.98.1, protoc, and lld on Linux), then use the absolute path to `funes/target/debug/funes`:

```sh
git clone https://github.com/audiodude/funes.git
git -C funes checkout --detach 69387f12dca29c2c8e939b0d9890e5768cc2067c
CARGO_BUILD_JOBS=2 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_DEV_OPT_LEVEL=1 \
  CARGO_PROFILE_DEV_INCREMENTAL=false RUSTFLAGS="-C link-arg=-fuse-ld=lld" \
  cargo build --locked --manifest-path funes/Cargo.toml
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

## Privacy and controls

`off`, login, budget, and revocation gates control Actomasto reads and hosted processing, **not independent Funes indexing**. Actomasto never enrolls, refreshes, semantically indexes, or remotely binds memory. `purge` deletes Actomasto drafts/evidence/pending content while preserving processing markers and spending. It does not delete original transcripts, independent corpus/enrollment/indexes, backups, or provider-held requests. Scope changes require explicit configuration apply; affected queued units are revoked. Conversation health starts closed on every restart until the current dependency and harnesses are validated.

## Verification

`FUNES_TEST_BIN=/absolute/fork/funes uv run pytest -q` exercises the real source subprocess contract. Without that explicit binary, cross-process cases skip rather than substituting a mock parser. Current evidence records 210 passing tests, 32 exact pre-cutover equivalence cases, restart-safe migration, dependency outage/recovery, and an authorized synthetic Anthropic run costing $0.010623 under a $1 cap. No posts were published or live sources enrolled. Implementation assisted by OpenAI Codex.

Actomasto consumes Funes source protocol 1 and its `omp-session3-schema1` capability, not the OMP extension API or an OMP package-version pin. The bridge's separately recorded [OMP 18.1.17 compatibility evidence](https://github.com/audiodude/omp-funes-bridge/blob/e50207d/verification/omp-18.1.17.json) does not replace the Actomasto measurements above, whose recorded environment used OMP 18.1.15. A native transcript schema rejected by Funes still pauses the affected harness; upgrading the bridge alone does not make that schema supported.

Fresh [OMP 18.1.17 consumer verification](verification/omp-18.1.17.json) passed all 210 tests with the real pinned Funes executable. Native RPC-authored completed turns retained exact text, provenance, and identity; aborted turns remained pending. Restart replay and ineligible-interval exclusion passed. This does not certify every historical transcript: the pinned Funes normalizer rejects `session_init` in older OMP child sessions, and live Actomasto also reports unsupported Codex versions. Those parser limitations are unchanged by this compatibility update.
