"""
Test-only launcher: runs the REAL Flask app (same routes, same auth, same
aggregation engine) on a given port, with one difference — the mock SMS
outbox is mirrored to a file so a separate test process (the Node
integration test) can read the "SMS" a farmer would have received.

The API itself never returns a login code over HTTP and this launcher does
not change that; it only observes the same in-memory outbox that
tests/test_auth.py reads directly. Usage:

    KISAN_TEST_OUTBOX=/tmp/outbox.jsonl python3 tests/run_test_server.py 5071
"""
import sys, os, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed
import auth
import app as app_module

OUTBOX_FILE = os.environ["KISAN_TEST_OUTBOX"]
open(OUTBOX_FILE, "w").close()


class FileMirroredOutbox(list):
    def append(self, item):
        super().append(item)
        with open(OUTBOX_FILE, "a") as f:
            f.write(json.dumps(item) + "\n")


auth._dev_outbox = FileMirroredOutbox()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5071
    seed.seed()
    # allow the origin the integration test pretends to be served from
    os.environ.setdefault("KISAN_BAZAR_ALLOWED_ORIGINS", "http://localhost:8080")
    app_module.app.run(port=port, debug=False, use_reloader=False)
