import express from "express";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Build the Express application.
 *
 * Notes are held in memory so the sandbox has a real create-a-record
 * interaction without requiring an external database.
 */
export function createApp() {
  const app = express();
  app.use(express.json());
  app.use(express.static(join(__dirname, "..", "public")));

  /** @type {{ id: number, text: string, createdAt: string }[]} */
  const notes = [];
  let nextId = 1;

  app.get("/api/health", (_req, res) => {
    res.json({ status: "ok", uptime: process.uptime() });
  });

  app.get("/api/notes", (_req, res) => {
    res.json({ notes });
  });

  app.post("/api/notes", (req, res) => {
    const text = typeof req.body?.text === "string" ? req.body.text.trim() : "";
    if (!text) {
      res.status(400).json({ error: "text is required" });
      return;
    }
    const note = { id: nextId++, text, createdAt: new Date().toISOString() };
    notes.push(note);
    res.status(201).json({ note });
  });

  return app;
}
