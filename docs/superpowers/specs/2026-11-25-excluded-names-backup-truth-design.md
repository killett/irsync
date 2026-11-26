# Design: under excluded names, copy only what the backup lacks

Date: 2026-11-25
Status: brainstorm in progress; approach not chosen

## Problem

A snapshot records every row of the source, the default-excluded names
(`.Trash-*`, `.cache`, `unfinished_downloads`) included. The backup holds
those names only where a `--no-exclude` run copied them, and a later run
with the default excludes neither updates nor deletes them there. So under
an excluded name the baseline says what the source had at the last run, and
nothing says what the backup holds. irsync guesses, and guesses wrong in
both directions. All four cases below were reproduced on scratch trees on
2026-11-25; the first was also seen on a real drive.

1. `--no-exclude` after runs with the default excludes: a file moved out of
   `unfinished_downloads` is previewed under "File moves", though the backup
   has no copy. The replay skips the move and rsync copies the file. The
   backup ends up right; the preview was wrong.
2. Same run: files deleted from an excluded folder are listed as "will be
   REMOVED FROM BACKUP" and count toward the >50% guard (AD-6), which
   refused a run that deletes nothing from the backup (8 of 15 rows). A file
   edited there is listed as "re-transferred" on its first transfer.
3. A run with the default excludes after a `--no-exclude` run: a file moved
   out of `unfinished_downloads` is listed as created and copied in full,
   though the backup holds an identical copy at the old path. That copy
   stays, so the backup then holds the file twice.
4. `--no-exclude` when the backup's copy at the old path is stale: the
   preview says "move", the replay renames the stale copy, and rsync
   re-copies the file because its size or mtime differs.

A second `--no-exclude` run directly after the first is right today: the
move is replayed and nothing is transferred.

## Requirement (owner, 2026-11-25)

- Copy a file only when the backup does not already hold it.
- List a move only when the backup holds the file at the old path, and then
  rename it.
- The preview's lists and the >50% guard count only what the run does to the
  backup.

## Rejected

- Diff with the default excludes on both sides whatever the flags say, and
  leave the excluded names to rsync alone. Truthful, but it copies a file
  the backup already holds whenever the old path is under an excluded name
  (reproduced: the second `--no-exclude` run in a row transfers the whole
  file instead of nothing). The owner: avoiding that copy is the point of
  irsync.
- Record in the snapshot header which exclude list the baseline's run used
  (AD-35's rejected alternative). It is right only for `--no-exclude` runs
  in a row: any run with the excludes in between rewrites the baseline and
  the knowledge is gone.

## Options under discussion

1. **Ask the backup.** A baseline row under an excluded name is a guess
   about the backup, checked before the preview with an `lstat` on the
   destination. A move from there is kept when the backup's file at the old
   path has the size and mtime of the source file now (rsync's own test),
   in a run with the default excludes too; otherwise the file is listed as
   created. Deleted and modified rows there are kept only when the backup
   holds the path. No snapshot change; works on existing baselines and
   backups. Misses a file renamed inside the excluded folder after the
   backup got it (one unneeded copy).
2. **Remember what the backup holds.** Rows under an excluded name are
   written only by a run that synced them (`--no-exclude`) and carried
   forward unchanged by runs with the excludes, so the baseline is true for
   every path and the diff is exact with no destination reads. Changes what
   is persisted and needs a header marker to tell old snapshots apart;
   existing baselines are not trusted under those names until the next
   `--no-exclude` run; the record drifts if anything else changes the
   backup.
3. **Both.** Remember, and check a move from an excluded name on the
   destination before listing it.
