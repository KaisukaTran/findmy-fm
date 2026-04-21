#!/usr/bin/env python3
"""Seed an admin user from env vars ADMIN_USERNAME / ADMIN_PASSWORD.

Usage:
    ADMIN_USERNAME=admin ADMIN_PASSWORD=changeme123 python scripts/seed_admin.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

username = os.environ.get("ADMIN_USERNAME", "admin")
password = os.environ.get("ADMIN_PASSWORD", "")

if not password or len(password) < 8:
    print("ERROR: ADMIN_PASSWORD must be set and at least 8 characters.", file=sys.stderr)
    sys.exit(1)

from services.auth.user_repository import ensure_table, get_by_username, create_user

ensure_table()

existing = get_by_username(username)
if existing:
    print(f"User '{username}' already exists (role={existing.role}). No changes made.")
    sys.exit(0)

user = create_user(username, password, role="admin")
print(f"Created admin user: {user.username} (id={user.id})")
