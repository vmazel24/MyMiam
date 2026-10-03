import json
from datetime import date, timedelta
from .nutrition import resting_energy, targets, totals, nutrient_bounds, total_bounds


def summary(store, user_id, day):
    today = date.today().isoformat()
    with store.connect() as db:
        row = db.execute("SELECT data FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        profile = json.loads(row[0]) if row else None
        weight = db.execute("SELECT weight FROM weights WHERE user_id=? AND day<=? ORDER BY day DESC LIMIT 1",
                            (user_id, day)).fetchone()
        meals = []
        for row in db.execute("SELECT * FROM meals WHERE user_id=? AND day=? ORDER BY created", (user_id, day)):
            meal = dict(row)
            meal["items"] = json.loads(meal["items"])
            for item in meal['items']:
                item['nutrient_bounds'] = nutrient_bounds(item)
            meal["totals"] = totals(meal["items"])
            meal['totals_bounds'] = total_bounds(meal['items'])
            info = db.execute('SELECT data FROM meal_insights WHERE meal_id=?', (meal['id'],)).fetchone()
            meal['clarifications'] = json.loads(info[0]) if info else []
            meals.append(meal)
        row = db.execute("SELECT * FROM days WHERE user_id=? AND day=?", (user_id, day)).fetchone()
        # No confirmation step: every logged meal contributes immediately,
        # including meals saved before automatic journals were introduced.
        complete = bool(meals)
        # Past-day snapshots keep goals stable; today's profile remains editable.
        if day < today and row and row["goal"]:
            profile = json.loads(row["goal"])
        row = db.execute("SELECT data FROM garmin_days WHERE user_id=? AND day=?", (user_id, day)).fetchone()
        garmin = json.loads(row[0]) if row else None
    target_breakdown = None
    if garmin and garmin["has_data"]:
        # Garmin is the sole expenditure source, including for a partial day.
        # Its total already contains active calories; never add them again.
        expenditure, source = garmin["total"], "Total Garmin"
        resting = garmin.get("resting")
        active = garmin.get("active")
        if active is not None and 0 <= active <= expenditure:
            if resting is None:
                resting = expenditure - active
            if profile:
                target_breakdown = {'base': expenditure - active, 'active': active,
                                    'deficit': profile['deficit'], 'base_source': 'Repos Garmin'}
    else:
        resting = resting_energy(profile, day, weight[0] if weight else None)
        expenditure = round(resting * profile["activity_factor"]) if profile else None
        source = "Estimation du profil"
    items = [item for meal in meals for item in meal["items"]]
    intake = totals(items)
    deficit = None if expenditure is None or intake["kcal"] is None or not complete else round(expenditure - intake["kcal"])
    return {"day": day, "meals": meals, "intake": intake, "intake_bounds": total_bounds(items), "has_meals": bool(meals), "complete": complete,
            "resting": resting, "expenditure": expenditure, "expenditure_source": source,
            "projected": day >= today, "garmin": garmin, "targets": targets(profile, expenditure),
            "target_breakdown": target_breakdown,
            "target_deficit": profile["deficit"] if profile else None,
            "deficit": deficit, "estimated_portions": sum(bool(i.get("estimated")) for i in items),
            "missing_nutrients": sum(any(v is None for v in i["nutrients"].values()) for i in items)}


def trends(store, user_id, end_day, count=30):
    end = date.fromisoformat(end_day)
    days = [summary(store, user_id, (end - timedelta(days=i)).isoformat()) for i in range(count - 1, -1, -1)]
    # Today's balance remains provisional and is excluded from historical totals.
    included = [d for d in days if d["day"] < date.today().isoformat() and d["deficit"] is not None]
    with store.connect() as db:
        weights = [dict(row) for row in db.execute("SELECT day,weight FROM weights WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day",
                                                  (user_id, days[0]["day"], end_day))]
    return {"days": days, "cumulative_deficit": sum(d["deficit"] for d in included),
            "covered_days": len(included), "elapsed_days": sum(d["day"] < date.today().isoformat() for d in days),
            "weights": weights}
