#!/usr/bin/env node
// AES-256-GCM-Verschlüsselung der graph.db für das statische Hosting.
// Gegenstück: decryptDb() in web/app.js (PBKDF2-SHA256, 310k Iterationen).
// Dateiformat: [16 B Salt][12 B IV][Ciphertext inkl. GCM-Tag]
//
// Aufruf:  SITE_PASSWORD=… node tools/encrypt.mjs <eingabe> <ausgabe>

import { readFileSync, writeFileSync } from "node:fs";
import { randomBytes, webcrypto } from "node:crypto";

const { subtle } = webcrypto;
const [, , inFile, outFile] = process.argv;
const password = process.env.SITE_PASSWORD;

if (!inFile || !outFile || !password) {
  console.error("Usage: SITE_PASSWORD=… node tools/encrypt.mjs <eingabe> <ausgabe>");
  process.exit(1);
}

const salt = randomBytes(16);
const iv = randomBytes(12);
const material = await subtle.importKey(
  "raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
const key = await subtle.deriveKey(
  { name: "PBKDF2", salt, iterations: 310_000, hash: "SHA-256" },
  material, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);

const plain = readFileSync(inFile);
const ct = new Uint8Array(await subtle.encrypt({ name: "AES-GCM", iv }, key, plain));
writeFileSync(outFile, Buffer.concat([salt, iv, Buffer.from(ct)]));
console.log(`${outFile}: ${(salt.length + iv.length + ct.length).toLocaleString()} Bytes geschrieben.`);
