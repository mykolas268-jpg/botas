import os
import sys
import tempfile
from pathlib import Path

# Must run before config/bot are imported: keep tests away from the real digest.db
# and from any proxy settings when talking to the local test server.
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests share fake-Discord helpers
