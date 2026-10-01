import fcntl
import json
import os
import math
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from garminconnect import Garmin


def calories(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and 0 <= result <= 30000 else None
    except (TypeError, ValueError):
        return None


def normalize_stats(raw, day):
    total = calories(raw.get("totalKilocalories"))
    active = calories(raw.get("activeKilocalories"))
    resting = calories(raw.get("bmrKilocalories"))
    return {"day": day, "total": total, "active": active, "resting": resting,
            "partial": day >= date.today().isoformat(),
            "has_data": total is not None and total > 0,
            "synced_at": datetime.now(timezone.utc).isoformat(), "source": "Garmin Connect"}


class GarminConnector:
    def __init__(self, directory):
        self.directory = Path(directory) / "garmin"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.code = None
        self.state = "idle"
        self.error = None

    def status(self):
        return {"connected": any(self.directory.glob("*.json")), "state": self.state,
                "error": self.error, "unofficial": True}

    @contextmanager
    def runtime_lock(self):
        fd = os.open(self.directory / "sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Garmin est déjà occupé par une synchronisation")
            yield

    def start(self, email, password):
        if not self.lock.acquire(blocking=False):
            raise ValueError("Une connexion ou synchronisation Garmin est déjà en cours")
        self.state, self.error, self.code = "connecting", None, None
        self.event.clear()

        def mfa():
            self.state = "mfa"
            if not self.event.wait(300):
                raise ValueError("Code Garmin expiré")
            self.state = "connecting"
            return self.code

        def worker():
            client = None
            try:
                client = Garmin(email=email, password=password, prompt_mfa=mfa,
                                retry_attempts=1, retry_max_wait=2)
                with self.runtime_lock():
                    client.login(str(self.directory))
                    client.client.dump(str(self.directory))
                    for path in self.directory.glob("*.json"):
                        path.chmod(0o600)
                self.state = "idle"
            except Exception:
                self.state, self.error = "error", "Connexion Garmin non confirmée. Vérifie tes identifiants et réessaie."
            finally:
                if client:
                    client.password = None
                self.code = None
                self.lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def mfa(self, code):
        if self.state != "mfa" or not str(code).isdigit() or not 4 <= len(str(code)) <= 10:
            raise ValueError("Code Garmin invalide ou aucune demande en cours")
        self.code = str(code)
        self.event.set()

    def sync(self, store, user_id, end_day, count=7):
        if not self.lock.acquire(blocking=False):
            raise ValueError("Garmin est déjà occupé")
        try:
            with self.runtime_lock():
                client = Garmin(retry_attempts=1, retry_max_wait=2)
                client.login(str(self.directory))
                end = date.fromisoformat(end_day)
                start_day = (end - timedelta(days=count - 1)).isoformat()
                activities = None
                try:
                    activities = client.get_activities_by_date(start_day, end_day)
                except Exception:
                    pass  # Keep daily totals usable if the activity endpoint is unavailable.
                updated = 0
                for offset in range(count):
                    day = (end - timedelta(days=offset)).isoformat()
                    raw = client.get_stats(day)
                    data = normalize_stats(raw, day)
                    data["activities"] = [{"name": str(a.get("activityName") or "Activité")[:200],
                        "calories": calories(a.get("calories")), "duration": calories(a.get("duration")),
                        "type": str((a.get("activityType") or {}).get("typeKey", ""))[:80]}
                        for a in (activities or []) if str(a.get("startTimeLocal", "")).startswith(day)]
                    data["activities_available"] = activities is not None
                    with store.connect() as db:
                        db.execute("INSERT OR REPLACE INTO garmin_days VALUES (?,?,?)", (user_id, day, json.dumps(data)))
                    updated += 1
                client.client.dump(str(self.directory))
                return {"updated": updated}
        except Exception:
            raise ValueError("Synchronisation Garmin indisponible. Les dernières données sont conservées.")
        finally:
            self.lock.release()
