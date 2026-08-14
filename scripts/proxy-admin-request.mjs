#!/usr/bin/env node
// Send one signed proxy-control request from the explorer's private network.

import crypto from "node:crypto";
import fs from "node:fs";

const [method, path, bodyText = "{}"] = process.argv.slice(2);
if (!method || !path?.startsWith("/v1/proxy/")) {
  throw new Error("usage: proxy-admin-request.mjs METHOD /v1/proxy/PATH [JSON_BODY]");
}
const token = process.env.CATALOGUE_CONTROL_TOKEN;
const controlUrl = process.env.CATALOGUE_CONTROL_URL;
if (!token || !controlUrl) throw new Error("control URL/token are unavailable");

const now = Math.floor(Date.now() / 1000);
const claims = {
  kid: process.env.CATALOGUE_OPERATOR_ASSERTION_KEY_ID || "current",
  sub: "rick@mazenet.org",
  role: "admin",
  aud: "catalogue-control",
  iat: now,
  exp: now + 45,
  auth_time: now,
  nonce: crypto.randomUUID(),
  method,
  path,
};
const raw = Buffer.from(JSON.stringify(claims));
const key = fs.readFileSync(
  process.env.CATALOGUE_OPERATOR_ASSERTION_PRIVATE_KEY_FILE,
  "utf8",
);
const encoded = (value) => Buffer.from(value).toString("base64url");
const response = await fetch(`${controlUrl.replace(/\/$/, "")}${path}`, {
  method,
  headers: {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
    "Idempotency-Key": crypto.randomUUID(),
    "X-Catalogue-Actor": encoded(raw),
    "X-Catalogue-Actor-Signature": encoded(crypto.sign(null, raw, key)),
  },
  body: method === "GET" ? undefined : JSON.stringify(JSON.parse(bodyText)),
});
const text = await response.text();
let body;
try {
  body = JSON.parse(text);
} catch {
  body = { error: text.slice(0, 500) };
}
console.log(JSON.stringify({ status: response.status, body }));
if (!response.ok) process.exitCode = 1;
