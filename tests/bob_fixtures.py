"""Synthetic Bob records; no source transcripts or machine-local values."""
import json
import sqlite3


def task(identity="chat-one", *, now=1_800_000_000, parent=None, kind="normal", status="active", costs=None):
    return {"id": identity, "project_id": "project-one", "parent_id": parent,
            "title": "Synthetic chat " + identity, "status": status, "directory": "/workspace/example",
            "version": None, "task_type": kind, "created_at": int(now * 1000),
            "updated_at": int(now * 1000), "costs": json.dumps(costs if costs is not None else
                {"input": 100, "output": 20, "cacheRead": 80, "cacheWrite": 5})}


def message(identity="message-one", *, chat="chat-one", role="assistant", now=1_800_000_000, data=None):
    return {"id": identity, "task_id": chat, "role": role, "created_at": int(now * 1000),
            "data": json.dumps(data if data is not None else {"role": role, "content": "PRIVATE_SENTINEL",
                "_meta": {"timestamp": int(now * 1000), "spend": {"input": 999_999, "output": 999_999}}})}


def database(path, tasks=None, messages=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE tasks (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
        title TEXT, status TEXT, directory TEXT, version TEXT, task_type TEXT,
        created_at INTEGER, updated_at INTEGER, costs TEXT);
        CREATE TABLE messages (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
        role TEXT, created_at INTEGER, data TEXT);
        CREATE INDEX messages_task ON messages(task_id, created_at);''')
    for table, rows in (("tasks", tasks or []), ("messages", messages or [])):
        for row in rows:
            db.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
    db.commit()
    return db
