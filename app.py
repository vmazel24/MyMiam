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
from mymiam.captures import favorite_context, prepare_draft
from mymiam.garmin import GarminConnector
from mymiam.nutrition import finite_number, food_record, normalize, resolve_items, search_foods, totals, valid_day, validate_profile
from mymiam.openai_plan import ChatGPTPlan, PlanError
from mymiam.storage import Store
from mymiam.food_research import import_product
from mymiam.references import reference_food

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
    plan = ChatGPTPlan(store.directory, store)
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
            "X-Frame-Options": "DENY", "Permissions-Policy": "camera=(), microphone=(self), geolocation=()",
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
        with store.connect() as db:
            row = db.execute("SELECT * FROM foods WHERE id=?", ("off:" + barcode,)).fetchone()
            reference = db.execute("SELECT 1 FROM food_references WHERE food_id=?", ("off:" + barcode,)).fetchone()
            if row and reference:
                return jsonify(food=reference_food(db, row))
        if limited("barcode:" + g.user["id"], 15, 60):
            return jsonify(error="Patiente avant une nouvelle recherche produit"), 429
        return jsonify(food=import_product(store, barcode))

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
        store.refresh_day(db, g.user["id"], day)

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
            db.execute("DELETE FROM meal_insights WHERE meal_id=?", (meal_id,))
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
            db.execute("DELETE FROM meal_insights WHERE meal_id=?", (meal_id,))
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
        try:
            context = {"local_hour": datetime.now().hour}
            return jsonify(prepare_draft(store, plan.parse(text, favorite_context(store, g.user["id"]), context), context))
        except requests.RequestException:
            raise PlanError("La connexion OpenAI est indisponible. Ton texte reste disponible.")

    @app.post("/api/captures")
    def capture():
        payload = body()
        day = valid_day(payload.get('day', date.today().isoformat()))
        if day > date.today().isoformat():
            raise ValueError('Les repas futurs ne sont pas inclus dans le journal')
        text = str(payload.get('text', '')).strip()
        request_id = str(payload.get('request_id', ''))
        hint = payload.get('slot_hint') or None
        if hint is not None and (not isinstance(hint, str) or hint not in SLOTS):
            raise ValueError('Créneau invalide')
        if len(text) > 4000 or not re.fullmatch(r'[a-zA-Z0-9_-]{16,100}', request_id):
            raise ValueError('Envoi invalide')
        if len(text) < 3:
            raise ValueError('Écris ton repas en quelques mots')
        signature = hashlib.sha256(json.dumps([day, hint, text]).encode()).hexdigest()
        capture_id = str(uuid.uuid4())
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id,signature FROM captures WHERE user_id=? AND request_id=?', (g.user['id'], request_id)).fetchone()
            if old:
                if old['signature'] != signature:
                    return jsonify(error='Cet envoi a déjà enregistré un autre contenu'), 409
                return jsonify(id=old['id']), 202
            if limited('parse:' + g.user['id'], 30, 3600):
                return jsonify(error='Limite de 30 interprétations par heure atteinte'), 429
            if db.execute("SELECT COUNT(*) FROM captures WHERE user_id=? AND status IN ('queued','analysing')", (g.user['id'],)).fetchone()[0] >= 3:
                return jsonify(error='Trois repas sont déjà en cours. Attends quelques instants.'), 429
            if not plan.status()['connected']:
                raise PlanError('Connecte ton forfait ChatGPT avant de lancer Luna')
            db.execute('INSERT INTO captures VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                capture_id, g.user['id'], day, hint, text, 'queued', None, None,
                time.time(), time.time(), request_id, signature))
        return jsonify(id=capture_id), 202

    @app.get("/api/captures")
    def capture_list():
        day = valid_day(request.args.get('day', date.today().isoformat()))
        with store.connect() as db:
            jobs = [dict(row) for row in db.execute(
                '''SELECT c.id,c.day,c.status,c.meal_id,c.error,c.text,c.created,c.updated,r.originals
                   FROM captures c LEFT JOIN capture_reanalyses r ON r.capture_id=c.id
                   WHERE c.user_id=? AND c.day=? ORDER BY c.created''',
                (g.user['id'], day))]
        for job in jobs:
            originals = job.pop('originals')
            job['reanalysis_meal_ids'] = [meal['id'] for meal in json.loads(originals)] if originals else []
        return jsonify(jobs=jobs)

    @app.post("/api/meals/<meal_id>/reanalyse")
    def reanalyse_meal(meal_id):
        body()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            meal = db.execute('SELECT * FROM meals WHERE id=? AND user_id=?', (meal_id, g.user['id'])).fetchone()
            if not meal:
                return jsonify(error='Repas introuvable'), 404
            if len(meal['text'].strip()) < 3:
                return jsonify(error='Ce repas n’a pas de récit à réanalyser. Tu peux modifier ses aliments.'), 400
            origin = db.execute('SELECT group_id FROM meal_origins WHERE meal_id=?', (meal_id,)).fetchone()
            group_id = origin[0] if origin else 'legacy:' + meal_id
            active = db.execute('''SELECT c.id,r.originals FROM captures c JOIN capture_reanalyses r ON r.capture_id=c.id
                WHERE c.user_id=? AND r.group_id=? AND c.status IN ('queued','analysing')''',
                (g.user['id'], group_id)).fetchone()
            if active:
                return jsonify(id=active['id'], reanalysis_meal_ids=[row['id'] for row in json.loads(active['originals'])]), 202
            originals = [dict(row) for row in db.execute('''SELECT m.* FROM meals m JOIN meal_origins o ON o.meal_id=m.id
                WHERE o.group_id=? AND m.user_id=? ORDER BY m.created,m.id''', (group_id, g.user['id']))] if origin else [dict(meal)]
            scope_slot = None
            origin_count = db.execute('SELECT COUNT(*) FROM meal_origins WHERE group_id=?', (group_id,)).fetchone()[0] if origin else 1
            if len(originals) < origin_count or any(row['day'] != meal['day'] or row['text'] != meal['text'] for row in originals) or not meal['request_id'].startswith('capture_'):
                # A manually edited/moved period is reanalysed on its own, so
                # replaying the shared original narrative cannot duplicate siblings
                # or bring back an earlier deleted/replaced period.
                originals, scope_slot = [dict(meal)], meal['slot']
            if limited('parse:' + g.user['id'], 30, 3600):
                return jsonify(error='Limite de 30 interprétations par heure atteinte'), 429
            if db.execute("SELECT COUNT(*) FROM captures WHERE user_id=? AND status IN ('queued','analysing')", (g.user['id'],)).fetchone()[0] >= 3:
                return jsonify(error='Attends la fin des repas en cours'), 429
            if not plan.status()['connected']:
                raise PlanError('Connecte ton forfait ChatGPT avant de lancer Luna')
            capture_id = str(uuid.uuid4())
            signature = hashlib.sha256(json.dumps(originals, sort_keys=True).encode()).hexdigest()
            db.execute('INSERT INTO captures VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                capture_id, g.user['id'], meal['day'], meal['slot'], meal['text'], 'queued', None, None,
                time.time(), time.time(), 'reanalyse_' + capture_id, signature))
            db.execute('INSERT INTO capture_reanalyses VALUES (?,?,?,?)',
                       (capture_id, group_id, json.dumps(originals), scope_slot))
        return jsonify(id=capture_id, reanalysis_meal_ids=[row['id'] for row in originals]), 202

    @app.post("/api/captures/<capture_id>/retry")
    def capture_retry(capture_id):
        body()
        if limited('parse:' + g.user['id'], 30, 3600):
            return jsonify(error='Limite de 30 interprétations par heure atteinte'), 429
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM captures WHERE id=? AND user_id=?', (capture_id, g.user['id'])).fetchone()
            if not row:
                return jsonify(error='Envoi introuvable'), 404
            if row[0] != 'failed':
                return jsonify(error='Cet envoi ne peut pas être relancé'), 409
            if db.execute("SELECT COUNT(*) FROM captures WHERE user_id=? AND status IN ('queued','analysing')", (g.user['id'],)).fetchone()[0] >= 3:
                return jsonify(error='Attends la fin des repas en cours'), 429
            db.execute("UPDATE captures SET status='queued',error=NULL,updated=? WHERE id=?", (time.time(), capture_id))
        return jsonify(ok=True)

    @app.delete("/api/captures/<capture_id>")
    def cancel_capture(capture_id):
        body()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM captures WHERE id=? AND user_id=?', (capture_id, g.user['id'])).fetchone()
            if not row:
                return jsonify(error='Envoi introuvable'), 404
            if row['status'] == 'done':
                return jsonify(error='Le repas est déjà enregistré ; modifie-le depuis le journal'), 409
            db.execute("UPDATE captures SET status='cancelled',updated=? WHERE id=?", (time.time(), capture_id))
        return jsonify(ok=True)

    @app.post("/api/meals/<meal_id>/refine")
    def refine_meal(meal_id):
        payload = body()
        group_index = payload.get('group')
        option_index = payload.get('option')
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (group_index, option_index)):
            raise ValueError('Choix invalide')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            meal = db.execute('SELECT * FROM meals WHERE id=? AND user_id=?', (meal_id, g.user['id'])).fetchone()
            info = db.execute('SELECT data FROM meal_insights WHERE meal_id=?', (meal_id,)).fetchone() if meal else None
            if not meal or not info:
                return jsonify(error='Cette précision n’est plus disponible. Le repas a été modifié.'), 409
            groups = json.loads(info[0])
            if group_index >= len(groups) or option_index >= len(groups[group_index]['options']):
                raise ValueError('Choix invalide')
            group = groups[group_index]
            option = group['options'][option_index]
            items = json.loads(meal['items'])
            index = group['item_index']
            items[index] = resolve_items(store, [{**items[index], **option, 'estimated': True,
                'label': items[index].get('label') or items[index]['name'],
                'note': group['label'] + ' · ' + option['label']}])[0]
            group['selected'] = option_index
            db.execute('UPDATE meals SET items=? WHERE id=?', (json.dumps(items), meal_id))
            db.execute('UPDATE meal_insights SET data=? WHERE meal_id=?', (json.dumps(groups), meal_id))
            refresh_day(db, meal['day'])
        return jsonify(ok=True)

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
