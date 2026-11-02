"""Entry point for `python3 token_counter ...` and `python3 -m token_counter ...`."""

import os
import sys

# When launched as `python3 token_counter`, sys.path[0] is the package directory itself;
# replace it with its parent so the package is importable and its modules don't shadow stdlib.
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
sys.path = [p for p in sys.path if os.path.abspath(p or ".") != _pkg_dir]
sys.path.insert(0, os.path.dirname(_pkg_dir))

try:
    import tiktoken  # noqa: F401
except ImportError:
    sys.exit("tiktoken is not installed. Install the dependencies: pip install -r requirements.txt")

from token_counter.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
