"""Supported finite and continuous worker entry point."""
import sys

if any(argument.split('=')[0] == '--workers' for argument in sys.argv[1:]):
    from worker_pool import main
else:
    from runner import main

if __name__ == "__main__":
    raise SystemExit(main())
