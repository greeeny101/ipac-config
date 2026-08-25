"""The entry point for `python3 ipacconf ...` and `python3 -m ipacconf ...`.

Python runs a directory by executing the `__main__.py` inside it - as a
script, with that directory on sys.path rather than its parent. So the
package this file belongs to is not importable yet, and a relative import
would fail. Putting the parent on the path first fixes that, and leaves the
`-m` form (where __package__ is already set) untouched.
"""

from __future__ import annotations

import os
import sys

if not __package__:
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

from ipacconf.cli import main  # noqa: E402 - must follow the path fix above

sys.exit(main())
