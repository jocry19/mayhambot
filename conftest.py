"""Test isolation for every package: on the Mac the tests run inside the live folder, so nothing may touch the real
data/ (the live database, the shared Helius credit ledger, rate-limit locks). Set before any module is imported:
core.config and core.http read these once at import."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="mayham-test-")
os.environ.setdefault("MAYHAM_DB_PATH", os.path.join(_tmp, "mayhambot.db"))
os.environ["MAYHAM_RATELIMIT_DIR"] = os.path.join(_tmp, "ratelimit")
