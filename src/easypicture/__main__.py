"""Module entry point for ``python -m easypicture``."""

from __future__ import annotations

import sys
import traceback

from easypicture.app import run


def main() -> None:
    """Run EasyPicture and translate unhandled failures to a non-zero exit."""
    try:
        exit_code = run(sys.argv[1:])
    except SystemExit:
        raise
    except BaseException:  # pragma: no cover - last-resort process boundary
        traceback.print_exc()
        exit_code = 1
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
