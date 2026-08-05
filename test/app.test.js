import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { createApp } from "../src/app.js";

let server;
let baseUrl;

before(async () => {
  const app = createApp();
  await new Promise((resolve) => {
    server = app.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address();
  baseUrl = `http://127.0.0.1:${port}`;
});

after(() => {
  server?.close();
});

test("GET /api/health reports ok", async () => {
  const res = await fetch(`${baseUrl}/api/health`);
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.status, "ok");
});

test("notes start empty", async () => {
  const res = await fetch(`${baseUrl}/api/notes`);
  const body = await res.json();
  assert.deepEqual(body.notes, []);
});

test("POST /api/notes creates a note and it is retrievable", async () => {
  const res = await fetch(`${baseUrl}/api/notes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: "hello sandbox" }),
  });
  assert.equal(res.status, 201);
  const { note } = await res.json();
  assert.equal(note.text, "hello sandbox");
  assert.ok(note.id > 0);

  const listRes = await fetch(`${baseUrl}/api/notes`);
  const { notes } = await listRes.json();
  assert.equal(notes.length, 1);
  assert.equal(notes[0].text, "hello sandbox");
});

test("POST /api/notes rejects empty text", async () => {
  const res = await fetch(`${baseUrl}/api/notes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: "   " }),
  });
  assert.equal(res.status, 400);
});
