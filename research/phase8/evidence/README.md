# Phase 8 evidence directory

This directory holds **no campaign evidence in git**. The `.gitignore` next to
this file excludes everything except this README, `.gitignore` and `.gitkeep`.
Live-market captures, database exports and logs are too large for the
repository, and they belong to a single campaign run. Archive them outside git
(see Retention). Commit a campaign's evidence only if the Phase 8.1 report
explicitly requires it.

## Layout (one directory per campaign)

```
evidence/
  <campaign_id>/                  e.g. 8.1-v1_2026-10-05 (protocol version + first session date)
    preflight/  preflight_<ts>_<run>.jsonl      tools.preflight     (before every session)
    logs/       server_<ts>_<run>.log           tools.launch        (INFO, timestamped, redacted)
    status/     status_<ts>_<run>.jsonl         tools.collector     (60 s samples)
    bars/       bars_<ts>_<run>.jsonl           tools.bars          (60 s captures, delta-encoded)
    drills/     drill_<mode>_<ts>_<run>.jsonl   tools.drill         (drill C/D only)
    db/         extract_<ts>_<run>/             tools.extract       (per session, after close)
                  manifest.json baseline.json <table>.jsonl
    reconcile/  reconcile_<ts>_<run>.jsonl      tools.reconcile
    operator/   operator_log.md                 hand-written drill/reset log (times in IST)
    SHA256SUMS                                  `sha256sum` of every file, appended at each session end
```

`<ts>` is the IST start time of the tool run (`YYYYMMDDTHHMMSS+0530`). `<run>` is
the tool's 12-hex-digit run id, which every record repeats.

## Rules

- **Append-only, never edited.** Every tool creates its file with
  exclusive-create mode and never reopens it. Each JSONL record carries a
  `record_sha256` of its own content. A correction is a new file that names the
  file it supersedes. The original stays.
- **Nothing is filled in.** A missing value, failed request, NaN price or absent
  bar is recorded as missing, failed, `"NaN"` or absent.
- **No secrets.** The tools hold no credentials. Error text and log lines go
  through `common.redact`, and database manifests carry the server and database
  names only. Never copy `.env`, settings files, tokens or connection strings
  in here.
- **Times.** Collector and capture records carry UTC and IST. Database exports
  keep the database's naive timestamps (protocol assumption A3: IST).

## Retention

Keep raw evidence unmodified until the Phase 8.1 report is accepted. After that,
archive the whole `<campaign_id>/` directory with its `SHA256SUMS`. Keep it for
at least as long as any decision based on the report stands. Delete the working
copy only after verifying the archive against `SHA256SUMS`.
