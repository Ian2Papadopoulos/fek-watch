import sys

# Windows consoles and pipes often default to a legacy code page (e.g. cp1252) that cannot
# encode Greek; always write UTF-8, and never crash on an unencodable character.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from .cli import main  # noqa: E402

sys.exit(main())
