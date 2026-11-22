"""Runs the whole pledge story against the API. Usage: python test_flow.py"""
import os, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
from app import app


def client():
    return app.test_client()


def sign_up(c, name, role):
    r = c.post("/api/register", json={"name": name, "email": f"{name.lower()}@x.com", "password": "password123", "role": role})
    assert r.status_code == 201, r.json


owner, other_biz, ann, bob = client(), client(), client(), client()
sign_up(owner, "Owner", "business"); sign_up(other_biz, "Other", "business")
sign_up(ann, "Ann", "customer"); sign_up(bob, "Bob", "customer")

pid = owner.post("/api/products", json={"name": "Test Chips", "category": "Snacks", "price": 30, "goal": 2,
                                        "factory_cost": 50000}).json["id"]
assert ann.post("/api/products", json={}).status_code == 403, "customers cannot list products"
assert owner.post(f"/api/products/{pid}/pledge").status_code == 403, "owner cannot back own product"
assert client().post(f"/api/products/{pid}/pledge").status_code == 401, "must sign in to pledge"

assert ann.post(f"/api/products/{pid}/pledge").json["goal_reached"] is False
owner_notes = owner.get("/api/notifications").json
assert "New pledge" in owner_notes[0]["message"], "founder receives new-pledge notification"
assert owner_notes[0]["read_at"] is None
assert owner.post("/api/notifications/read", json={"ids": [owner_notes[0]["id"]]}).json["ok"] is True
assert owner.get("/api/notifications").json[0]["read_at"] is not None
assert ann.post(f"/api/products/{pid}/pledge").status_code == 409, "only one pledge per person"
assert ann.post(f"/api/products/{pid}/pay").status_code == 409, "cannot pay before it is made"

assert bob.post(f"/api/products/{pid}/pledge").json["goal_reached"] is True
assert bob.get(f"/api/products/{pid}").json["status"] == "in_production"
assert "sent for production" in ann.get("/api/notifications").json[0]["message"]

assert ann.post(f"/api/products/{pid}/manufactured").status_code == 403, "customer cannot mark made"
assert other_biz.post(f"/api/products/{pid}/manufactured").status_code == 403, "other owners cannot either"
assert ann.post(f"/api/products/{pid}/pay").status_code == 409, "still no payment while in production"

assert owner.post(f"/api/products/{pid}/manufactured").status_code == 200
assert "ready" in ann.get("/api/notifications").json[0]["message"]
assert ann.post(f"/api/products/{pid}/pay", json={"method": "UPI demo"}).json["amount"] == 26          # 30 x 0.85 rounded
assert ann.post(f"/api/products/{pid}/pay").status_code == 409, "cannot pay twice"
assert len(owner.get("/api/owner/products").json) == 1
analytics = owner.get("/api/owner/analytics")
assert analytics.status_code == 200
assert analytics.json["stats"]["products"] == 1
assert analytics.json["stats"]["pledges"] == 2
assert analytics.json["stats"]["goals_reached"] == 1
assert analytics.json["stats"]["demo_payments"] == 1
assert len(analytics.json["trend"]) == 7
assert ann.get("/api/owner/analytics").status_code == 403, "customers cannot view founder analytics"
assert ann.get("/api/recommendations").status_code == 200
assert ann.post("/api/notifications/read", json={"all": True}).json["ok"] is True
assert ann.get("/api/notifications").json[0]["read_at"] is not None
assert client().get("/api/owner/analytics").status_code == 401, "analytics requires sign-in"
assert len(client().get("/api/products").json) == 7                        # 6 sample + 1 new
assert ann.get(f"/api/products/{pid}").json["is_owner"] is False and owner.get(f"/api/products/{pid}").json["is_owner"] is True
print("all checks passed")
