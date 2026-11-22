"""
Demand model for Pledge First.
Predicts the probability that a product concept reaches its pledge goal
within 7 days, using what is known on a given day.

NOTE: the training data is SYNTHETIC (generated below), because no real
launch data was available. The pipeline is real; swap in real data later.
"""
import json
import numpy as np

rng = np.random.default_rng(42)
AVG = {"Snacks": 25, "Beverages": 35, "Personal care": 180, "Home": 220}
cats = list(AVG)

def make_dataset(n=6000):
    X, y = [], []
    for _ in range(n):
        cat = rng.choice(cats)
        ratio = float(np.clip(rng.lognormal(0, 0.45), 0.3, 4.0))   # price / category average
        goal = int(rng.choice([50, 100, 150, 200, 300, 500, 1000]))
        # hidden "true" appeal: cheaper than average and smaller goals do better, plus luck
        appeal = 1.75 - 0.55 * ratio - 0.12 * np.log10(goal) + rng.normal(0, 0.35)
        rate = max(0.02, appeal) * goal / 7.0                       # expected pledges per day
        daily = rng.poisson(rate, size=7)
        total = daily.sum()
        d = int(rng.integers(1, 8))                                 # day number when we look (1 to 7)
        frac = daily[:d].sum() / goal                               # share of the goal pledged so far
        X.append([ratio, np.log10(goal), frac, d])
        y.append(1 if total >= goal else 0)
    return np.array(X, float), np.array(y, float)

X, y = make_dataset()
split = int(0.8 * len(X))
Xtr, Xte, ytr, yte = X[:split], X[split:], y[:split], y[split:]

mu, sd = Xtr.mean(0), Xtr.std(0)
norm = lambda A: (A - mu) / sd

def sigmoid(z): return 1 / (1 + np.exp(-z))

# logistic regression trained by gradient descent
w, b = np.zeros(X.shape[1]), 0.0
A = norm(Xtr)
for _ in range(4000):
    p = sigmoid(A @ w + b)
    w -= 0.3 * (A.T @ (p - ytr) / len(ytr) + 0.01 * w)   # small L2 penalty
    b -= 0.3 * np.mean(p - ytr)

pred = sigmoid(norm(Xte) @ w + b) > 0.5
print("test accuracy:", round(float((pred == yte).mean()), 3))
print("baseline (always guess majority):", round(float(max(yte.mean(), 1 - yte.mean())), 3))
print("weights [price_ratio, log10_goal, fraction_so_far, day_number]:", np.round(w, 2).tolist(), "bias", round(float(b), 2))

json.dump({"mu": mu.round(4).tolist(), "sd": sd.round(4).tolist(),
           "w": w.round(4).tolist(), "b": round(float(b), 4)}, open("model.json", "w"))
print(open("model.json").read())
