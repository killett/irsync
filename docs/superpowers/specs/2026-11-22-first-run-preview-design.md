# Design: a first-backup summary that compares like with like

Date: 2026-11-22
Status: approved (brainstorm), not yet implemented

## Problem

When a backup has no usable snapshot, irsync shows a first-run summary before
the confirmation prompt (mount-safety spec, Rule 3):

```
=== FIRST BACKUP — no prior snapshot ===
Source:      /media/u/F          70,995 entries, 20.0 TiB
Destination: /media/u/F_backup   12 existing entries
rsync will transfer the source in full and DELETE anything at the
destination that is not on the source.
```

The two counts measure different things. The source figure is every row of
the fresh snapshot, at every depth, including the root itself. The
destination figure is the number of names directly under the destination
root, from the same `iterdir` the destination gate performs. Side by side
they read as "the backup holds 12 of 70,995 entries", which makes an
up-to-date backup of the right drive look nearly empty. A user stopped at
exactly this prompt, frightened, and had to verify by hand (top-level `ls`
of both drives, `df`, and a full `--dry-run`) that the backup was in fact
identical.

The closing sentence adds to it: "transfer the source in full" is wrong
whenever the destination already holds a backup, because rsync copies only
what differs.

The refusal that precedes `--allow-nonempty-dest` has the same flaw ("already
contains 12 entries ... rsync would DELETE them"): it counts top-level names
without saying so, and claims every one of them would be deleted.

A related defect sits on the same output path and was reproduced while
designing this: a file name that is not valid UTF-8 (os.fsdecode turns each
bad byte into a lone surrogate) crashes the change preview. Under `--yes` with
a strict UTF-8 stdout, a run in which such a file was moved logs
`'utf-8' codec can't encode character '\udcff' ... surrogates not allowed`
and exits 2 before rsync, so that drive can never be backed up until the
name changes. Interactively, the same text crashes when piped to `less`,
which `subprocess.Popen(..., text=True)` always encodes strictly.

## Decision

Make the summary compare like with like, add a cheap name check that tells
whether the destination looks like this source's backup, and make every name
that reaches a preview safe to print. No additional scan of either tree: the
mount-safety spec's "no additional tree scan" rule stands.

Alternatives considered:

- **Wording only** (label the destination figure "top-level items"). Smallest
  change, but it would not have told the user whether `F_backup` was the right
  backup.
- **Also run an rsync dry run before the prompt** and print its delete/copy
  counts. The definitive answer, but every first backup, including `--yes`
  runs, would pay a full comparison of both trees first, and parsing
  `--stats` output ties irsync to rsync's wording. The summary points to
  `--dry-run` instead.

## Rule 1: the first-run summary

For a local destination:

```
=== FIRST BACKUP (no usable snapshot) ===
Source:      /media/u/F
             11 top-level items
             70,994 files and folders in all, 20.0 TiB
Destination: /media/u/F_backup
             12 top-level items:
               11 also on the source
                1 skipped by rsync, so kept: .Trash-1000
                0 not on the source (a real run DELETES these)
A real run makes the destination match the source: it copies
whatever differs and DELETES anything there that is not on the source.
For the exact list of files, answer N and rerun with --dry-run.
```

- **Source top-level count** comes from one listing of the source root, with
  irsync's reserved names left out, the same rule the destination listing
  uses (AD-5). A real listing rather than the snapshot rows, because the
  snapshot skips nested mount points (AD-27) while the top-level directory
  of one is still there.
- **"files and folders in all"** is the snapshot's row count minus the root
  row (`path == "."`), so it counts what is on the drive, not the drive
  itself. The size is unchanged: the sum of regular-file sizes.
- **Destination split.** Each top-level destination name falls in exactly one
  group, tested in this order:
  1. *skipped by rsync, so kept* — it matches rsync's excludes (the same
     `exclude_matcher` predicate the diff uses, AD-32, built from an empty
     list under `--no-exclude`). rsync never deletes an excluded name on the
     receiving side, since irsync does not pass `--delete-excluded`.
  2. *also on the source* — the source's top level has the same name. The
     comparison is exact, as rsync's is: `Films` vs `films`, or the same
     text in two Unicode normalization forms, are different names.
  3. *not on the source* — a real run deletes it.

  All three lines are always shown. The second and third name their items,
  sorted, up to ten, then `(and N more)`. Counts are right-aligned in a
  column and use thousands separators.
- **Other destination states.** A destination that does not exist yet reads
  `does not exist yet (rsync will create it)`. One that exists but holds
  nothing besides irsync's own files reads `empty`. A remote destination
  still reads `not checked (remote)` and has no split; it is never listed
  (AD-25).
- **Closing sentence.** "A real run makes the destination match the source"
  is true for a real run and for a `--dry-run` preview alike.
- **The `--dry-run` hint** appears only when there is a prompt to answer:
  not under `--yes`, and not under `--dry-run`, which shows rsync's own
  output right after.
- **Banner** `=== FIRST BACKUP (no usable snapshot) ===` covers both a
  missing snapshot and one rejected for belonging to another source; the
  existing log line still says which.
- **Singular and plural.** `1 top-level item`, `2 top-level items`;
  `1 file or folder in all`, `2 files and folders in all`.
- **ASCII.** Every fixed string in the summary is ASCII. This removes the em
  dash noted in PROGRESS.md's open question about non-ASCII strings, the only
  instance of that problem that could raise rather than be escaped. Names
  keep their real characters apart from Rule 3's escapes.

## Rule 2: the refusal before `--allow-nonempty-dest`

The destination gate's condition is unchanged: no usable baseline and any
non-reserved top-level name at a local destination, excluded names included,
refuses unless `--allow-nonempty-dest`. Only the message changes:

```
Refusing: no usable snapshot, but the destination /media/u/F_backup already
holds 12 top-level items (11 also on the source, 1 skipped by rsync so kept,
0 not on the source). A first backup makes the destination match the source
and DELETES anything there that is not on the source. Check that the source
drive is mounted and that this is its backup, then pass --allow-nonempty-dest
to adopt it.
```

When the third group is not empty, its names follow, up to ten, then
`(and N more)`, with the same wording as the summary.

## Rule 3: names are made safe before they reach a preview

One helper renders a file name or path for display, and every name or path
placed in preview text goes through it: the first-run summary, the refusal,
and the existing change preview (`_format_preview`: moves, modifications,
creations, deletions). It escapes, unambiguously:

- a byte that was not valid UTF-8 (a lone surrogate U+DC80–U+DCFF from
  `surrogateescape`) as `\xNN`, the original byte;
- a control character below U+0080 (C0 and DEL) as `\n`, `\r`, `\t`, or
  `\xNN`. Such bytes are always valid UTF-8, so `\xNN` below `\x80` can
  only mean the character, never a raw byte;
- any other control, format, separator, or surrogate code point (Unicode
  categories Cc, Cf, Cs, Zl, Zp; this covers C1 controls, bidi overrides,
  and U+2028/U+2029) as `\uXXXX`;
- a backslash as `\\`, so an escape in the output can only have come from
  this helper.

Every other character is printed as is. A name can then neither crash the
preview nor fake a line in it.

## Not changing

- The destination gate's condition (Rule 2).
- Refusing a destination whose names mostly are not on the source even with
  `--allow-nonempty-dest`. That would be a new guard; it is recorded as an
  open question in PROGRESS.md.
- The source size, which still counts excluded content.

## Testing

Red/green TDD, a test per rule and per edge case: the like-with-like counts,
each split group (excluded, `--no-exclude`, on-source, not-on-source),
the ten-name cap, a missing, an empty, and a remote destination, singular
and plural, the hint's three modes, ASCII-only fixed text, the refusal
wording, and the display helper's escapes. The non-UTF-8 tests create real
files with bad bytes (PROGRESS.md's synthesized-row trap) and run under a
strict UTF-8 stdout; one end-to-end test runs irsync on real directories and
checks the printed summary.
