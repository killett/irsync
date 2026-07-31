"""Entry point for `python -m irsync`."""

from irsync.cli import main

if __name__ == "__main__":
    # Propagate main()'s return value as the process exit status. Without
    # this, `python -m irsync` exited 0 for every failure — refused run (2),
    # lock held (75), user abort (130), rsync's own non-zero code — so a cron
    # wrapper saw success no matter what happened. The `irsync` console
    # script always had this behaviour (its generated wrapper calls
    # sys.exit(main())); this makes the two entry points agree.
    raise SystemExit(main())
