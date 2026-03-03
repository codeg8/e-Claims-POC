import os
import jwt

JWT_SECRET  = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod-at-least-256-bits-long")

users = [
    {"sub": "user-1", "role": "CLAIMANT",   "email": "user-c@example.com"},
    {"sub": "user-2", "role": "ADJUSTER",   "email": "user-a@example.com"},
    {"sub": "user-3", "role": "SUPERVISOR", "email": "user-s@example.com"},
]

print("JWT_SECRET: ", JWT_SECRET)
for user in users:
    token = jwt.encode(user, JWT_SECRET, algorithm="HS256")
    print(f"{user['role']} token: {token}")
