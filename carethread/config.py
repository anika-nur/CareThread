"""
config.py — Settings shared by every script that opens the database.

    CARETHREAD_DB=carethread_frag.db python app.py

CARETHREAD_DB picks the SQLite file. It defaults to carethread.db; the
fragmentation build (build_frag_db.py) writes carethread_frag.db instead, so
the two can sit side by side.
"""

import os

DB_PATH = os.environ.get("CARETHREAD_DB", "carethread.db")
