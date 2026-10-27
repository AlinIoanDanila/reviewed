"""Entrypoint for `python -m reviewed`."""

from .cli import main

if __name__ == "__main__":
	raise SystemExit(main())
