# Design: list a move only when the backup can make it

Date: 2026-11-25
Status: implemented and merged into `main` on 2026-11-26 (plan:
`docs/superpowers/plans/2026-11-25-ask-the-backup.md`); not released

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

## Design

### 1. Under an excluded name, the backup is asked

A baseline row whose path lies under a default-excluded name is a hint, not
a fact. "Under a default-excluded name" means that the row's own name or one
of its parent folders matches `Options.exclude_dirs`, as `exclude_matcher`
decides it, whether or not the run passes `--no-exclude`. Before the diff,
irsync looks at that path on the destination with an `lstat` and replaces
the hint with what is there:

| The destination has, at the row's path | The baseline row is | The diff then reports |
|---|---|---|
| nothing | dropped | created, if the source still has the file |
| a file with the row's size and mtime (whole seconds) | kept as is | a move if the path changed, nothing if it did not |
| a file that differs | kept, with the destination's size and mtime | modified if the path is the same; deleted (old path) and created (new path) if it changed |

The diff (`compute_changes`) then runs as it does today: its "before" side
describes the backup instead of the source's past. Section 2 says which
rows are asked about in each kind of run; in a run with the default
excludes only the table's second line can apply. Consequences:

- Case 1: created, and copied because the backup needs it.
- A current copy on the backup: a move, replayed, nothing transferred.
- Case 4: a deletion and a creation, which is what rsync does.
- Case 2: the >50% guard counts only deletions that exist on the backup,
  against a "before" that holds only rows the backup has, and a first
  transfer is listed as created.
- A first `--no-exclude` run lists every file the backup lacks under those
  names as created, where it used to leave them to the note at the top.

The identity check between baseline and source is untouched: a move still
needs the same inode, size, nanosecond mtime, and birth time (AD-3, AD-13).
The lookup is a further condition, never a replacement, so it cannot turn
two different source files into a move. The comparison with the destination
is to the whole second because that is rsync's test: irsync then never
lists a move that rsync would follow with a transfer, and never refuses one
that rsync would have left alone.

Cost: on a `--no-exclude` run, one lookup per baseline row under an
excluded name, skipping a whole folder the destination lacks.

### 2. Which rows are checked, per kind of run

**A run with the default excludes.** rsync never touches the excluded names
on the destination, so the only thing irsync can do there is take a file
out.

- Checked: baseline file rows under an excluded name whose inode now
  appears, once, in the part of the source rsync syncs, and in no other
  baseline row. Usually none to a few per run.
- Such a row is kept only when the destination holds a matching copy at the
  old path and the identity check passes, so that it is certain to become a
  file move. The replay renames the copy out of the excluded folder:
  nothing is sent and no second copy is left (case 3).
- Otherwise the row is dropped and the file is listed as created, as today.
- Nothing else under an excluded name is listed or counted: the run changes
  nothing there. No path under a name rsync skips may appear in the
  modified, created, or deleted lists.
- AD-2 holds: a lookup happens only when a file has appeared in the synced
  tree, which already means rsync will run. A run with nothing to do never
  touches the backup drive.

Folders are handled file by file in such a run. A folder moved out of an
excluded name is listed as created, with one file move per file the
destination holds. Renaming the whole folder would carry along whatever
else the destination has in it, and rsync would then delete those leftovers
without the preview having listed them. What is left in the destination's
excluded folder stays there, as before (see `PROGRESS.md`, frozen copies).

**A `--no-exclude` run.** rsync syncs every name, so every baseline row
under an excluded name is checked by the rule in section 1, files and
folders alike.

- A folder row is kept when the destination has a folder at its path, and
  dropped, with every row below it, when the destination has nothing there.
  A kept folder that moved is a directory move as usual. The files inside
  are still checked one by one: a matching one rides along with the folder,
  a missing one is created, a stale one is deleted and created.
- Not listed: what the destination holds under those names that the
  baseline has no row for. irsync cannot ask about a path the baseline
  does not record, whether no run ever recorded it or a later run's
  baseline dropped it. rsync deletes it; the preview's note shrinks to say
  exactly that, and `--dry-run` still shows every file. For a local
  destination the note (AD-35) becomes, its first line unchanged:

  ```
  === --no-exclude: rsync also syncs .Trash-*, .cache, unfinished_downloads ===
  The lists below cover these names too, checked against the backup. They
  cannot show one thing: a file there that only the backup has and the last
  run did not record, which rsync DELETES. --dry-run shows every file.
  ```

  Amended 2026-11-26 (owner's approval, after the final review): the note
  first said "no earlier run recorded". The baseline holds only the last
  run's rows, so a file that an earlier `--no-exclude` run recorded and
  copied, and that has since left the source, is deleted unlisted too
  (reproduced with an emptied trash: 6 files, 0 listed as deleted).

- Such a run still always reaches rsync (AD-35), because only rsync finds
  what the baseline does not record. When the diff is empty it still logs
  "No source changes since last backup, but --no-exclude: running rsync
  anyway"; that now means the backup holds every recorded row.

**Boundary.** Only the default-excluded names are checked. A row elsewhere
is what the last successful run synced and stays trusted. If something
other than irsync changes the backup, the lists can still be wrong there;
rsync still makes the backup right, and the replay still skips a missing
file (AD-9).

### 3. A remote destination claims no moves

Until the ssh helper exists (the next design), irsync can neither look at a
remote backup nor rename on it. For a remote destination:

- The diff is computed without move detection. Anything moved or renamed is
  reported as deleted (old path) and created (new path), entry by entry,
  which is what rsync does there: a renamed folder of 12 files is 13
  deletions and 13 creations.
- When the diff with move detection would have held moves, a note opens the
  preview and the log gets a warning:

  ```
  === Remote destination: 1 directory move and 1 file move cannot be replayed ===
  They are listed below as deleted and created: rsync deletes them on the
  backup and sends them again.
  ```

  A count of zero is left out ("3 file moves cannot be replayed"). Under
  `--no-exclude` this note follows the `--no-exclude` note.
- The >50% guard (AD-6) counts them, since they are deletions on the backup.
  A behaviour change: a large rename to a remote destination is refused
  until `--allow-massive-delete` is passed, under `--yes` too. When the
  note above applies, the refusal adds: "On a remote destination a moved or
  renamed entry counts, because rsync deletes it there and sends it again."
- Excluded names are never checked. A run with the default excludes is as
  today. A `--no-exclude` run leaves those names out of the lists on both
  sides and keeps today's note (AD-35), which points to `--dry-run` for
  every file.
- The help text and README say the replay needs a local destination.

What rsync transfers does not change. This only makes the preview and the
guard say what happens; the ssh design turns these entries back into moves.

### 4. The pieces and the flow of a run

| Piece | Where | What it does |
|---|---|---|
| `Destination` interface | new `destination.py` | `look(paths)` answers, per path, nothing there or type, size and mtime; `replay(dir_moves, file_moves)` renames. |
| `LocalDestination` | `destination.py` | `look` is an `lstat` under the resolved backup root. A path that runs through a symlinked folder below the root counts as absent, the rule the replay has (AD-9). `replay` is `apply_moves`, unchanged. |
| Remote destination | none yet | No `Destination` object; section 3 applies. The ssh helper becomes the second implementation. |
| Row checking | new `baseline.py`, no I/O | Picks the baseline rows to ask about and applies section 1's table to the answers. |
| Identity check | `diff.py` | Pulled out of `compute_moves` into one shared function, so "same file" has one definition. |
| `compute_changes` | `diff.py` | Gains a switch to run without move detection (section 3); otherwise untouched. |
| Orchestrator | `backup.py` | Wires these together. |

A run that has a baseline:

1. Snapshot the source and read the baseline, as today.
2. Split the baseline into hints (rows under a default-excluded name) and
   trusted rows (the rest).
3. Pick the hints to ask about: all of them under `--no-exclude`, the
   moved-out file candidates in a run with the excludes, none for a remote
   destination.
4. Call `look` once with all those paths.
5. Build "before" from the trusted rows plus the checked hints, and diff.
6. Guard, preview, prompt, as today.
7. Replay through the destination, run rsync, persist, as today.

A run with no usable baseline is unchanged: it has no rows to check.

Unchanged: the snapshot format and what is written to it, the replay's own
checks (no overwrite, the cross-filesystem refusal, the symlink rule), and
the order of the guards. `look` takes every path in one call so the ssh
implementation needs one round trip. Steps 2, 3 and 5 are functions on rows
and are tested without a destination; the one new piece of I/O is `look`.

### 5. When the lookup cannot answer, and edge cases

- Nothing at the path, or a parent that is not a folder: absent. Not an
  error. A destination root that does not exist yet makes everything
  absent.
- Any other error from `look` (permission denied, an I/O error): the run is
  refused with a message naming the path and exits 2 (`EXIT_REFUSED`).
  Nothing has been written to the destination at that point. An `ALL` run
  counts it as that drive's error and carries on. Refused rather than
  treated as absent: absent would list files as created on the strength of
  an error, and a backup that cannot be read should be known before rsync
  runs.
- Wrong type on the destination (a folder where the row says file, or the
  reverse): the row is kept with the destination's type, so the diff
  reports the path as deleted and created and rsync replaces it. In a run
  with the excludes such a candidate is simply dropped.
- Symlinks and special files are checked by type only.
- A hint whose inode has several paths (hard links) is never a move, as
  today, and never a candidate in a run with the excludes.
- The destination changes between the lookup and the replay: the replay's
  own checks still apply (a missing file is skipped, nothing is
  overwritten), and rsync puts it right.
- `--dry-run` does the lookups, which only read, so its preview matches a
  real run's.
- Old baselines work as they are: the snapshot format does not change.
- A FAT destination (2-second timestamps): rsync already re-sends files
  there on every run unless `--modify-window` is used, which irsync does
  not pass. irsync follows rsync's test, so it finds no moves out of
  excluded names there and makes nothing worse.

### 6. Tests and docs

Every test is written to fail first. The end-to-end ones run on real files
with the real rsync, because the rule rests on what `lstat` and rsync report
(see `PROGRESS.md`, the synthesized-row trap).

The reproduced cases become regression tests:

1. Case 1: first `--no-exclude`, a file moved out, the backup lacks it:
   listed as created, not as a move; the backup ends identical.
2. Case 2: deletions and an edit inside an excluded folder the backup never
   held are not listed and the >50% guard does not refuse. When the backup
   does hold them, they are listed and counted.
3. Case 3: a run with the excludes after a `--no-exclude` run, a file moved
   out: a file move, nothing sent, no second copy left.
4. Case 4: a stale copy on the backup: deleted and created; the backup ends
   identical.
5. A second `--no-exclude` run in a row is still a move with nothing sent
   (passes today; a guard against regressing).

New behaviour:

6. A folder moved out: file by file in a run with the excludes; a directory
   move under `--no-exclude`, with a missing child created and a stale child
   deleted and created.
7. Whatever irsync accepts as a move, rsync then leaves alone: a copy whose
   mtime differs only in nanoseconds is a move with nothing sent; one a
   second apart is not a move.
8. A run with the default excludes and nothing to do performs no lookup
   (AD-2).
9. A run with the excludes never lists a path under an excluded name as
   modified, created, or deleted.
10. A lookup error refuses the run with exit 2 and leaves the backup
    unchanged.
11. A symlinked folder in the backup is not followed.
12. A remote destination, with a stand-in `ssh` on `PATH` as a fixture:
    moves listed as deleted and created, the note with its counts, the
    guard refusing a large rename and saying why, `--allow-massive-delete`
    letting it through, and a `--no-exclude` run leaving the excluded names
    out of the lists.

Without a destination: section 1's table row by row, the candidate picking
of section 2, and the shared identity check. The existing move tests pass
unchanged.

Some existing `--no-exclude` tests change: on an unchanged source whose
backup lacks the excluded names, the diff now lists them as created, so the
"No source changes since last backup, but --no-exclude" line is no longer
logged there, and the note has new wording. The plan names each test.

Docs:

- Architecture doc: new decisions for "under an excluded name the backup is
  asked", "a remote destination claims no moves", and the destination
  interface; updates to AD-2 (when the destination is read before the
  preview), AD-9, AD-32, and AD-35 (the note's wording); the module table.
- README and `--help`: the `--no-exclude` preview, and that the replay needs
  a local destination.
- `PROGRESS.md`: current work, and the open questions this touches (frozen
  copies, replay over ssh).

Version: these are behaviour changes, so the next release is 0.2.0.
Releasing stays a separate step the owner triggers.

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
