import sys

if sys.version_info < (3, 11):
    print("Codex Session Repair 需要 Python 3.11 或更新版本。", file=sys.stderr)
    raise SystemExit(2)

from .cli import main

raise SystemExit(main())
