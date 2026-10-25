# Project: todo-api

A small, well-tested REST API for personal to-do lists, written in Python.

## What it should do
- Users can create, list, update and delete to-do items.
- Each item has: id, title (1-200 chars, required), notes (optional, up to 2000 chars),
  due_date (optional, ISO date), priority (low | normal | high, default normal),
  done (bool, default false), created_at, updated_at.
- List endpoint supports filtering by `done` and `priority`, and sorting by `due_date`
  or `created_at` (ascending/descending), with simple limit/offset pagination.
- Input validation errors return HTTP 422 with a clear JSON error body; unknown ids return 404.
- Data is stored in SQLite (a file path from an environment variable, defaulting to
  `todo.db`); tests use an in-memory or temporary database.
- A `GET /health` endpoint returns `{"status": "ok"}`.
- A command-line entry point starts the server on a configurable port.

## Constraints
- Python 3.12, FastAPI, Pydantic v2, the standard library sqlite3 module (no ORM).
- pytest with FastAPI's TestClient; the full suite must run offline in under a minute.
- README explains how to install, run, and test.

## Out of scope
Authentication, multiple users, a web frontend, Docker images, deployment.
