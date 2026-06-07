#!/usr/bin/env python3
"""
Run on PythonAnywhere Bash console after uploading bug_fixes_batch1.zip:
  python3 deploy_fixes.py

Backs up current files, then overwrites with fixed versions.
"""
import zipfile, os, shutil
from datetime import datetime

ZIP = os.path.expanduser("~/bug_fixes_batch1.zip")
TARGET = "/home/jobhunterweb/job-hunter-web"
BACKUP = os.path.expanduser(f"~/backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

if not os.path.exists(ZIP):
    print(f"❌ Upload {ZIP} first via the Files tab")
    exit(1)

# Backup
os.makedirs(BACKUP, exist_ok=True)
os.makedirs(os.path.join(BACKUP, "templates"), exist_ok=True)

with zipfile.ZipFile(ZIP, "r") as zf:
    for name in zf.namelist():
        target = os.path.join(TARGET, name)
        backup = os.path.join(BACKUP, name)
        if os.path.exists(target):
            shutil.copy2(target, backup)
            print(f"  📦 Backed up: {name}")
        zf.extract(name, TARGET)
        print(f"  ✅ Deployed:  {name}")

print(f"\n✅ All files deployed to {TARGET}")
print(f"📦 Backups saved to {BACKUP}")
print(f"\n👉 Now reload your web app in the Web tab")
print(f"👉 Then run your regression tests")
