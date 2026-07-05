import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tasks


class DislikeMigrationTests(unittest.TestCase):
    def test_legacy_read_later_digest_is_not_migrated_to_disliked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "digests.db"
            con = sqlite3.connect(db_path)
            con.execute("""CREATE TABLE digests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE NOT NULL,
                timestamp TEXT,
                title TEXT,
                cn_title TEXT,
                keywords TEXT,
                journal TEXT,
                source TEXT DEFAULT 'rss',
                preview TEXT,
                created_ts INTEGER NOT NULL,
                read_later INTEGER NOT NULL DEFAULT 0,
                interested INTEGER NOT NULL DEFAULT 0,
                is_read INTEGER NOT NULL DEFAULT 0
            )""")
            con.execute("""INSERT INTO digests(
                filename, title, created_ts, read_later
            ) VALUES('legacy.html', 'Legacy', 1, 1)""")
            con.commit()
            con.close()

            with patch.object(tasks, "DIGEST_DB", db_path):
                migrated = tasks._digest_db()
                row = migrated.execute(
                    "SELECT read_later, disliked FROM digests WHERE filename='legacy.html'"
                ).fetchone()
                migrated.close()

        self.assertEqual(row, (1, 0))

    def test_legacy_read_later_feedback_loses_positive_weight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "admin.db"
            con = sqlite3.connect(db_path)
            con.execute("""CREATE TABLE interest_feedback(
                filename TEXT PRIMARY KEY,
                title TEXT,
                journal TEXT,
                keywords TEXT,
                preview TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                first_interested_ts INTEGER NOT NULL DEFAULT 0,
                counts_toward_trigger INTEGER NOT NULL DEFAULT 0,
                updated_ts INTEGER NOT NULL DEFAULT 1,
                read_later INTEGER NOT NULL DEFAULT 0,
                interested INTEGER NOT NULL DEFAULT 0,
                is_read INTEGER NOT NULL DEFAULT 0,
                pdf_matched INTEGER NOT NULL DEFAULT 0,
                preference_weight REAL NOT NULL DEFAULT 0,
                primary_signal TEXT NOT NULL DEFAULT '',
                first_seen_ts INTEGER NOT NULL DEFAULT 1,
                ever_interested INTEGER NOT NULL DEFAULT 0
            )""")
            con.execute("""INSERT INTO interest_feedback(
                filename, read_later, preference_weight, primary_signal
            ) VALUES('legacy.html', 1, 70, 'read_later')""")
            con.commit()
            con.close()

            with (
                patch.object(tasks, "ADMIN_DB", db_path),
                patch.object(
                    tasks,
                    "_preference_weights",
                    return_value=dict(tasks.DEFAULT_PREFERENCE_WEIGHTS),
                ),
            ):
                migrated = tasks._admin_db()
                row = migrated.execute("""SELECT read_later, disliked,
                    preference_weight, primary_signal
                    FROM interest_feedback WHERE filename='legacy.html'""").fetchone()
                profile = migrated.execute("""SELECT dislike_schema_version,
                    feedback_revision FROM interest_profile WHERE id=1""").fetchone()
                migrated.close()

        self.assertEqual(row, (1, 0, 0.0, ""))
        self.assertEqual(profile, (1, 1))

    def test_dislike_weight_is_negative_and_overrides_implicit_signals(self):
        with patch.object(
            tasks,
            "_preference_weights",
            return_value=dict(tasks.DEFAULT_PREFERENCE_WEIGHTS),
        ):
            weight, signal = tasks._preference_weight(
                pdf_matched=True,
                disliked=True,
                interested=False,
                is_read=True,
            )

        self.assertEqual(signal, "disliked")
        self.assertEqual(weight, -70)

    def test_weight_validation_accepts_new_shape_and_rejects_positive_dislike(self):
        valid = tasks.validate_preference_weights({
            "pdf_matched": 100,
            "interested": 40,
            "is_read": 10,
            "disliked": -80,
        })
        self.assertEqual(valid["disliked"], -80)
        with self.assertRaises(ValueError):
            tasks.validate_preference_weights({
                "pdf_matched": 100,
                "interested": 40,
                "is_read": 10,
                "disliked": 20,
            })


if __name__ == "__main__":
    unittest.main()
