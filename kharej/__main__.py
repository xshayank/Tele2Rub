"""Package entrypoint — ``python -m kharej`` delegates to the worker CLI."""

from __future__ import annotations

import sys

from kharej.worker import main

sys.exit(main())
