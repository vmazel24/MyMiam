import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from curl_cffi import requests as renfo_requests
from curl_cffi.requests.exceptions import RequestException as RenfoRequestException
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from mymiam.dashboard import summary, trends
from mymiam.garmin import GarminConnector
from mymiam.nutrition import finite_number, food_record, normalize, resolve_items, search_foods, totals, valid_day, validate_profile
from mymiam.openai_plan import ChatGPTPlan, PlanError
from mymiam.storage import Store

ROOT = Path(__file__).resolve().parent
SLOTS = {"breakfast", "lunch", "dinner", "snack"}


def create_app(config=None):
    app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
    app.config.update(INSTANCE=os.environ.get("MYMIAM_INSTANCE", str(ROOT / "instance")),
                      PUBLIC_ORIGIN=os.environ.get("MYMIAM_ORIGIN", "http://127.0.0.1:8092"),
                      RENFO_URL=os.environ.get("RENFO_URL", "https://95.216.152.157.sslip.io"),
                      MAX_CONTENT_LENGTH=65536, SECURE_COOKIES=os.environ.get("MYMIAM_SECURE_COOKIES") == "1")
    if config:
        app.config.update(config)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    store = Store(app.config["INSTANCE"])
    plan = ChatGPTPlan(store.directory)
    garmin = GarminConnector(store.directory)
    app.extensions.update(store=store, plan=plan, garmin=garmin)
    attempts, rate_lock = {}, threading.Lock()

    def limited(key, maximum, window=900):
        with rate_lock:
            now = time.monotonic()
            attempts[key] = [v for v in attempts.get(key, []) if now - v < window]
            if len(attempts[key]) >= maximum:
                return True
            attempts[key].append(now)
            if len(attempts) > 10000:
                stale = [k for k, values in attempts.items() if not values or now - values[-1] > window]
                for old in stale:
                    del attempts[old]
            return False

    def renfo(path, token=None, payload=None):
        base = app.config["RENFO_URL"].rstrip("/")
        headers = {"Origin": base, "Accept": "application/json"}
        if token:
            headers["Cookie"] = "renfo_session=" + token
        try:
            # The host's OpenSSL 1.1.1 can omit SNI for DNS names beginning
            # with an IPv4 address, such as Renfo's sslip.io hostname.
            # libcurl sends SNI correctly and still verifies the certificate.
            response = renfo_requests.request("POST" if payload is not None else "GET", base + path,
                                              headers=headers, json=payload, timeout=(5, 12),
                                              allow_redirects=False, verify=True)
            return response, response.json()
        except (RenfoRequestException, ValueError):
            raise PlanError("Renfo est momentanément inaccessible. Réessaie dans un instant.")

    def authenticated():
        token = request.cookies.get("mymiam_session", "")
        if not token or len(token) > 200:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with store.connect() as db:
            session = db.execute("SELECT * FROM sessions WHERE hash=? AND expires>?", (digest, time.time())).fetchone()
        if not session:
            return None
        response, result = renfo("/api/auth/me", session["renfo_token"])
        user = result.get("user")
        if response.status_code != 200 or not isinstance(user, dict) or user.get("id") != store.setting("owner") or not user.get("legacyOwner"):
            return None
        g.renfo_token = session["renfo_token"]
        g.session_hash = digest
        return user

    @app.before_request
    def protect():
        if request.path.startswith("/api/"):
            origin = app.config["PUBLIC_ORIGIN"]
            if request.host != urlsplit(origin).netloc and not app.testing:
                return jsonify(error="Hôte non autorisé"), 400
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if request.headers.get("Origin") != origin:
                    return jsonify(error="Origine non autorisée"), 403
                if not request.is_json:
                    return jsonify(error="Une requête JSON est requise"), 415
            if request.path not in ("/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/health"):
                g.user = authenticated()
                if not g.user:
                    return jsonify(error="Connecte-toi avec ton compte Renfo"), 401

    @app.after_request
    def security(response):
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY", "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def value_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(PlanError)
    def integration_error(error):
        return jsonify(error=str(error)), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error="Requête non autorisée ou invalide"), error.code

    def body():
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise ValueError("Requête invalide")
        return payload

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/sw.js")
    def worker():
        response = send_from_directory(app.static_folder, "sw.js")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/health")
    def health():
        return jsonify(ok=True)

    @app.post("/api/auth/login")
    def login():
        payload = body()
        if limited("login:" + request.remote_addr, 10):
            return jsonify(error="Trop de tentatives. Réessaie dans 15 minutes."), 429
        email, password = str(payload.get("email", "")), str(payload.get("password", ""))
        if not 3 <= len(email) <= 254 or not 1 <= len(password) <= 256:
            raise ValueError("Vérifie ton email et ton mot de passe")
        response, result = renfo("/api/auth/login", payload={"email": email, "password": password})
        user = result.get("user")
        if response.status_code != 200 or not isinstance(user, dict):
            return jsonify(error="Email ou mot de passe Renfo incorrect"), 401
        renfo_token = response.cookies.get("renfo_session")
        if not renfo_token or not re.fullmatch(r"[A-Za-z0-9_-]{20,200}", renfo_token):
            raise PlanError("Renfo n'a pas confirmé la session")
        if not user.get("legacyOwner") or not store.bind_owner(user["id"]):
            renfo("/api/auth/logout", renfo_token, {})
            return jsonify(error="Cette version de MyMiam est réservée au propriétaire Renfo"), 403
        token = secrets.token_urlsafe(32)
        with store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), renfo_token, time.time() + 2592000))
        result = jsonify(user=user)
        result.set_cookie("mymiam_session", token, httponly=True, secure=app.config["SECURE_COOKIES"], samesite="Strict", max_age=2592000)
        return result

    @app.get("/api/auth/me")
    def me():
        return jsonify(user=authenticated())

    @app.post("/api/auth/logout")
    def logout():
        token = request.cookies.get("mymiam_session", "")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with store.connect() as db:
            row = db.execute("SELECT renfo_token FROM sessions WHERE hash=?", (digest,)).fetchone()
            db.execute("DELETE FROM sessions WHERE hash=?", (digest,))
        if row:
            try:
                renfo("/api/auth/logout", row[0], {})
            except PlanError:
                pass
        response = jsonify(ok=True)
        response.delete_cookie("mymiam_session", secure=app.config["SECURE_COOKIES"], samesite="Strict")
        return response

    @app.get("/api/foods")
    def foods():
        return jsonify(foods=search_foods(store, request.args.get("q", "")[:100]))

    @app.get("/api/foods/barcode/<barcode>")
    def barcode_lookup(barcode):
        if not re.fullmatch(r"\d{8,14}", barcode):
            raise ValueError("Code-barres invalide")
        if limited("barcode:" + g.user["id"], 30, 60):
            return jsonify(error="Patiente avant une nouvelle recherche produit"), 429
        response = requests.get("https://world.openfoodfacts.org/api/v2/product/" + barcode,
                                params={"fields": "product_name,brands,nutriments,nutrition_data_per,nutrition_data_prepared_per"},
                                headers={"User-Agent": "MyMiam/0.1 (personal open-source nutrition tracker; https://github.com/vmazel24/MyMiam)"}, timeout=12)
        if response.status_code != 200:
            raise ValueError("Produit non trouvé ou Open Food Facts indisponible")
        product = response.json().get("product")
        if not product:
            raise ValueError("Produit non trouvé")
        raw = product.get("nutriments", {})
        nutrients = {}
        for key, field in (("kcal", "energy-kcal"), ("protein", "proteins"), ("carbs", "carbohydrates"), ("fat", "fat"), ("fiber", "fiber")):
            value = raw.get(field + "_100g")
            nutrients[key] = None if value is None else finite_number(value, 0, 1000 if key == "kcal" else 100, "Nutriments du produit")
        name = str(product.get("product_name") or barcode) + " · " + str(product.get("brands", ""))
        with store.connect() as db:
            db.execute("INSERT OR REPLACE INTO foods VALUES (?,?,?,?,?,?)", (
                "off:" + barcode, name[:300], normalize(name), "Open Food Facts · ODbL", json.dumps(nutrients), json.dumps({k: "Valeur absente" for k,v in nutrients.items() if v is None})))
            row = db.execute("SELECT * FROM foods WHERE id=?", ("off:" + barcode,)).fetchone()
        return jsonify(food=food_record(row))

    @app.get("/api/profile")
    def profile():
        with store.connect() as db:
            row = db.execute("SELECT data FROM profiles WHERE user_id=?", (g.user["id"],)).fetchone()
        return jsonify(profile=json.loads(row[0]) if row else None)

    @app.put("/api/profile")
    def save_profile():
        payload = validate_profile(body())
        with store.connect() as db:
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?)", (g.user["id"], json.dumps(payload)))
            db.execute("INSERT OR REPLACE INTO weights VALUES (?,?,?)", (g.user["id"], date.today().isoformat(), payload["weight"]))
            db.execute("UPDATE days SET goal=? WHERE user_id=? AND day=?",
                       (json.dumps(payload), g.user["id"], date.today().isoformat()))
        return jsonify(profile=payload)

    @app.post("/api/weights")
    def save_weight():
        payload = body()
        day = valid_day(payload.get("day"))
        if day > date.today().isoformat():
            raise ValueError("Un poids ne peut pas être saisi dans le futur")
        weight = finite_number(payload.get("weight"), 30, 350, "Poids")
        with store.connect() as db:
            db.execute("INSERT OR REPLACE INTO weights VALUES (?,?,?)", (g.user["id"], day, weight))
        return jsonify(ok=True)

    @app.get("/api/dashboard")
    def dashboard():
        return jsonify(summary(store, g.user["id"], valid_day(request.args.get("day", date.today().isoformat()))))

    @app.get("/api/trends")
    def history():
        day = valid_day(request.args.get("day", date.today().isoformat()))
        count = int(finite_number(request.args.get("days", 30), 7, 366, "Période"))
        return jsonify(trends(store, g.user["id"], day, count))

    def refresh_day(db, day):
        """Include logged meals automatically and preserve historical goals."""
        user_id = g.user["id"]
        profile = db.execute("SELECT data FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        logged = db.execute("SELECT EXISTS(SELECT 1 FROM meals WHERE user_id=? AND day=?)",
                            (user_id, day)).fetchone()[0]
        goal = profile[0] if profile else None
        db.execute("""INSERT INTO days VALUES (?,?,?,?)
                      ON CONFLICT(user_id,day) DO UPDATE SET complete=excluded.complete,
                      goal=CASE WHEN excluded.day=? THEN excluded.goal ELSE COALESCE(days.goal,excluded.goal) END""",
                   (user_id, day, logged, goal, date.today().isoformat()))

    def meal_fields(payload):
        day = valid_day(payload.get("day"))
        if day > date.today().isoformat():
            raise ValueError("Les repas futurs ne sont pas inclus dans le journal")
        slot = payload.get("slot", "lunch")
        if slot not in SLOTS:
            raise ValueError("Type de repas invalide")
        title = str(payload.get("title", "Repas")).strip()[:120] or "Repas"
        return day, slot, title, str(payload.get("text", ""))[:4000], resolve_items(store, payload.get("items"))

    @app.post("/api/meals/preview")
    def preview():
        items = resolve_items(store, body().get("items"))
        return jsonify(items=items, totals=totals(items))

    @app.post("/api/meals")
    def save_meal():
        payload = body()
        day, slot, title, text, items = meal_fields(payload)
        request_id = str(payload.get("request_id", ""))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{16,100}", request_id):
            raise ValueError("Identifiant de sauvegarde manquant")
        meal_id = str(uuid.uuid4())
        with store.connect() as db:
            db.execute("INSERT OR IGNORE INTO meals VALUES (?,?,?,?,?,?,?,?,?)", (
                meal_id, g.user["id"], day, slot, title, text, json.dumps(items), datetime.now(timezone.utc).isoformat(), request_id))
            row = db.execute("SELECT * FROM meals WHERE user_id=? AND request_id=?", (g.user["id"], request_id)).fetchone()
            if (row["day"], row["slot"], row["title"], row["text"], json.loads(row["items"])) != (day, slot, title, text, items):
                return jsonify(error="Cet identifiant a déjà enregistré un autre repas"), 409
            refresh_day(db, day)
        return jsonify(id=row["id"]), 201

    @app.put("/api/meals/<meal_id>")
    def edit_meal(meal_id):
        day, slot, title, text, items = meal_fields(body())
        with store.connect() as db:
            old = db.execute("SELECT day FROM meals WHERE id=? AND user_id=?", (meal_id, g.user["id"])).fetchone()
            if not old:
                return jsonify(error="Repas introuvable"), 404
            db.execute("UPDATE meals SET day=?,slot=?,title=?,text=?,items=? WHERE id=? AND user_id=?",
                       (day, slot, title, text, json.dumps(items), meal_id, g.user["id"]))
            refresh_day(db, day)
            if old["day"] != day:
                refresh_day(db, old["day"])
        return jsonify(ok=True)

    @app.delete("/api/meals/<meal_id>")
    def delete_meal(meal_id):
        with store.connect() as db:
            row = db.execute("SELECT day FROM meals WHERE id=? AND user_id=?", (meal_id, g.user["id"])).fetchone()
            if not row:
                return jsonify(error="Repas introuvable"), 404
            db.execute("DELETE FROM meals WHERE id=? AND user_id=?", (meal_id, g.user["id"]))
            refresh_day(db, row["day"])
        return jsonify(ok=True)

    @app.get("/api/favorites")
    def favorites():
        with store.connect() as db:
            values = [{"id": row["id"], "title": row["title"], "items": json.loads(row["items"])}
                      for row in db.execute("SELECT * FROM favorites WHERE user_id=? ORDER BY title", (g.user["id"],))]
        return jsonify(favorites=values)

    @app.post("/api/favorites")
    def save_favorite():
        payload = body()
        title = str(payload.get("title", "")).strip()[:120]
        if not title:
            raise ValueError("Donne un nom à ce repas habituel")
        items = resolve_items(store, payload.get("items"))
        with store.connect() as db:
            db.execute("INSERT INTO favorites VALUES (?,?,?,?)", (str(uuid.uuid4()), g.user["id"], title, json.dumps(items)))
        return jsonify(ok=True), 201

    @app.delete("/api/favorites/<favorite_id>")
    def delete_favorite(favorite_id):
        with store.connect() as db:
            db.execute("DELETE FROM favorites WHERE id=? AND user_id=?", (favorite_id, g.user["id"]))
        return jsonify(ok=True)

    @app.get("/api/integrations")
    def integrations():
        return jsonify(chatgpt=plan.status(), garmin=garmin.status(), catalogue=store.setting("ciqual"))

    @app.post("/api/chatgpt/connect")
    def connect_chatgpt():
        if limited("chatgpt-connect", 2, 60):
            return jsonify(error="La connexion est déjà ouverte sur le PC. Patiente une minute."), 429
        subprocess.Popen([sys.executable, str(ROOT / "scripts/connect_chatgpt.py"), "--instance", str(store.directory)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT, start_new_session=True)
        return jsonify(message="Ouvre http://127.0.0.1:9455 sur le PC qui héberge MyMiam pour autoriser ton compte.")

    @app.post("/api/chatgpt/models")
    def check_models():
        return jsonify(models=plan.models())

    @app.post("/api/meals/parse")
    def parse():
        text = str(body().get("text", "")).strip()
        if not 3 <= len(text) <= 4000:
            raise ValueError("Décris ton repas en quelques phrases")
        if limited("parse:" + g.user["id"], 30, 3600):
            return jsonify(error="Limite de 30 interprétations par heure atteinte"), 429
        with store.connect() as db:
            favorite_context = [{"name": r["title"], "items": [{"label": i["name"], "grams": i["grams"]} for i in json.loads(r["items"])]}
                                for r in db.execute("SELECT * FROM favorites WHERE user_id=? LIMIT 20", (g.user["id"],))]
        try:
            draft = plan.parse(text, favorite_context)
            if not isinstance(draft, dict) or not isinstance(draft.get("items"), list) or not 1 <= len(draft["items"]) <= 40:
                raise PlanError("Luna n'a pas fourni une liste d'aliments exploitable")
            for item in draft["items"]:
                if not isinstance(item, dict):
                    raise PlanError("Luna a fourni un aliment invalide. Réessaie ou utilise la saisie manuelle.")
                label = str(item.get("label", ""))[:150]
                matches = search_foods(store, label, 5)
                if not matches:
                    # Keep cooking qualifiers when possible; explicit choices in the review UI.
                    words = [w for w in normalize(label).split() if len(w) > 2]
                    cooking = [w for w in words if w in ("cuit", "cru") and w not in words[:2]]
                    matches = search_foods(store, " ".join(words[:2] + cooking), 5) if words else []
                item["label"] = label
                item["matches"] = matches
                item["food_id"] = matches[0]["id"] if matches else None
                if item.get("grams") is not None:
                    item["grams"] = finite_number(item["grams"], 0.1, 10000, "Quantité proposée")
                item["note"] = str(item.get("note", ""))[:300]
                item["estimated"] = bool(item.get("estimated"))
            draft["questions"] = [str(v)[:400] for v in draft.get("questions", [])][:10]
            return jsonify(draft)
        except requests.RequestException:
            raise PlanError("La connexion OpenAI est indisponible. Ton texte reste disponible.")

    @app.post("/api/garmin/connect")
    def garmin_login():
        payload = body()
        if limited("garmin-connect", 5):
            return jsonify(error="Trop de tentatives Garmin. Patiente 15 minutes."), 429
        email, password = str(payload.get("email", "")), str(payload.get("password", ""))
        if not 3 <= len(email) <= 254 or not 1 <= len(password) <= 256:
            raise ValueError("Identifiants Garmin manquants")
        garmin.start(email, password)
        return jsonify(ok=True)

    @app.post("/api/garmin/mfa")
    def garmin_mfa():
        garmin.mfa(body().get("code"))
        return jsonify(ok=True)

    @app.post("/api/garmin/sync")
    def garmin_sync():
        payload = body()
        end_day = valid_day(payload.get("day", date.today().isoformat()))
        if end_day > date.today().isoformat():
            raise ValueError("Synchronisation future impossible")
        if limited("garmin-sync", 5, 3600):
            return jsonify(error="Patiente avant une nouvelle synchronisation Garmin"), 429
        return jsonify(garmin.sync(store, g.user["id"], end_day))

    @app.get("/api/export")
    def export():
        with store.connect() as db:
            output = {table: [dict(row) for row in db.execute("SELECT * FROM " + table + " WHERE user_id=?", (g.user["id"],))]
                      for table in ("meals", "profiles", "days", "weights", "favorites", "garmin_days")}
        response = jsonify(output)
        response.headers["Content-Disposition"] = 'attachment; filename="mymiam-export.json"'
        return response

    return app


app = create_app()
