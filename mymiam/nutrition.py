import json
import math
import re
import unicodedata
from datetime import date

NUTRIENTS = ("kcal", "protein", "carbs", "fat", "fiber")
STOP = {"de", "des", "du", "d", "a", "la", "le", "les", "avec", "au", "aux", "et", "un", "une"}


def normalize(text):
    text = str(text).replace("’", " ").replace("'", " ").replace("œ", "oe").replace("Œ", "OE")
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def finite_number(value, low, high, label):
    if isinstance(value, bool):
        raise ValueError(label + " invalide")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(label + " invalide")
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(label + " hors limites")
    return number


def valid_day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        raise ValueError("Date invalide")


def food_record(row):
    return {"id": row["id"], "name": row["name"], "source": row["source"],
            "nutrients": json.loads(row["nutrients"]), "flags": json.loads(row["flags"])}


def search_foods(store, query, limit=12):
    query = normalize(query)
    words = [w for w in query.split() if w not in STOP]
    if "poulet" in words and "blanc" in words:
        words = ["filet" if w == "blanc" else w for w in words]
    if not words:
        return []
    with store.connect() as db:
        # A small catalogue; score complete food names without dropping qualifiers.
        rows = db.execute("SELECT * FROM foods").fetchall()
    scored = []
    for row in rows:
        text = row["normalized"]
        tokens = set(text.split())
        def matches_word(word):
            if word == "cuit":
                return any(t.startswith(("cuit", "grill", "poel", "roti", "bouilli", "vapeur")) for t in tokens)
            if word == "cru":
                return any(t.startswith("cru") for t in tokens)
            return word in tokens or (len(word) > 3 and any(t.startswith(word) for t in tokens))
        matches = sum(matches_word(w) for w in words)
        if matches != len(words):
            continue
        score = 10 * matches + (40 if query in text else 0) - len(text) / 120
        if text.startswith(query):
            score += 12
        if text.split()[0] in words:
            score += 15
        qualifiers = {"bio", "pane", "foie", "coeur", "grecque", "sucre", "creme", "chevre", "brebis", "bifidus"}
        score -= 12 * len((tokens & qualifiers) - set(words))
        scored.append((score, food_record(row)))
    return [v for _, v in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]


def resolve_items(store, inputs):
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 40:
        raise ValueError("Ajoute entre 1 et 40 aliments")
    result = []
    with store.connect() as db:
        for item in inputs:
            if not isinstance(item, dict):
                raise ValueError("Aliment invalide")
            row = db.execute("SELECT * FROM foods WHERE id=?", (str(item.get("food_id", "")),)).fetchone()
            if not row:
                raise ValueError("Choisis un aliment du catalogue pour chaque ligne")
            grams = finite_number(item.get("grams"), 0.1, 10000, "Quantité en grammes")
            food = food_record(row)
            result.append({"food_id": food["id"], "name": food["name"], "source": food["source"],
                           "grams": grams, "estimated": bool(item.get("estimated", False)),
                           "note": str(item.get("note", ""))[:300], "flags": food["flags"],
                           "nutrients": {k: None if food["nutrients"].get(k) is None else
                                         round(food["nutrients"][k] * grams / 100, 3) for k in NUTRIENTS}})
    return result


def totals(items):
    # If any part is unknown, a total is unknown (never silently a zero).
    return {key: None if any(i["nutrients"].get(key) is None for i in items)
            else round(sum(i["nutrients"].get(key, 0) for i in items), 1) for key in NUTRIENTS}


def validate_profile(data):
    if not isinstance(data, dict):
        raise ValueError("Profil invalide")
    birth = date.fromisoformat(str(data.get("birth_date", "")))
    age = (date.today() - birth).days / 365.2425
    if not 18 <= age <= 110:
        raise ValueError("Profil adulte : vérifie la date de naissance")
    if data.get("sex") not in ("female", "male"):
        raise ValueError("Choisis le paramètre de l'équation de repos")
    result = {"birth_date": birth.isoformat(), "sex": data["sex"],
              "height": finite_number(data.get("height"), 100, 250, "Taille"),
              "weight": finite_number(data.get("weight"), 30, 350, "Poids"),
              "activity_factor": finite_number(data.get("activity_factor"), 1.1, 2.5, "Facteur d'activité"),
              "deficit": finite_number(data.get("deficit", 0), 0, 1000, "Déficit cible")}
    for key, default in (("protein_pct", 20), ("carbs_pct", 45), ("fat_pct", 35)):
        result[key] = finite_number(data.get(key, default), 5, 80, "Répartition des macros")
    if abs(result["protein_pct"] + result["carbs_pct"] + result["fat_pct"] - 100) > 0.01:
        raise ValueError("La répartition des macros doit faire 100 %")
    return result


def resting_energy(profile, at_day, weight=None):
    if not profile:
        return None
    birth, when = date.fromisoformat(profile["birth_date"]), date.fromisoformat(at_day)
    age = when.year - birth.year - ((when.month, when.day) < (birth.month, birth.day))
    return round(10 * (weight or profile["weight"]) + 6.25 * profile["height"] - 5 * age
                 + (5 if profile["sex"] == "male" else -161))


def targets(profile, expenditure):
    if not profile or expenditure is None:
        return None
    energy = max(0, expenditure - profile["deficit"])
    return {"kcal": round(energy), "protein": round(energy * profile["protein_pct"] / 400),
            "carbs": round(energy * profile["carbs_pct"] / 400),
            "fat": round(energy * profile["fat_pct"] / 900)}
