"""Import before the widget so tests cannot discover real runtime data."""
import atexit
import os
import tempfile

_runtime = tempfile.TemporaryDirectory(prefix='quota-widget-tests-')
os.environ['CODEX_QUOTA_DATA_DIR'] = _runtime.name
os.environ['CODEX_QUOTA_WEEKLY_PER_WINDOW'] = '16'
atexit.register(_runtime.cleanup)
