# Cursor-Sandbox

A tiny Node.js web app used to demonstrate a working Cloud Agent development
environment. It serves a small notes UI backed by an in-memory HTTP API.

## Requirements

- Node.js >= 20 (developed against Node 22)

## Getting started

```bash
npm ci        # install dependencies from the lockfile
npm start     # start the server on http://localhost:3000
```

Then open http://localhost:3000 and add a note.

## Development

```bash
npm run dev   # start the server with auto-reload
npm test      # run the test suite (Node's built-in test runner)
npm run lint  # run ESLint
```

## API

| Method | Path          | Description                     |
| ------ | ------------- | ------------------------------- |
| GET    | `/api/health` | Health check with uptime        |
| GET    | `/api/notes`  | List all notes                  |
| POST   | `/api/notes`  | Create a note (`{ "text": … }`) |

## Project layout

```
src/app.js       Express app factory (routes + in-memory store)
src/server.js    HTTP server entry point
public/index.html Frontend UI
test/app.test.js  API tests
```

## Cloud Agent environment

`.cursor/environment.json` configures the Cloud Agent environment:

- `install`: `npm ci`
- `terminals`: runs `npm start` (the web server) on port 3000
