"""Give SDK subprocesses a private AstrBot root before any SDK import.

Module-level side effect on purpose: AstrBot resolves its data directory at
import time, so the root must exist before any test imports the SDK.  When
this module is imported first, it creates a fresh temporary root and pins
``ASTRBOT_ROOT``.  When another test module already pinned a root (for
example tests/test_astrbot_staging_host.py's import-time root), reuse that
root instead of clobbering it: every module in the same interpreter shares
one SDK root, so paths pinned at one import time stay valid at call time.
(Batch-order pollution: the old unconditional overwrite let a later import
repin the root AFTER the SDK captured the earlier one, so generated files
landed outside the roots the SDK then allowed.)

Subprocess entry points are unaffected: each subprocess starts with a clean
environment, so they always take the fresh-root branch.
"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory


_pinned = os.environ.get("ASTRBOT_ROOT")
if _pinned and Path(_pinned).exists():
    ASTRBOT_ROOT = Path(_pinned).resolve()
    _temporary_root = None
else:
    _temporary_root = TemporaryDirectory(prefix="companion-v2-astrbot-root-")
    ASTRBOT_ROOT = Path(_temporary_root.name).resolve()
    os.environ["ASTRBOT_ROOT"] = str(ASTRBOT_ROOT)


def assert_sdk_database_isolated(db_helper) -> None:
    database_path = Path(db_helper.db_path).resolve()
    if not database_path.is_relative_to(ASTRBOT_ROOT):
        raise AssertionError("AstrBot SDK database escaped the temporary data root")
