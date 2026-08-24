#!/usr/bin/env node
/**
 * Minimal Node.js app demonstrating AWS Lambda MicroVMs lifecycle hooks.
 *
 * Main app listens on port 8080 (GET /health, POST /execute).
 * Lifecycle hooks listen on port 9000:
 *   POST /aws/lambda-microvms/runtime/v1/{ready,run,resume,suspend,terminate}
 */

const express = require("express");
const vm = require("vm");

const APP_PORT = 8080;
const HOOKS_PORT = 9000;
const BASE_PATH = "/aws/lambda-microvms/runtime/v1";

const app = express();
app.use(express.json());

app.get("/health", (req, res) => {
  res.json({ status: "healthy" });
});

app.post("/execute", (req, res) => {
  const code = (req.body || {}).code || "";
  if (!code) return res.status(400).json({ error: "No code provided" });

  let stdout = "";
  const sandbox = {
    console: {
      log: (...args) => {
        stdout += args.map(String).join(" ") + "\n";
      },
    },
  };

  try {
    vm.runInNewContext(code, sandbox, { timeout: 5000 });
    res.json({ success: true, output: stdout });
  } catch (err) {
    res.json({ success: false, error: (err && err.stack) || String(err) });
  }
});

app.listen(APP_PORT, "0.0.0.0", () => {
  console.log(`App listening on port ${APP_PORT}`);
});

const hooks = express();
hooks.use(express.json());

hooks.post(`${BASE_PATH}/ready`, (req, res) => {
  console.log("Ready hook called");
  res.status(200).end();
});

hooks.post(`${BASE_PATH}/run`, (req, res) => {
  console.log("Run hook called", req.body || {});
  res.status(200).end();
});

hooks.post(`${BASE_PATH}/resume`, (req, res) => {
  console.log("Resume hook called");
  res.status(200).end();
});

hooks.post(`${BASE_PATH}/suspend`, (req, res) => {
  console.log("Suspend hook called");
  res.status(200).end();
});

hooks.post(`${BASE_PATH}/terminate`, (req, res) => {
  console.log("Terminate hook called");
  res.status(200).end();
});

hooks.listen(HOOKS_PORT, "0.0.0.0", () => {
  console.log(`Lifecycle hooks listening on port ${HOOKS_PORT}`);
});
