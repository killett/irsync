# Design: `--no-exclude` always reaches rsync

Date: 2026-11-23
Status: approved (brainstorm), not yet implemented

## Problem

`--no-exclude` drops rsync's default excludes (`.Trash-*`, `.cache`,
`unfinished_downloads`) and widens the snapshot diff to match
(`exclude_matcher([])`, AD-32). With a baseline, though, the no-change
short-circuit (AD-2) still runs first. If nothing under the source changed
since the last snapshot, excluded content included, the diff is empty and
irsync logs "No source changes since last backup; backup drive untouched."
and exits 0 without running rsync, under `--dry-run` too.

The short-circuit assumes the backup already holds what this run would copy.
That holds for a run with the default excludes, but not for the first
`--no-exclude` run after runs with them: those never copied the excluded
names, so the backup can lack them however unchanged the source is. The user
asked for the trash, caches and partial downloads and got nothing, with no
hint why. If anything at all had changed, rsync would have run unexcluded and
copied all of it, so the outcome depended on unrelated activity. The
workaround, adding `--force`, is a step the owner would have to remember on
a run that may be months away.

Reproduced on a scratch tree on 2026-11-23 (logged then as an open question).

## Rule 1: `--no-exclude` turns off the no-change short-circuit

When a baseline exists, `--no-exclude` is given and the default exclude list
is non-empty, an empty diff no longer returns early. irsync logs, at INFO:

```
No source changes since last backup, but --no-exclude: running rsync anyway
so the backup gets what the default excludes skip (.Trash-*, .cache,
unfinished_downloads).
```

and carries on to the preview, the confirmation, and rsync (or rsync's dry
run under `--dry-run`). Nothing else about the run changes: the >50% guard
(AD-6) sees the same empty diff it sees under `--force` today, the snapshot
is persisted after a successful rsync as before, and `--force` stays valid
(redundant with `--no-exclude`, harmless).

An empty exclude list (no CLI flag or config key can produce one today) means
`--no-exclude` changes nothing, so the short-circuit stays.

Runs without `--no-exclude`, the owner's cron (`irsync ALL --yes`) included,
behave exactly as before, and AD-2 holds for them. `--no-exclude` runs become
an exception to AD-2: they reach the backup drive even when nothing changed.

## Rule 2: the preview says what it does not show

Under `--no-exclude` (baseline present, exclude list non-empty), the change
preview opens with a note, before the move/deletion/creation lists:

```
=== --no-exclude: rsync also syncs .Trash-*, .cache, unfinished_downloads ===
On the backup, rsync makes these match the source, copying and DELETING
files there. The lists below show only what changed on the source since
the last backup; --dry-run shows every file rsync will copy or delete.
```

Why: with the default excludes, the preview's lists match what rsync changes,
apart from content that exists only on the backup. Under `--no-exclude` after
excluded runs, rsync also copies every excluded file the backup lacks and
deletes excluded content on the backup that is gone from the source (the
owner's F_backup has an `unfinished_downloads` folder). None of that is in
the diff, so on an unchanged drive the preview is all zeros while rsync
writes and deletes. Before Rule 1 this took `--force` too; once one flag
triggers it, the preview has to say so. The note goes through the same path
as the preview (`_emit_preview`): paged interactively, printed under `--yes`.
Its fixed text is ASCII, like the first-run summary's.

The first-run summary (no baseline) needs no note: it already says rsync
deletes anything on the destination that is not on the source, and under
`--no-exclude` it counts nothing as kept.

## Out of scope

- Copies made by a `--no-exclude` run stay frozen on the backup during later
  runs with the default excludes (rsync protects an excluded name on the
  receiver). Still an open question in `PROGRESS.md`, alongside perishable
  excludes.
- Remote sources and `--no-snapshot` go through `_run_rsync_only`, which
  always runs rsync; unaffected.

## Rejected alternative

Record in the snapshot which exclude list the last successful run used, and
short-circuit only when that run already copied everything this one would.
Two `--no-exclude` runs in a row on an unchanged drive would then leave the
backup drive untouched. It changes the snapshot format (older versions
reading a newer snapshot, a field to validate), which is more risk than an
occasional flag justifies.

## Tests (red first)

Scratch tree, baseline from a run with the default excludes, nothing changed
since:

1. `--no-exclude --yes` copies the trash, cache and partial-download files to
   the destination. Fails today: nothing is copied.
2. `--no-exclude --dry-run --yes` prints rsync's lines for those files and
   leaves the destination unchanged. Fails today: rsync never runs.
3. A run without `--no-exclude` still short-circuits and leaves the
   destination untouched (AD-2 regression guard).
4. The preview note appears under `--no-exclude` and not without it.

## Docs

`--no-exclude` help text; architecture doc (AD-2 exception, AD-32, the
`backup.py` row, test count); `PROGRESS.md` (open question migrated here,
current work).
