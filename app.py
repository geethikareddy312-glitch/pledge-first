"""
Pledge First: backend API (Flask + SQLite).

Rules this backend enforces (so the website cannot cheat):
  * Only signed-in users can pledge, and only once per product.
  * A business owner cannot pledge for their own product.
  * When pledges reach the goal, the product goes "in_production" and every
    backer is notified. Nobody is asked to pay yet.
  * Only the business owner who listed a product can mark it "made".
  * Backers can pay only after the product is "made". Payment here is simulated.
"""
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from functools import wraps

from flask import Flask, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(HERE, "pledge_first.db"))
DAY = 86400
WINDOW_DAYS = 7          # a product has 7 days to reach its goal
EARLY_BIRD = 0.85        # backers pay 15% less than the normal price
CATEGORY_AVG = {"Snacks": 25, "Beverages": 35, "Personal care": 180, "Home": 220}

with open(os.path.join(HERE, "model.json")) as f:   # made by train_demand_model.py
    MODEL = json.load(f)

app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="")
app.config.update(
    # Set SECRET_KEY in the environment for real use, or sessions reset on every restart.
    SECRET_KEY=os.environ.get("SECRET_KEY") or os.urandom(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('customer','business')));
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
  name TEXT NOT NULL, category TEXT NOT NULL, price INTEGER NOT NULL, goal INTEGER NOT NULL,
  factory_cost INTEGER NOT NULL, description TEXT, how_made TEXT, ingredients TEXT, packaging TEXT, net_quantity TEXT, shelf_life TEXT, facts TEXT,
  status TEXT NOT NULL DEFAULT 'collecting' CHECK(status IN ('collecting','in_production','made')),
  created_at INTEGER NOT NULL, ends_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pledges(
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  user_id INTEGER NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
  paid INTEGER NOT NULL DEFAULT 0,
  payment_method TEXT, payment_ref TEXT,
  UNIQUE(product_id, user_id));            -- this line is what stops double pledges
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  product_id INTEGER REFERENCES products(id), message TEXT NOT NULL, created_at INTEGER NOT NULL,
  read_at INTEGER);
"""


# ---------- database helpers ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, isolation_level=None)   # we manage transactions ourselves
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


@contextmanager
def tx():
    """One all-or-nothing transaction. BEGIN IMMEDIATE blocks other writers, so two
    people pledging at the same moment cannot both squeeze past the goal."""
    con = db()
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def now():
    return int(time.time())


def err(message, code):
    return jsonify(error=message), code


# ---------- auth helpers ----------
def current_user():
    uid = session.get("uid")
    if uid is None:
        return None
    user = db().execute("SELECT id,name,email,role FROM users WHERE id=?", (uid,)).fetchone()
    if user is None:
        session.clear()
    return user


def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if current_user() is None:
            return err("Please sign in first", 401)
        return f(*a, **k)
    return wrapper


def business_only(f):
    @login_required
    @wraps(f)
    def wrapper(*a, **k):
        if current_user()["role"] != "business":
            return err("Only business owners can do this", 403)
        return f(*a, **k)
    return wrapper


def to_int(value, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


# ---------- product helpers ----------
def effective_status(p, pledged):
    if p["status"] != "collecting":
        return p["status"]
    return "collecting" if now() < p["ends_at"] else "missed"


def chance_of_reaching_goal(p, pledged):
    """Logistic regression trained in train_demand_model.py (on synthetic data)."""
    day = min(WINDOW_DAYS, int((now() - p["created_at"]) // DAY) + 1)
    ratio = min(4.0, max(0.3, p["price"] / CATEGORY_AVG.get(p["category"], 50)))
    x = [ratio, math.log10(p["goal"]), pledged / p["goal"], day]
    z = MODEL["b"] + sum((xi - mu) / sd * w for xi, mu, sd, w in zip(x, MODEL["mu"], MODEL["sd"], MODEL["w"]))
    z = max(-30.0, min(30.0, z))
    return max(1, min(99, round(100 / (1 + math.exp(-z)))))


def product_json(p, pledged, user_id=None):
    status = effective_status(p, pledged)
    out = {
        "id": p["id"], "name": p["name"], "category": p["category"], "description": p["description"],
        "price": p["price"], "early_bird_price": round(p["price"] * EARLY_BIRD),
        "goal": p["goal"], "pledged": pledged, "status": status,
        "days_left": max(0, math.ceil((p["ends_at"] - now()) / DAY)) if status == "collecting" else 0,
        "how_made": p["how_made"], "ingredients": p["ingredients"], "packaging": p["packaging"],
        "net_quantity": p["net_quantity"], "shelf_life": p["shelf_life"], "facts": p["facts"],
    }
    if status == "collecting":
        out["chance_of_reaching_goal"] = chance_of_reaching_goal(p, pledged)
    if user_id is not None:
        out["is_owner"] = p["owner_id"] == user_id
        row = db().execute("SELECT paid FROM pledges WHERE product_id=? AND user_id=?", (p["id"], user_id)).fetchone()
        out["you_pledged"] = row is not None
        out["you_paid"] = bool(row["paid"]) if row else False
    return out


PRODUCT_SQL = ("SELECT p.*, (SELECT COUNT(*) FROM pledges WHERE product_id=p.id) AS pledged "
               "FROM products p")


def notify_backers(con, product_id, message):
    con.execute("INSERT INTO notifications(user_id,product_id,message,created_at) "
                "SELECT user_id,?,?,? FROM pledges WHERE product_id=?", (product_id, message, now(), product_id))


# ---------- routes: accounts ----------
@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}
    name, email = str(d.get("name", "")).strip(), str(d.get("email", "")).strip().lower()
    password, role = str(d.get("password", "")), d.get("role", "customer")
    if not (1 <= len(name) <= 30) or "@" not in email or len(email) > 120:
        return err("Enter a name (up to 30 characters) and a valid email", 400)
    if len(password) < 8:
        return err("Password must be at least 8 characters", 400)
    if role not in ("customer", "business"):
        return err("Role must be customer or business", 400)
    try:
        cur = db().execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
                           (name, email, generate_password_hash(password), role))
    except sqlite3.IntegrityError:
        return err("That email is already registered", 409)
    session.clear()
    session["uid"] = cur.lastrowid
    return jsonify(id=cur.lastrowid, name=name, email=email, role=role), 201


@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    row = db().execute("SELECT * FROM users WHERE email=?", (str(d.get("email", "")).strip().lower(),)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], str(d.get("password", ""))):
        return err("Wrong email or password", 401)     # same message for both, on purpose
    session.clear()
    session["uid"] = row["id"]
    return jsonify(id=row["id"], name=row["name"], email=row["email"], role=row["role"])


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def me():
    return jsonify(dict(current_user()))


# ---------- routes: products ----------
@app.get("/api/products")
def list_products():
    user = current_user()
    sql, args = PRODUCT_SQL, []
    category = request.args.get("category")
    if category:
        sql, args = sql + " WHERE p.category=?", [category]
    rows = db().execute(sql + " ORDER BY p.created_at DESC", args).fetchall()
    return jsonify([product_json(r, r["pledged"], user["id"] if user else None) for r in rows])


@app.get("/api/products/<int:pid>")
def get_product(pid):
    user = current_user()
    r = db().execute(PRODUCT_SQL + " WHERE p.id=?", (pid,)).fetchone()
    if r is None:
        return err("Product not found", 404)
    return jsonify(product_json(r, r["pledged"], user["id"] if user else None))


@app.post("/api/products")
@business_only
def create_product():
    d = request.get_json(silent=True) or {}
    name = str(d.get("name", "")).strip()
    price, goal = to_int(d.get("price"), 5, 2000), to_int(d.get("goal"), 2, 5000)
    cost = to_int(d.get("factory_cost"), 1000, 10**8)
    if not (1 <= len(name) <= 40) or d.get("category") not in CATEGORY_AVG or None in (price, goal, cost):
        return err("Check the name, category, price (5-2000), goal (2-5000) and factory cost", 400)
    text = lambda key, n: str(d.get(key, "")).strip()[:n]
    cur = db().execute(
        "INSERT INTO products(owner_id,name,category,price,goal,factory_cost,description,how_made,"
        "ingredients,packaging,created_at,ends_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (session["uid"], name, d["category"], price, goal, cost, text("description", 120),
         text("how_made", 300), text("ingredients", 300), text("packaging", 300),
         now(), now() + WINDOW_DAYS * DAY))
    return jsonify(id=cur.lastrowid), 201


@app.get("/api/owner/products")
@business_only
def owner_products():
    rows = db().execute(PRODUCT_SQL + " WHERE p.owner_id=? ORDER BY p.created_at DESC", (session["uid"],)).fetchall()
    return jsonify([product_json(r, r["pledged"], session["uid"]) | {"factory_cost": r["factory_cost"]} for r in rows])


@app.get("/api/owner/analytics")
@business_only
def owner_analytics():
    """Aggregated analytics for the signed-in founder, based on stored pledge rows."""
    uid = session["uid"]
    rows = db().execute(
        PRODUCT_SQL + " WHERE p.owner_id=? ORDER BY p.created_at DESC", (uid,)
    ).fetchall()
    products = [product_json(r, r["pledged"], uid) for r in rows]
    total_products = len(rows)
    total_pledges = sum(r["pledged"] for r in rows)
    reached = sum(1 for x in products if x["status"] in ("in_production", "made"))
    active = sum(1 for x in products if x["status"] == "collecting")
    made = sum(1 for x in products if x["status"] == "made")
    paid = db().execute(
        "SELECT COUNT(*) FROM pledges pl JOIN products p ON p.id=pl.product_id "
        "WHERE p.owner_id=? AND pl.paid=1", (uid,)
    ).fetchone()[0]
    category_rows = db().execute(
        "SELECT p.category, COUNT(DISTINCT p.id) AS products, COUNT(pl.id) AS pledges "
        "FROM products p LEFT JOIN pledges pl ON pl.product_id=p.id "
        "WHERE p.owner_id=? GROUP BY p.category ORDER BY pledges DESC, p.category", (uid,)
    ).fetchall()
    trend_rows = db().execute(
        "SELECT date(pl.created_at,'unixepoch') AS day, COUNT(*) AS pledges "
        "FROM pledges pl JOIN products p ON p.id=pl.product_id "
        "WHERE p.owner_id=? AND pl.created_at >= ? GROUP BY day ORDER BY day",
        (uid, now() - 6 * DAY)
    ).fetchall()
    trend_map = {r["day"]: r["pledges"] for r in trend_rows}
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).date()
    trend = []
    for offset in range(6, -1, -1):
        day = (today - datetime.timedelta(days=offset)).isoformat()
        trend.append({"date": day, "label": (today - datetime.timedelta(days=offset)).strftime("%a"),
                      "pledges": trend_map.get(day, 0)})
    # Calibrate the synthetic-model score with this founder's completed campaign history.
    # A small Beta-style prior (strength 4) prevents tiny samples from dominating.
    history_rows = db().execute(
        "SELECT p.category, COUNT(*) AS campaigns, "
        "SUM(CASE WHEN p.status IN ('in_production','made') THEN 1 ELSE 0 END) AS successes "
        "FROM products p WHERE p.owner_id=? AND (p.status IN ('in_production','made') OR p.ends_at<=?) "
        "GROUP BY p.category", (uid, now())
    ).fetchall()
    history = {r["category"]: {"campaigns": r["campaigns"], "successes": r["successes"] or 0}
               for r in history_rows}
    predictions = []
    for x in products:
        if x["status"] != "collecting":
            continue
        h = history.get(x["category"], {"campaigns": 0, "successes": 0})
        base = int(x.get("chance_of_reaching_goal") or 1)
        n, wins = int(h["campaigns"]), int(h["successes"])
        adjusted = round((base * 4 + wins * 100) / (4 + n))
        predictions.append({
            "id": x["id"], "name": x["name"], "chance": max(1, min(99, adjusted)),
            "model_score": base, "pledged": x["pledged"], "goal": x["goal"],
            "status": x["status"], "history_count": n, "history_successes": wins,
            "basis": "owner_category_history" if n else "synthetic_model_only"
        })
    return jsonify(
        stats={"products": total_products, "pledges": total_pledges, "goals_reached": reached,
               "active": active, "made": made, "demo_payments": paid},
        trend=trend, categories=[dict(r) for r in category_rows],
        predictions=predictions, products=products
    )


@app.get("/api/recommendations")
def recommendations():
    """Simple explainable recommendations based on categories a customer has backed."""
    user = current_user()
    rows = db().execute(PRODUCT_SQL + " ORDER BY p.created_at DESC").fetchall()
    all_items = [product_json(r, r["pledged"], user["id"] if user else None) for r in rows]
    preferred = None
    if user:
        pref = db().execute(
            "SELECT p.category, COUNT(*) AS n FROM pledges pl JOIN products p ON p.id=pl.product_id "
            "WHERE pl.user_id=? GROUP BY p.category ORDER BY n DESC LIMIT 1", (user["id"],)
        ).fetchone()
        if pref:
            preferred = pref["category"]
    if not preferred:
        preferred = db().execute(
            "SELECT p.category FROM products p LEFT JOIN pledges pl ON pl.product_id=p.id "
            "GROUP BY p.category ORDER BY COUNT(pl.id) DESC LIMIT 1"
        ).fetchone()
        preferred = preferred["category"] if preferred else None
    picks = [x for x in all_items if x["category"] == preferred and not x.get("you_pledged") and x["status"] == "collecting"]
    return jsonify(category=preferred, items=picks[:3], reason=("Based on a category you have supported" if user else "Popular category across demo campaigns"))




@app.post("/api/products/<int:pid>/pledge")
@login_required
def pledge(pid):
    uid = session["uid"]
    with tx() as con:
        p = con.execute(PRODUCT_SQL + " WHERE p.id=?", (pid,)).fetchone()
        if p is None:
            return err("Product not found", 404)
        if p["owner_id"] == uid:
            return err("You cannot pledge for your own product", 403)
        if effective_status(p, p["pledged"]) != "collecting":
            return err("This product is no longer taking pledges", 409)
        try:
            con.execute("INSERT INTO pledges(product_id,user_id,created_at) VALUES(?,?,?)", (pid, uid, now()))
        except sqlite3.IntegrityError:
            return err("You have already pledged for this product", 409)
        pledged = p["pledged"] + 1
        con.execute("INSERT INTO notifications(user_id,product_id,message,created_at) VALUES(?,?,?,?)",
                    (p["owner_id"], pid, f"New pledge for {p['name']}: {pledged} of {p['goal']} pledges received.", now()))
        if pledged >= p["goal"]:
            con.execute("UPDATE products SET status='in_production' WHERE id=?", (pid,))
            notify_backers(con, pid, f"{p['name']} reached its goal and has been sent for production. "
                                     "You pay nothing now. We will tell you when it is ready.")
    return jsonify(pledged=pledged, goal=p["goal"], goal_reached=pledged >= p["goal"])


@app.post("/api/products/<int:pid>/manufactured")
@business_only
def mark_manufactured(pid):
    with tx() as con:
        p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if p is None:
            return err("Product not found", 404)
        if p["owner_id"] != session["uid"]:
            return err("Only the owner of this product can mark it as manufactured", 403)
        if p["status"] != "in_production":
            return err("Only a product that reached its goal can be marked as manufactured", 409)
        con.execute("UPDATE products SET status='made' WHERE id=?", (pid,))
        notify_backers(con, pid, f"{p['name']} has been made and is ready. "
                                 f"Pay ₹{round(p['price'] * EARLY_BIRD)} to get yours.")
    return jsonify(ok=True)


@app.post("/api/products/<int:pid>/pay")
@login_required
def pay(pid):
    uid = session["uid"]
    with tx() as con:
        p = con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        pl = con.execute("SELECT * FROM pledges WHERE product_id=? AND user_id=?", (pid, uid)).fetchone()
        if p is None or pl is None:
            return err("You have not pledged for this product", 404)
        if p["status"] != "made":
            return err("You can pay only after the product has been made", 409)
        if pl["paid"]:
            return err("You have already paid", 409)
        d = request.get_json(silent=True) or {}
        method = d.get("method")
        if method not in ("UPI demo", "Card demo", "Cash on delivery demo"):
            return err("Choose one of the demo payment methods", 400)
        # Classroom simulation only: no gateway, card data, bank data or money movement.
        reference = "PF-DEMO-" + str(pl["id"]) + "-" + str(now())
        con.execute("UPDATE pledges SET paid=1,payment_method=?,payment_ref=? WHERE id=?",
                    (method, reference, pl["id"]))
    return jsonify(ok=True, amount=round(p["price"] * EARLY_BIRD), method=method,
                   reference=reference, message="Demo payment successful. No money was charged.")


@app.get("/api/notifications")
@login_required
def notifications():
    rows = db().execute("SELECT id,product_id,message,created_at,read_at FROM notifications "
                        "WHERE user_id=? ORDER BY id DESC LIMIT 50", (session["uid"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/notifications/read")
@login_required
def mark_notifications_read():
    d = request.get_json(silent=True) or {}
    uid = session["uid"]
    timestamp = now()
    if d.get("all") is True:
        db().execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (timestamp, uid))
    else:
        ids = d.get("ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(i, int) or i < 1 for i in ids):
            return err("Provide notification IDs or set all to true", 400)
        placeholders = ",".join("?" for _ in ids)
        db().execute(f"UPDATE notifications SET read_at=? WHERE user_id=? AND id IN ({placeholders}) AND read_at IS NULL",
                     [timestamp, uid] + ids)
    return jsonify(ok=True)


@app.get("/")
def index():
    return app.send_static_file("index.html")


# ---------- setup ----------
SAMPLES = [  # name, category, price, goal, factory cost, description, how made, ingredients, packaging, net, shelf life, facts, sample backers
    ("Roasted Chana Crunch", "Snacks", 25, 5, 60000, "Crunchy roasted chickpeas with a light chaat seasoning.",
     "Chickpeas are soaked overnight, air-dried, then roasted in small batches without deep frying. A light coat of oil and seasoning is added while they are still warm, then they are cooled and packed.",
     "Chickpeas 85%, sunflower oil, rock salt, black pepper, chaat masala (dry mango, cumin, coriander).",
     "Two-layer paper and foil pouch that keeps out moisture. The front shows the product, the back carries the label.",
     "35 g", "5 months", "Energy (per 100 g)|380 kcal\nProtein|19 g\nCarbohydrate|58 g\nFat|8 g\nSodium|450 mg", 3),
    ("Peri-Peri Makhana Chips", "Snacks", 30, 200, 150000, "Baked fox nuts with a spicy coating.",
     "Fox nuts (makhana) are sun-dried, popped, then roasted in a little oil. Peri-peri seasoning is tumbled on while they are warm, then they are cooled and sealed under nitrogen to stay crisp.",
     "Fox nuts 70%, rice bran oil, peri-peri seasoning (red chilli, garlic, onion, paprika, salt), citric acid.",
     "Three-layer metallised film pouch, flushed with nitrogen gas so the chips do not crush or go stale. Printed with the ingredient list on the back.",
     "40 g", "6 months", "Energy (per 100 g)|420 kcal\nProtein|9 g\nCarbohydrate|62 g\nFat|14 g\nSodium|520 mg", 164),
    ("Cold Brew Chai Concentrate", "Beverages", 120, 150, 220000, "Dilute with water or milk, ready in seconds.",
     "Assam tea leaves and whole spices are steeped in cold water for 12 hours, filtered, lightly sweetened, then bottled and pasteurised. Mix one part concentrate with three parts water or milk.",
     "Water, Assam tea leaves, cane sugar, ginger, cardamom, cinnamon, clove.",
     "Glass bottle with a tamper-proof cap and a recycled paper sleeve. Keep in the fridge after opening and use within 7 days.",
     "250 ml bottle", "4 months unopened", "Energy (per 30 ml)|45 kcal\nCarbohydrate|11 g\nof which sugar|10 g\nFat|0 g\nCaffeine|about 25 mg", 38),
    ("Neem and Tulsi Face Wash", "Personal care", 150, 300, 300000, "Gentle daily wash for oily skin.",
     "Neem and tulsi leaves are steeped to make an extract, which is blended into a mild cleanser base. The mix is checked for skin-friendly pH, then filled into tubes.",
     "Water, mild coconut-derived cleanser, neem extract, tulsi extract, glycerin, aloe vera, preservative.",
     "Laminated squeeze tube with a flip cap, inside a carton that lists directions and cautions.",
     "100 ml tube", "24 months", "pH|5.5\nHow to use|Twice a day\nSkin type|Oily and combination\nStorage|Below 30 C", 240),
    ("Jackfruit Jerky", "Snacks", 90, 250, 180000, "Smoky plant-based chewy snack.",
     "Raw jackfruit is boiled, shredded and marinated overnight in a smoky spice mix. It is then slow-dried at low heat for about 8 hours until chewy.",
     "Raw jackfruit 80%, soy sauce, smoked paprika, jaggery, garlic, black pepper, salt.",
     "Resealable stand-up pouch with a small oxygen-absorber sachet inside to keep it fresh.",
     "50 g", "5 months", "Energy (per 100 g)|280 kcal\nProtein|6 g\nCarbohydrate|55 g\nFibre|12 g\nSodium|780 mg", 12),
    ("Bamboo Dish Scrub Set", "Home", 199, 120, 90000, "Plastic-free scrubs that last months.",
     "Bamboo fibre is softened, pressed into flat pads and dried. Cotton loops are stitched on by hand so each scrub can hang to dry.",
     "Bamboo fibre, natural cotton loop, plant-based binder. No plastic.",
     "Recycled cardboard sleeve with no plastic wrap. The scrubs can be composted after use.",
     "Pack of 3", "Lasts 2 to 3 months each", "Contents|3 scrubs\nSafe on non-stick|Yes\nDishwasher safe|No", 57),
]


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    # Upgrade databases created by earlier versions without disturbing existing rows.
    notification_columns = {row[1] for row in con.execute("PRAGMA table_info(notifications)")}
    if "read_at" not in notification_columns:
        con.execute("ALTER TABLE notifications ADD COLUMN read_at INTEGER")
    pledge_columns = {row[1] for row in con.execute("PRAGMA table_info(pledges)")}
    if "payment_method" not in pledge_columns:
        con.execute("ALTER TABLE pledges ADD COLUMN payment_method TEXT")
    if "payment_ref" not in pledge_columns:
        con.execute("ALTER TABLE pledges ADD COLUMN payment_ref TEXT")
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        # Sample founder and sample backers. They share one random, unknown password,
        # so nobody can log in as them. They only make the sample pledge counts real.
        unusable = generate_password_hash(os.urandom(16).hex())
        owner = con.execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
                            ("Sample Founder", "sample-founder@example.com", unusable, "business")).lastrowid
        most = max(row[-1] for row in SAMPLES)
        backers = [con.execute("INSERT INTO users(name,email,password_hash,role) VALUES(?,?,?,?)",
                               (f"Sample backer {i}", f"sample-backer-{i}@example.com", unusable, "customer")).lastrowid
                   for i in range(most)]
        t = now()
        for n, c, p, g_, k, d, how, ing, pack, net, shelf, facts, backed in SAMPLES:
            pid = con.execute(
                "INSERT INTO products(owner_id,name,category,price,goal,factory_cost,description,how_made,ingredients,"
                "packaging,net_quantity,shelf_life,facts,created_at,ends_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (owner, n, c, p, g_, k, d, how, ing, pack, net, shelf, facts, t, t + WINDOW_DAYS * DAY)).lastrowid
            con.executemany("INSERT INTO pledges(product_id,user_id,created_at) VALUES(?,?,?)",
                            [(pid, u, t) for u in backers[:backed]])
    con.commit()
    con.close()


init_db()

if __name__ == "__main__":
    import threading
    import webbrowser
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000")).start()   # opens your browser
    print("Pledge First is running at http://localhost:5000  (close this window to stop it)")
    app.run(port=5000)        # for real use, run behind gunicorn/waitress with HTTPS
