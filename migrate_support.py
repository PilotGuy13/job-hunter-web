#!/usr/bin/env python3
"""
Migration: Create support_tickets table.
Run on PythonAnywhere: cd ~/job-hunter-web && python3 migrate_support.py
"""
import sqlite3, os

DB_PATH = os.path.join(os.path.dirname(__file__), "instance", "job_hunter.db")

if not os.path.exists(DB_PATH):
    print(f"Database not found at {DB_PATH}")
    exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS support_tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_number   VARCHAR(16) UNIQUE NOT NULL,
    user_id         INTEGER REFERENCES users(id),
    name            VARCHAR(120) DEFAULT '',
    email           VARCHAR(120) DEFAULT '',
    category        VARCHAR(64) DEFAULT 'general',
    priority        VARCHAR(16) DEFAULT 'normal',
    status          VARCHAR(16) DEFAULT 'new',
    subject         VARCHAR(256) DEFAULT '',
    message         TEXT DEFAULT '',
    admin_notes     TEXT DEFAULT '',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at     DATETIME,
    user_plan       VARCHAR(16) DEFAULT ''
)
""")

print("✅ support_tickets table created (or already exists)")
conn.commit()
conn.close()
print("✅ Migration complete!")
