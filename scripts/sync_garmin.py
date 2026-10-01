"""Scheduled read-only import of seven recent Garmin days for the bound owner."""
import os
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mymiam.garmin import GarminConnector
from mymiam.storage import Store


if __name__ == "__main__":
    store = Store(os.environ.get("MYMIAM_INSTANCE", str(Path(__file__).resolve().parents[1] / "instance")))
    owner = store.setting("owner")
    connector = GarminConnector(store.directory)
    if owner and connector.status()["connected"]:
        try:
            result = connector.sync(store, owner, date.today().isoformat())
            print(f"Garmin : {result['updated']} journées actualisées.")
        except ValueError:
            print("Garmin indisponible ; les données déjà synchronisées sont conservées.", file=sys.stderr)
            sys.exit(1)
    else:
        print("Garmin : connexion initiale en attente.")
