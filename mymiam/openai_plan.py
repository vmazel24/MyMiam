"""Official ChatGPT plan OAuth; no API key and no billed fallback."""
import base64
import fcntl
import hashlib
import json
import os
import secrets
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import jwt
import requests

from .skills import meal_instructions
from .nutrition_tools import NutritionTools, TOOLS
from .storage import Store
from .food_research import research

ISSUER = "https://auth.openai.com"
TOKEN_ENDPOINT = ISSUER + "/api/accounts/oauth/token"
API = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class PlanError(Exception):
    pass


def protected_write(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


class ChatGPTPlan:
    def __init__(self, directory, store=None):
        self.store = store
        self.directory = Path(directory)
        self.path = self.directory / "chatgpt.json"

    def saved(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / "chatgpt.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def host_id(self):
        path = self.directory / "host.json"
        if not path.exists():
            protected_write(path, {"id": "urn:uuid:" + str(uuid.uuid4())})
        return json.loads(path.read_text())["id"]

    def authorization(self, redirect_uri):
        saved = self.saved()
        attempt = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                   "verifier": secrets.token_urlsafe(64), "redirect_uri": redirect_uri,
                   "client_id": saved.get("client_id"), "subject": saved.get("subject"),
                   "created": time.time()}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt["verifier"].encode()).digest()).decode().rstrip("=")
        params = {"client_id": attempt["client_id"] or "dynamic_agent_client", "response_type": "code",
                  "redirect_uri": redirect_uri, "scope": SCOPES, "resource": API,
                  "state": attempt["state"], "nonce": attempt["nonce"],
                  "ext_agent_host_id": self.host_id(), "code_challenge": challenge,
                  "code_challenge_method": "S256"}
        if not attempt["client_id"]:
            params["agent_name_hint"] = "MyMiam"
        # No ID token in URLs/logs; the account selector is fine for reauthorization.
        return attempt, ISSUER + "/api/accounts/authorize?" + urlencode(params)

    def exchange(self, attempt, query):
        if time.time() - attempt["created"] > 600 or not secrets.compare_digest(
                query.get("state", ""), attempt["state"]):
            raise PlanError("Connexion expirée ou non vérifiée. Relance la connexion.")
        if query.get("error") or not query.get("code"):
            raise PlanError("Autorisation ChatGPT refusée ou incomplète.")
        client_id = query.get("client_id") or attempt["client_id"]
        if not client_id or client_id == "dynamic_agent_client" or (
                attempt["client_id"] and client_id != attempt["client_id"]):
            raise PlanError("L'enregistrement MyMiam n'a pas été confirmé.")
        response = requests.post(TOKEN_ENDPOINT, data={"grant_type": "authorization_code",
            "client_id": client_id, "code": query["code"], "code_verifier": attempt["verifier"],
            "redirect_uri": attempt["redirect_uri"], "resource": API}, timeout=25, allow_redirects=False)
        if response.status_code != 200:
            raise PlanError("OpenAI n'a pas accepté la connexion. Relance l'autorisation.")
        tokens = response.json()
        identity = self.verify_identity(tokens.get("id_token", ""), client_id, attempt["nonce"])
        if attempt["subject"] and identity["sub"] != attempt["subject"]:
            raise PlanError("Le compte autorisé ne correspond pas au compte déjà associé.")
        scopes = tokens.get("scope", "").split()
        if "chatgpt.tokens.use.direct" not in scopes or not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise PlanError("L'autorisation d'utiliser ton forfait ChatGPT n'a pas été accordée.")
        saved = {"client_id": client_id, "subject": identity["sub"], "email": identity.get("email", ""),
                 "access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
                 "id_token": tokens["id_token"], "scopes": scopes,
                 "expires_at": time.time() + tokens.get("expires_in", 3600)}
        with self.locked():
            protected_write(self.path, saved)
        return identity

    @staticmethod
    def verify_identity(token, client_id, nonce):
        keys = jwt.PyJWKClient(ISSUER + "/.well-known/jwks.json", timeout=15)
        key = keys.get_signing_key_from_jwt(token)
        identity = jwt.decode(token, key.key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                              leeway=5, options={"require": ["sub", "exp", "iat", "nonce"]})
        if not secrets.compare_digest(str(identity["nonce"]), nonce):
            raise PlanError("Identité ChatGPT non vérifiée.")
        return identity

    def access_token(self):
        # Caller holds process-wide file lock, preventing refresh-token races.
        saved = self.saved()
        if not saved.get("access_token"):
            raise PlanError("Connecte ton forfait ChatGPT depuis le PC pour utiliser Luna.")
        if saved.get("expires_at", 0) <= time.time() + 90:
            response = requests.post(TOKEN_ENDPOINT, data={"grant_type": "refresh_token",
                "client_id": saved["client_id"], "refresh_token": saved["refresh_token"], "resource": API},
                timeout=25, allow_redirects=False)
            if response.status_code != 200:
                raise PlanError("La connexion ChatGPT doit être renouvelée depuis le PC.")
            tokens = response.json()
            if not tokens.get("access_token"):
                raise PlanError("Renouvellement ChatGPT incomplet.")
            saved.update(access_token=tokens["access_token"],
                         refresh_token=tokens.get("refresh_token", saved["refresh_token"]),
                         expires_at=time.time() + tokens.get("expires_in", 3600))
            protected_write(self.path, saved)
        return saved["access_token"]

    def models(self):
        with self.locked():
            token = self.access_token()
            response = requests.get(API + "/models", headers={"Authorization": "Bearer " + token}, timeout=20)
            if response.status_code != 200:
                raise PlanError("Impossible de vérifier les modèles de ton forfait.")
            body = response.json()
            models = [{"slug": v["slug"], "name": v.get("display_name", v["slug"])}
                      for v in body.get("models", []) if v.get("visibility") == "list" and "luna" in v.get("slug", "").lower()]
            if not models:
                raise PlanError("Luna n'est pas proposé à ce compte sur cette connexion.")
            preferred = next((v["slug"] for v in models if v["slug"] == "gpt-6-luna"), models[0]["slug"])
            protected_write(self.directory / "model.json", {"slug": preferred, "models": models})
            return models

    def status(self):
        saved = self.saved()
        model = {}
        path = self.directory / "model.json"
        if path.exists():
            model = json.loads(path.read_text())
        return {"connected": bool(saved.get("access_token")), "email": saved.get("email"),
                "model": model.get("slug"), "billing": "included_plan", "paid_fallback": False}

    def parse(self, text, favorites, context=None):
        path = self.directory / "model.json"
        if not path.exists():
            self.models()
        model = json.loads(path.read_text())["slug"]
        if "luna" not in model.lower():
            raise PlanError("Seul Luna est activé pour cette version.")
        option = {"type": "object", "additionalProperties": False,
                  "required": ["label", "grams", "food_label", "food_id"], "properties": {
                      "food_id": {"type": ["string", "null"]},
                      "label": {"type": "string"}, "grams": {"type": "number"}, "food_label": {"type": "string"}}}
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["title", "slot", "items", "clarifications"], "properties": {
                    "title": {"type": "string"}, "slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                    "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                        "required": ["label", "slot", "food_id", "grams", "estimated", "note"], "properties": {
                            "slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                            "food_id": {"type": ["string", "null"]},
                            "label": {"type": "string"}, "grams": {"type": "number"},
                            "estimated": {"type": "boolean"}, "note": {"type": "string"}}}},
                    "clarifications": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                        "required": ["item_index", "label", "selected", "options"], "properties": {
                            "item_index": {"type": "integer"}, "label": {"type": "string"}, "selected": {"type": "integer"},
                            "options": {"type": "array", "items": option}}}}}}
        instructions = (meal_instructions() + "\nContexte du repas : " +
            json.dumps({k: v for k, v in (context or {}).items() if k != 'capture_id'}, ensure_ascii=False) +
            "\nRecettes habituelles de cet utilisateur : " + json.dumps(favorites, ensure_ascii=False))
        payload = {"model": model, "instructions": instructions,
                   "input": [{"role": "user", "content": text}], "reasoning": {"effort": "low"},
                   "text": {"format": {"type": "json_schema", "name": "meal", "strict": True, "schema": schema}},
                   "store": False, "stream": True}
        usage = {}
        research_responses = 0
        token = None
        def research_public(query):
            nonlocal research_responses
            research_responses += 1
            try:
                return research(self.response, token, model, query, usage)
            except (PlanError, ValueError, requests.RequestException):
                return {'found': False, 'exact_match': False,
                        'note': 'La source externe n’a pas pu être vérifiée ; conserver une approximation visible.'}
        nutrition = NutritionTools(self.store or Store(self.directory), researcher=research_public, narrative=text)
        payload['tools'] = TOOLS
        payload['tool_choice'] = 'required'
        payload['include'] = ['reasoning.encrypted_content']
        calls = 0
        repair_requested = False
        repair_rounds = tool_errors = last_repair_errors = 0
        audit_id = str((context or {}).get('capture_id') or uuid.uuid4())
        # Only generated capture UUIDs may select a filename.
        audit_id = str(uuid.UUID(audit_id))
        trace_path = self.directory / 'analysis_traces' / (audit_id + '.json')
        def trace(status, **details):
            protected_write(trace_path, {'model': model, 'at': time.time(), 'status': status,
                'tools': nutrition.audit, 'usage': usage, **details})
        traces = sorted(trace_path.parent.glob('*.json'), key=lambda p: p.stat().st_mtime) if trace_path.parent.exists() else []
        for old in traces[:-99]:
            old.unlink(missing_ok=True)
        started = time.monotonic()
        with self.locked():
            token = self.access_token()
            # Simple captures keep the short loop. Public recipe research may need
            # ingredient lookup and persistence, so gets three additional turns.
            for turn in range(10):
                if time.monotonic() - started > 240:
                    raise PlanError("L'analyse a pris trop de temps. Ton envoi est conservé.")
                final_turn = (9 if repair_rounds > 1 else
                              6 if nutrition.external_count or nutrition.estimated_count or repair_requested else 3)
                if turn >= final_turn:
                    payload['tool_choice'] = 'none'
                trace('analysing', response_count=turn + research_responses)
                body, output, text = self.response(payload, token)
                for key, value in body.get('usage', {}).items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
                tool_calls = [item for item in output if item.get('type') == 'function_call']
                if tool_calls:
                    if payload['tool_choice'] == 'none' or turn >= final_turn or calls + len(tool_calls) > (16 if repair_rounds > 1 else 12):
                        raise PlanError("Luna a dépassé la limite de recherches. Ton envoi est conservé.")
                    # Stateless HTTP: replay completed calls and encrypted reasoning,
                    # then attach client-executed results; no previous_response_id.
                    payload['input'].extend(output)
                    all_references = True
                    for call in tool_calls:
                        calls += 1
                        try:
                            result = nutrition.execute(call.get('name', ''), json.loads(call.get('arguments', '{}')),
                                                       call.get('namespace'))
                        except (ValueError, TypeError) as error:
                            tool_errors += 1
                            result = {'error': str(error)[:250]}
                        all_references = all_references and bool(result.get('results')) and all(
                            value.get('reference_match') for value in result.get('results', []))
                        payload['input'].append({'type': 'function_call_output', 'call_id': call['call_id'],
                                                 'output': json.dumps(result, ensure_ascii=False)})
                    trace('analysing', response_count=turn + 1 + research_responses)
                    payload['tool_choice'] = 'none' if all_references else 'auto'
                    continue
                pending = nutrition.pending_research()
                if pending:
                    if turn >= final_turn:
                        raise PlanError("Une référence nommée n'a pas été vérifiée. Ton envoi est conservé.")
                    payload['input'].extend(output)
                    payload['input'].append({'role': 'developer', 'content':
                        'Les références nommées suivantes doivent passer par nutrition.research_food '
                        'avant une fiche finale, même pour une approximation : ' + json.dumps(pending, ensure_ascii=False)})
                    payload['tool_choice'] = 'required'
                    continue
                try:
                    draft = json.loads(text)
                    nutrition.validate_selection(draft)
                except (ValueError, TypeError, AttributeError):
                    raise PlanError("Luna n'a pas fourni une fiche vérifiée dans le catalogue. Ton envoi est conservé.")
                missing = nutrition.selection_issues(draft)
                can_repair = not repair_requested or (repair_rounds < 2 and tool_errors > last_repair_errors)
                if missing and nutrition.searched and can_repair and turn < 9:
                    repair_requested = True
                    repair_rounds += 1
                    last_repair_errors = tool_errors
                    trace('repairing', missing=missing)
                    payload['input'].extend(output)
                    payload['input'].append({'role': 'developer', 'content':
                        'Contrôle nutritionnel : ces aliments ont des valeurs absentes, une préparation incompatible ou un plat incomplet : '
                        + json.dumps(missing, ensure_ascii=False) +
                        '. Conserver tous les plats, accompagnements et créneaux. Regrouper les ingrédients '
                        'internes en une seule ligne du plat composé, sans doublon ; inclure ses composants '
                        'usuels manquants en signalant les hypothèses et respecter les exclusions. '
                        'Pour un aliment identifiable, chercher '
                        'un synonyme ou un comparable cohérent ; sinon rechercher ses ingrédients puis utiliser '
                        'nutrition.estimate_recipe pour une composition estimée, avec poids final et hypothèses. '
                        'Ne pas inventer de chiffres ni poser de questions. Une vraie composition impossible à '
                        'identifier peut rester null avec la raison dans note. Fournir ensuite la fiche complète.'})
                    payload['tool_choice'] = 'auto'
                    continue
                # A rejected preparation must not retain plausible-looking
                # numbers for another cooking state after the bounded repair.
                for issue in missing:
                    if issue.get('kind') in ('preparation', 'dish'):
                        item = draft['items'][issue['item_index']]
                        item['food_id'] = None
                        item['note'] = ('Estimation du plat entier à compléter ; les valeurs d’un ingrédient seul ne sont pas utilisées.'
                                        if issue['kind'] == 'dish' else
                                        'Estimation de la cuisson à compléter ; les valeurs du produit cru ne sont pas utilisées.')
                        draft['clarifications'] = [group for group in draft.get('clarifications', [])
                            if group.get('item_index') != issue['item_index']]
                trace('partial' if missing else 'done', missing=missing, draft=draft,
                      response_count=turn + 1 + research_responses, repair_requested=repair_requested)
                protected_write(self.directory / 'last_usage.json', {
                    'model': model, 'at': time.time(), 'usage': usage,
                    'tool_calls': calls, 'tools': nutrition.calls,
                    'response_count': turn + 1 + research_responses})
                return draft
        raise PlanError("La connexion Luna s'est interrompue. Le repas n'a pas été enregistré.")

    @staticmethod
    def response(payload, token):
        with requests.post(API + '/responses', json=payload, headers={'Authorization': 'Bearer ' + token},
                           stream=True, timeout=(10, 75), allow_redirects=False) as response:
            if response.status_code != 200:
                if response.status_code == 429:
                    raise PlanError("Quota ChatGPT atteint. Ton texte reste disponible pour une saisie manuelle.")
                raise PlanError("Luna n'a pas répondu sur ton forfait. Tu peux saisir le repas manuellement.")
            size, fragments, completed_items = 0, [], {}
            for line in response.iter_lines():
                size += len(line)
                if size > 2_000_000:
                    raise PlanError('Réponse Luna trop volumineuse.')
                if not line.startswith(b'data: ') or line[6:] == b'[DONE]':
                    continue
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    raise PlanError('Flux Luna invalide. Ton envoi est conservé.')
                kind = event.get('type')
                if kind == 'response.output_text.delta':
                    fragments.append(event.get('delta', ''))
                elif kind == 'response.output_item.done':
                    completed_items[event['output_index']] = event['item']
                elif kind in ('response.failed', 'response.incomplete', 'error'):
                    error = event.get('response', {}).get('error') or event.get('error') or {}
                    if error.get('code') in ('subscription_sharing_usage_limit_exceeded',
                                             'rate_limit_exceeded', 'insufficient_quota'):
                        raise PlanError("Limite d’usage ChatGPT atteinte pour les applications. Ton envoi est conservé ; réessaie après son renouvellement ou utilise la saisie manuelle.")
                    raise PlanError("Interprétation incomplète ou quota indisponible. Le repas n'a pas été enregistré.")
                elif kind == 'response.completed':
                    body = event['response']
                    output = body.get('output') or [completed_items[index] for index in sorted(completed_items)]
                    text = ''.join(c.get('text', '') for item in output if item.get('type') == 'message'
                                   for c in item.get('content', []) if c.get('type') == 'output_text')
                    return body, output, text or ''.join(fragments)
        raise PlanError("La connexion Luna s'est interrompue. Le repas n'a pas été enregistré.")
