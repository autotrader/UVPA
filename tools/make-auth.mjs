#!/usr/bin/env node
// Erzeugt auth.json für das Frontend-Gate: PBKDF2-SHA256-Hash von SITE_PASSWORD.
// Das Gate ist eine Nutzungshürde, kein Datenschutz — die Dokumente im Repo
// sind amtlich öffentlich. Gegenstück: checkAuth() in web/app.js.
//
// Aufruf:  SITE_PASSWORD=… node tools/make-auth.mjs <auth.json>

import { writeFileSync } from "node:fs";
import { pbkdf2Sync, randomBytes } from "node:crypto";

const [, , outFile] = process.argv;
const password = process.env.SITE_PASSWORD;

if (!outFile || !password) {
  console.error("Usage: SITE_PASSWORD=… node tools/make-auth.mjs <auth.json>");
  process.exit(1);
}

const iterations = 310_000;
const salt = randomBytes(16);
const hash = pbkdf2Sync(password, salt, iterations, 32, "sha256");
writeFileSync(outFile, JSON.stringify({
  salt: salt.toString("hex"),
  iterations,
  hash: hash.toString("hex"),
}));
console.log(`${outFile} geschrieben (PBKDF2-SHA256, ${iterations.toLocaleString("de-DE")} Iterationen).`);
