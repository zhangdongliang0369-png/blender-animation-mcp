"""Persistent catalog. SQLite updates index files only after successful writes."""
import base64
import contextlib
import hashlib
import hmac
import json
from pathlib import Path, PurePosixPath
import sqlite3
import time
from uuid import uuid4

MODELS = {".blend", ".blend1", ".fbx", ".glb", ".gltf", ".obj", ".stl", ".step", ".stp"}
DEPENDENCIES = {".png", ".jpg", ".jpeg", ".exr", ".hdr", ".bin", ".mtl"}
MIB = 1024 * 1024


def project_path(value: str) -> str:
    parts = value.split("/")
    if (
        not 1 <= len(parts) <= 8 or len(value) > 220
        or any(not p or p in {".", ".."} or p.startswith(".") for p in parts)
        or any(ch in value for ch in '\\:\x00\n\r')
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ValueError("Invalid asset path")
    path = PurePosixPath(value)
    if path.suffix.lower() not in MODELS | DEPENDENCIES:
        raise ValueError("Unsupported asset extension")
    return path.as_posix()


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Store:
    def __init__(self, root: Path, owner: str, secret: str):
        self.root = root.resolve()
        self.owner = owner
        self.secret = secret.encode()
        (self.root / "projects").mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = self.root / "catalog.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, created REAL NOT NULL,
                    request TEXT NOT NULL, UNIQUE(owner, request));
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
                    hash TEXT NOT NULL, size INTEGER NOT NULL, created REAL NOT NULL,
                    UNIQUE(project, name, hash));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
                    request TEXT NOT NULL, spec TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, started REAL, ended REAL,
                    error_code TEXT, error TEXT, outputs TEXT, version TEXT,
                    UNIQUE(project, request));
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id),
                    task TEXT NOT NULL UNIQUE REFERENCES jobs(id), parent TEXT, created REAL NOT NULL,
                    hash TEXT NOT NULL, blender TEXT NOT NULL);
            """)

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def one(self, query: str, args: tuple):
        with self.connection() as db:
            return db.execute(query, args).fetchone()

    def all(self, query: str, args: tuple):
        with self.connection() as db:
            return [dict(row) for row in db.execute(query, args)]

    def require_project(self, project_id):
        row = self.one("SELECT * FROM projects WHERE id=? AND owner=?", (str(project_id), self.owner))
        if row is None:
            raise ValueError("Project was not found or is not accessible")
        return dict(row)

    def create_project(self, name: str, request_id: str):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM projects WHERE owner=? AND request=?", (self.owner, request_id)).fetchone()
            if row is not None:
                if row["name"] != name:
                    raise ValueError("Request ID already used with different project data")
                return dict(row)
            ident = uuid4().hex
            db.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (ident, self.owner, name, time.time(), request_id))
        return self.require_project(ident)

    def project_dir(self, project: str) -> Path:
        path = self.root / "projects" / project
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def asset(self, project: str, asset_id: str):
        row = self.one("SELECT * FROM assets WHERE project=? AND id=?", (project, asset_id))
        if row is None:
            raise ValueError("Asset was not found in this project")
        return dict(row)

    def asset_file(self, asset) -> Path:
        return self.project_dir(asset["project"]) / "assets" / asset["id"]

    def add_asset(self, project, name, incoming: Path, digest, size):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM assets WHERE project=? AND name=? AND hash=?", (project, name, digest),
            ).fetchone()
            if row is not None:
                return dict(row)
            ident = uuid4().hex
            directory = self.project_dir(project) / "assets"
            directory.mkdir(mode=0o700, exist_ok=True)
            incoming.replace(directory / ident)
            (directory / ident).chmod(0o400)
            db.execute("INSERT INTO assets VALUES (?,?,?,?,?,?)", (ident, project, name, digest, size, time.time()))
        return self.asset(project, ident)

    def version(self, project: str, version_id: str):
        row = self.one("SELECT * FROM versions WHERE project=? AND id=?", (project, version_id))
        if row is None:
            raise ValueError("Version was not found in this project")
        return dict(row)

    def task(self, project: str, task_id: str):
        row = self.one("SELECT * FROM jobs WHERE project=? AND id=?", (project, task_id))
        if row is None:
            raise ValueError("Task was not found in this project")
        return dict(row)

    def job_dir(self, project: str, task_id: str) -> Path:
        return self.project_dir(project) / "jobs" / task_id

    def enqueue(self, project: str, request_id: str, spec: dict):
        encoded = canonical(spec)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM jobs WHERE project=? AND request=?", (project, request_id),
            ).fetchone()
            if existing:
                if existing["spec"] != encoded:
                    raise ValueError("Request ID already used with different task data")
                return dict(existing)
            if db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0] >= 12:
                raise ValueError("Queue is full; retry later")
            ident = uuid4().hex
            db.execute(
                "INSERT INTO jobs(id,project,request,spec,status,created) VALUES (?,?,?,?,'queued',?)",
                (ident, project, request_id, encoded, time.time()),
            )
        return self.task(project, ident)

    def sign(self, purpose: str, fields: dict, seconds: int = 900) -> str:
        data = {"purpose": purpose, "owner": self.owner, "exp": int(time.time()) + seconds, **fields}
        body = base64.urlsafe_b64encode(canonical(data).encode()).decode().rstrip("=")
        signature = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        return body + "." + signature

    def authorize(self, token: str, purpose: str) -> dict:
        if len(token) > 1800:
            raise ValueError("Invalid authorization")
        body, signature = token.split(".", 1)
        expected = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid authorization")
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data["exp"] < time.time() or data["purpose"] != purpose or data["owner"] != self.owner:
            raise ValueError("Expired or invalid authorization")
        self.require_project(data["project"])
        return data
