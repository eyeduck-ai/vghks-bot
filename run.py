import sys


def run_biometry_worker():
    """Dispatch the parser's worker subprocess from the Windows one-file build."""
    # The windowed build leaves Python's streams unset. Its OCR subprocess
    # inherits redirected standard descriptors for the parser's JSON protocol.
    try:
        sys.stdin = open(0, encoding="utf-8", closefd=False)
        sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
        sys.stderr = open(2, "w", encoding="utf-8", closefd=False)
        from biometry_parser.worker import main

        return main()
    except (ImportError, OSError):
        return 1


if __name__ == "__main__":
    if getattr(sys, "frozen", False) and sys.argv[1:] == ["-m", "biometry_parser.worker"]:
        raise SystemExit(run_biometry_worker())

    import multiprocessing

    from vghks_bot.biometry import count_worker_main

    multiprocessing.freeze_support()
    page_count = count_worker_main(sys.argv[1:])
    if page_count is not None:
        raise SystemExit(page_count)

    from vghks_bot.app import main

    raise SystemExit(main())
