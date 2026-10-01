"""Single worker for restart-safe meal capture processing."""
import fcntl
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mymiam.captures import CaptureWorker
from mymiam.openai_plan import ChatGPTPlan
from mymiam.storage import Store

store = Store(os.environ.get('MYMIAM_INSTANCE', str(Path(__file__).resolve().parents[1] / 'instance')))
fd = os.open(store.directory / 'captures-worker.lock', os.O_CREAT | os.O_RDWR, 0o600)
with os.fdopen(fd, 'w') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with store.connect() as db:
        db.execute("UPDATE captures SET status='queued' WHERE status IN ('analysing')")
    worker = CaptureWorker(store, ChatGPTPlan(store.directory))
    last_cleanup = 0
    while True:
        if time.time() - last_cleanup > 300:
            worker.cleanup()
            last_cleanup = time.time()
        if not worker.process_one():
            time.sleep(1)
