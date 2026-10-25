Follow-up change request for mechcalc (use with: agentctl extend mechcalc --file outlines/mechcalc-api.md)

Add a small HTTP API on top of the existing library, without changing the library's behaviour:
- New optional extra `api` in pyproject.toml with fastapi and uvicorn; the core package keeps
  zero runtime dependencies.
- Module `mechcalc.api` exposing a FastAPI app with:
  - GET /health -> {"status": "ok"}
  - GET /materials -> the materials table
  - POST /sections/{kind} (rect | circle | tube | ibeam) with the dimensions as JSON -> A, I, c
  - POST /beams with support, load, length, magnitude, material and a section object -> the
    beam result
- Invalid input returns HTTP 422 with a clear message; unknown material returns 404.
- `mechcalc serve --port 8000` starts the API (only when the api extra is installed; otherwise
  print how to install it and exit 2).
- Tests use FastAPI's TestClient and need no network.
