"""Run the recorded fake-device browser scenarios used by ``tests/e2e``.

Install the UI and E2E groups first. This command never accepts a DSP address:
every scenario injects ``e2e.fake.invalid`` instead of creating a real client.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("ignore/playwright"),
                        help="parent directory for a unique recorded run")
    parser.add_argument("--review-videos", action="store_true",
                        help="pause and scroll at key completed states for review")
    parser.add_argument("pytest_args", nargs="*", help="additional pytest selectors or options")
    args = parser.parse_args()
    command = [sys.executable, "-m", "pytest", "tests/e2e", "--run-e2e", "--e2e-artifacts", str(args.artifacts)]
    if args.review_videos:
        command.append("--review-videos")
    command.extend(args.pytest_args)
    raise SystemExit(subprocess.call(command))


if __name__ == "__main__":
    main()
