"""SQLite evidence store. Source data is never modified."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


MAX_EVIDENCE_FRAMES = 10
REVIEW_FIELDS = {"status", "notes", "address", "title", "municipal_authority", "reporter_name", "reporter_contact",
                 "severity", "reported_at"}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    source TEXT, config TEXT, status TEXT);
                CREATE TABLE IF NOT EXISTS hazards (
                    id TEXT PRIMARY KEY, session_id TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reference_imports (
                    id INTEGER PRIMARY KEY, source TEXT, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hazard_review (
                    hazard_id TEXT PRIMARY KEY REFERENCES hazards(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hazard_evidence (
                    id INTEGER PRIMARY KEY, hazard_id TEXT NOT NULL REFERENCES hazards(id),
                    frame_key TEXT NOT NULL, jpeg BLOB NOT NULL, metadata TEXT NOT NULL,
                    sha256 TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(hazard_id, frame_key));
                CREATE INDEX IF NOT EXISTS evidence_hazard_idx ON hazard_evidence(hazard_id,id);
                CREATE INDEX IF NOT EXISTS hazards_session_idx ON hazards(session_id);
                CREATE TRIGGER IF NOT EXISTS evidence_ten_frame_limit
                BEFORE INSERT ON hazard_evidence
                WHEN (SELECT COUNT(*) FROM hazard_evidence WHERE hazard_id=NEW.hazard_id)>=10
                BEGIN SELECT RAISE(ABORT, 'Evidence limit is ten frames per hazard'); END;
            ''')
            for table, columns in (("sessions", {"metadata": "TEXT"}),
                                   ("hazards", {"created_at": "TEXT", "updated_at": "TEXT"})):
                existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, kind in columns.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            db.execute("UPDATE hazards SET created_at=(SELECT created_at FROM sessions WHERE sessions.id=hazards.session_id) WHERE created_at IS NULL")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def start_session(self, session_id, source, config):
        with self.connect() as db:
            db.execute("INSERT INTO sessions(id,source,config,status) VALUES(?,?,?,?)",
                       (session_id, source, json.dumps(config), "running"))

    def finish_session(self, session_id, status):
        with self.connect() as db:
            db.execute("UPDATE sessions SET status=? WHERE id=?", (status, session_id))

    def save_hazard(self, session_id, event):
        with self.connect() as db:
            db.execute("INSERT INTO hazards(id,session_id,payload,created_at,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
                       "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                       (event["id"], session_id, json.dumps(event, allow_nan=False)))

    def hazards(self, session_id=None):
        with self.connect() as db:
            rows = db.execute(self._hazard_query() + (" WHERE h.session_id=?" if session_id else "") + " ORDER BY h.rowid DESC",
                              (session_id,) if session_id else ()).fetchall()
        return [self._hazard_value(row) for row in rows]

    @staticmethod
    def _hazard_query():
        return ("SELECT h.payload,h.created_at,h.updated_at,r.payload,"
                "(SELECT COUNT(*) FROM hazard_evidence e WHERE e.hazard_id=h.id),"
                "(SELECT MIN(id) FROM hazard_evidence e WHERE e.hazard_id=h.id) "
                "FROM hazards h LEFT JOIN hazard_review r ON r.hazard_id=h.id")

    @staticmethod
    def _hazard_value(row):
        value = json.loads(row[0])
        # Keep user-entered details separate from updates by the detector.
        review = json.loads(row[3]) if row[3] else {}
        value.update({key: item for key, item in review.items() if key in REVIEW_FIELDS})
        value.setdefault("status", "open")
        value.setdefault("severity", "medium")
        value["created_at"] = row[1]
        value["updated_at"] = row[2]
        value["evidence_count"] = row[4]
        value["thumbnail_url"] = f"/api/evidence/{row[5]}.jpg" if row[5] else None
        return value

    def hazard(self, hazard_id):
        with self.connect() as db:
            row = db.execute(self._hazard_query() + " WHERE h.id=?", (hazard_id,)).fetchone()
        return self._hazard_value(row) if row else None

    def save_hazard_report(self, hazard_id, details):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(self._hazard_query() + " WHERE h.id=?", (hazard_id,)).fetchone()
            if row is None:
                return None
            review = json.loads(row[3]) if row[3] else {}
            review.update(details)
            review.setdefault("reported_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            db.execute("INSERT INTO hazard_review(hazard_id,payload) VALUES(?,?) "
                       "ON CONFLICT(hazard_id) DO UPDATE SET payload=excluded.payload",
                       (hazard_id, json.dumps(review, allow_nan=False)))
            updated = db.execute(self._hazard_query() + " WHERE h.id=?", (hazard_id,)).fetchone()
        return self._hazard_value(updated)

    def save_evidence(self, hazard_id, jpeg, metadata, max_frames=MAX_EVIDENCE_FRAMES):
        """Persist at most ten JPEG frames per hazard, including concurrent writers."""
        if not isinstance(jpeg, (bytes, bytearray)) or not jpeg or len(jpeg) > 8 * 1024 * 1024:
            raise ValueError("Evidence must contain 1 byte to 8 MiB of JPEG data")
        if not isinstance(metadata, dict):
            raise ValueError("Evidence metadata must be an object")
        if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= MAX_EVIDENCE_FRAMES:
            raise ValueError("Evidence limit must be an integer from 1 to 10")
        payload = json.dumps(metadata, allow_nan=False)
        digest = hashlib.sha256(jpeg).hexdigest()
        frame_index = metadata.get("frame_index")
        time_s = metadata.get("time_s")
        if frame_index is not None:
            if isinstance(frame_index, bool) or not isinstance(frame_index, (int, float)) or not math.isfinite(frame_index) or frame_index < 0 or int(frame_index) != frame_index:
                raise ValueError("Frame index must be a nonnegative integer")
            frame_key = "frame:" + str(int(frame_index))
        elif time_s is not None:
            if isinstance(time_s, bool) or not isinstance(time_s, (int, float)) or not math.isfinite(time_s) or time_s < 0:
                raise ValueError("Evidence time must be finite and nonnegative")
            frame_key = "time:" + str(time_s)
        else:
            frame_key = "sha256:" + digest
        with self.connect() as db:
            # Counting and insertion must share a reserved writer transaction.
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM hazards WHERE id=?", (hazard_id,)).fetchone():
                raise ValueError("Save the hazard before adding evidence")
            saved = db.execute("SELECT id,metadata,created_at,sha256 FROM hazard_evidence WHERE hazard_id=? AND frame_key=?", (hazard_id, frame_key)).fetchone()
            if saved:
                return self._evidence_value(saved, hazard_id)
            count = db.execute("SELECT COUNT(*) FROM hazard_evidence WHERE hazard_id=?", (hazard_id,)).fetchone()[0]
            if count >= max_frames:
                return None
            cursor = db.execute("INSERT INTO hazard_evidence(hazard_id,frame_key,jpeg,metadata,sha256) VALUES(?,?,?,?,?)",
                                (hazard_id, frame_key, sqlite3.Binary(jpeg), payload, digest))
            saved = db.execute("SELECT id,metadata,created_at,sha256 FROM hazard_evidence WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._evidence_value(saved, hazard_id)

    @staticmethod
    def _evidence_value(row, hazard_id):
        return {**json.loads(row[1]), "id": row[0], "hazard_id": hazard_id,
                "url": f"/api/evidence/{row[0]}.jpg", "mime_type": "image/jpeg", "created_at": row[2], "sha256": row[3]}

    def evidence(self, hazard_id):
        with self.connect() as db:
            rows = db.execute("SELECT id,metadata,created_at,sha256 FROM hazard_evidence WHERE hazard_id=? ORDER BY id", (hazard_id,)).fetchall()
        return [self._evidence_value(row, hazard_id) for row in rows]

    def evidence_image(self, evidence_id):
        with self.connect() as db:
            row = db.execute("SELECT id,metadata,created_at,sha256,hazard_id,jpeg FROM hazard_evidence WHERE id=?", (evidence_id,)).fetchone()
        return (bytes(row[5]), self._evidence_value(row[:4], row[4])) if row else None

    @staticmethod
    def _session_value(row):
        metadata = json.loads(row[5] or "{}")
        return {"id": row[0], "created_at": row[1], "source": row[2], "config": json.loads(row[3] or "{}"),
                "status": row[4], **metadata, "metadata": metadata}

    def session(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT id,created_at,source,config,status,metadata FROM sessions WHERE id=?", (session_id,)).fetchone()
        return self._session_value(row) if row else None

    def sessions(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,created_at,source,config,status,metadata FROM sessions ORDER BY created_at DESC,rowid DESC").fetchall()
            counts = dict(db.execute("SELECT session_id,COUNT(*) FROM hazards GROUP BY session_id"))
        return [{**self._session_value(row), "hazard_count": counts.get(row[0], 0)} for row in rows]

    def session_update(self, session_id, **extra):
        if set(extra) & {"id", "config", "created_at", "source", "status"}:
            raise ValueError("Session metadata cannot replace protected session fields")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT metadata FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                return None
            value = json.loads(row[0] or "{}")
            value.update(extra)
            db.execute("UPDATE sessions SET metadata=? WHERE id=?", (json.dumps(value, allow_nan=False), session_id))
        return self.session(session_id)

    def save_references(self, source, records, import_id=None):
        # A row id is only unique inside its survey/sheet. Namespace persisted
        # ids so a second workbook's row "1" cannot erase existing evidence.
        if import_id is None:
            import_id = hashlib.sha256(json.dumps([source, records], sort_keys=True, allow_nan=False).encode()).hexdigest()
        normalized = []
        for record in records:
            item = dict(record)
            item["source_row_id"] = item.get("source_row_id", item.get("id"))
            item["source"] = item.get("source") or source
            item["import_id"] = import_id
            fingerprint = {key: value for key, value in item.items() if key != "id"}
            item["id"] = "import-" + hashlib.sha256(json.dumps(fingerprint, sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]
            normalized.append(item)
        with self.connect() as db:
            payload = json.dumps(normalized, sort_keys=True, allow_nan=False)
            db.execute("INSERT INTO reference_imports(source,payload) SELECT ?,? "
                       "WHERE NOT EXISTS (SELECT 1 FROM reference_imports WHERE source=? AND payload=?)",
                       (source, payload, source, payload))
        return normalized

    def references(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM reference_imports").fetchall()
        unique = {}
        for row in rows:
            for item in json.loads(row[0]):
                unique[(item.get("source"), item.get("id"))] = item
        return list(unique.values())
