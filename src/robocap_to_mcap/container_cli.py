from __future__ import annotations

import sys

from . import __version__
from .cli import main as local_main
from .cloud_cli import main as s3_main


USAGE = """RoboCap to MCAP cloud converter

Usage:
  robocap-mcap-cloud local <session-folder> [robocap-mcap options]
  robocap-mcap-cloud s3 --input-uri s3://... --output-uri s3://... [options]
  robocap-mcap-cloud version

Run 'robocap-mcap-cloud local --help' or 'robocap-mcap-cloud s3 --help'
for mode-specific options.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    command, command_args = args[0], args[1:]
    if command == "local":
        return local_main(command_args)
    if command == "s3":
        return s3_main(command_args)
    if command in {"version", "--version"}:
        print(__version__)
        return 0
    print(f"Unknown command: {command}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
