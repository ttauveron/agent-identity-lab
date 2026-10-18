"""Durable, user-scoped Agent Framework sessions for single-instance services."""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from agent_framework import AgentSession


class SessionStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS sessions (
                subject TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                session_json TEXT NOT NULL,
                PRIMARY KEY (subject, conversation_id)
            )""")
            connection.commit()
        os.chmod(self.path, 0o600)
        return sqlite3.connect(self.path, timeout=30)

    def load(self, subject: str, conversation_id: str) -> AgentSession | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT session_json FROM sessions WHERE subject=? AND conversation_id=?",
                (subject, conversation_id),
            ).fetchone()
        return AgentSession.from_dict(json.loads(row[0])) if row else None

    def save(self, subject: str, conversation_id: str, session: AgentSession) -> None:
        serialized = json.dumps(session.to_dict())
        with closing(self._connect()) as connection, connection:
            connection.execute("""INSERT INTO sessions (subject, conversation_id, session_json)
                VALUES (?, ?, ?)
                ON CONFLICT(subject, conversation_id) DO UPDATE SET session_json=excluded.session_json""",
                (subject, conversation_id, serialized),
            )
