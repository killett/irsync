# Design: under excluded names, copy only what the backup lacks

Date: 2026-11-25
Status: brainstorm in progress; approach chosen, design sections under review

## Problem

A snapshot records every row of the source, the default-excluded names
(`.Trash-*`, `.cache`, `unfinished_downloads`) included. The backup holds
those names only where a `--no-exclude` run copied them, and a later run
with the default excludes neither updates nor deletes them there. So under
an excluded name the baseline says what the source had at the last run, and
nothing says what the backup holds. irsync guesses, and guesses wrong in
both directions. Cases 1 to 4 were reproduced on scratch trees on
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

A separate case breaks the same promise, a listed move that is not a rename
on the backup:

5. A remote destination (`host:/path`) never gets the replay. `apply_moves`
   runs only for a local destination (`os.rename`, AD-9), and nothing logs
   the skip; it has been so since 0.1.0. Reproduced 2026-11-25 with a
   stand-in ssh that runs the remote side locally: for a renamed folder of
   12 files plus one moved file, the preview lists one directory move and
   one file move, then rsync's `--delete-before` deletes 14 entries on the
   backup and sends 6.8 MB again, where the same change to a local
   destination deletes nothing and sends 0 bytes. The >50% guard does not
   see it (the diff reports 0 deleted; rsync removes 14 of the backup's 18
   entries). A transfer cut off part-way (irsync's rsync command run
   directly and slowed, not Ctrl-C on irsync) left the backup with neither
   copy of what was not yet sent: the old folder gone, 2 of 12 files under
   the new name. The help text and README promise the replay without this
   exception, and no test runs a diff against a remote destination.

## Requirement (owner, 2026-11-25)

- Copy a file only when the backup does not already hold it.
- List a move only when the backup holds the file at the old path, and then
  rename it.
- The preview's lists and the >50% guard count only what the run does to the
  backup.

## Decisions (owner, 2026-11-25)

- **Ask the backup.** A baseline row under an excluded name is a guess
  about the backup, checked on the destination before the preview with an
  `lstat`, never by reading file contents. "The backup holds the file"
  means rsync's own test: same size, and same mtime to the whole second
  (checked on rsync 3.4.1: a copy whose mtime differs only in nanoseconds
  is left alone, one a second apart is sent again).
- **One interface for the destination.** Looking at the backup and renaming
  on it sit behind one interface. The local destination implements it now;
  a helper run over ssh can implement it later without changing the callers.
- **A remote destination is made honest in this design, not fast.** Until
  the ssh helper exists, a move to a remote destination is listed as what
  rsync does with it (deleted and sent again), counts toward the >50%
  guard, and the run says the replay is unavailable. The help text and
  README stop promising otherwise.
- **Replay over ssh is the next design**, after this one.

## Options considered

1. **Ask the backup.** Chosen. No snapshot change; works on existing
   baselines and backups. Misses a file renamed inside the excluded folder
   after the backup got it (one unneeded copy).
2. **Remember what the backup holds.** Rows under an excluded name written
   only by a run that synced them and carried forward by runs with the
   excludes. Exact with no destination reads, but it changes what is
   persisted, needs a header marker to tell old snapshots apart, trusts
   nothing on existing baselines until the next `--no-exclude` run, and
   drifts if anything else changes the backup.
3. **Both.** Not needed while option 1 covers the reproduced cases.

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
