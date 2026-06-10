#!/usr/bin/env python3
"""
Migration script: Add columns for OTP email verification (#68) and country cooldown (#66).
Run on PythonAnywhere Bash:
    cd ~/job-hunter-web && python3 migrate_68_66.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "instance", "job_hunter.db")

if not os.path.exists(DB_PATH):
    print(f"Database not found at {DB_PATH}")
    exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

migrations = [
    ("email_verified",     "ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 1"),
    ("email_otp",          "ALTER TABLE users ADD COLUMN email_otp VARCHAR(6) DEFAULT ''"),
    ("otp_expires",        "ALTER TABLE users ADD COLUMN otp_expires DATETIME"),
    ("country_changed_at", "ALTER TABLE users ADD COLUMN country_changed_at DATETIME"),
]

for col_name, sql in migrations:
    try:
        cursor.execute(sql)
        print(f"  ✅ Added column: {col_name}")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print(f"  ⏭️  Column already exists: {col_name}")
        else:
            print(f"  ❌ Error adding {col_name}: {e}")

# Ensure all existing users are marked as verified
cursor.execute("UPDATE users SET email_verified = 1 WHERE email_verified IS NULL")
print(f"  ✅ Marked all existing users as email_verified")

conn.commit()
conn.close()
print("\n✅ Migration complete!")
