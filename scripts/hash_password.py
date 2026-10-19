#!/usr/bin/env python3
"""Print a DASHBOARD_USERS entry: python3 scripts/hash_password.py alice  (prompts for the password)."""
import getpass
import hashlib
import sys

user = sys.argv[1] if len(sys.argv) > 1 else input("username: ")
if ":" in user or "," in user:
    raise SystemExit("username may not contain ':' or ','")
pw = getpass.getpass("password (min 12 chars): ")
if len(pw) < 12:
    raise SystemExit("use at least 12 characters")
print(f"{user}:{hashlib.sha256(pw.encode()).hexdigest()}")
