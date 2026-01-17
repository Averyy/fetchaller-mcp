#!/usr/bin/env node
/**
 * Local test for fetchaller-mcp
 * Run: node test-local.mjs
 *
 * Tests PDF support, SSRF protection, and basic fetching.
 * Requires the MCP server to NOT be running (tests functions directly).
 */

import { extractText, getDocumentProxy } from "unpdf";

// ============ SSRF Protection Tests ============

function isPrivateHost(hostname) {
  if (hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]") {
    return true;
  }
  if (hostname.endsWith(".local") || hostname.endsWith(".internal")) {
    return true;
  }
  if (hostname.endsWith(".nip.io") || hostname.endsWith(".xip.io") || hostname.endsWith(".localtest.me") || hostname === "localtest.me") {
    return true;
  }
  const ipv4Match = hostname.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (ipv4Match) {
    const [, a, b] = ipv4Match.map(Number);
    if (a === 10) return true;
    if (a === 172 && b >= 16 && b <= 31) return true;
    if (a === 192 && b === 168) return true;
    if (a === 169 && b === 254) return true;
    if (a === 127) return true;
    if (a === 0) return true;
  }
  const ipv6 = hostname.startsWith("[") && hostname.endsWith("]") ? hostname.slice(1, -1).toLowerCase() : null;
  if (ipv6) {
    const v4MappedMatch = ipv6.match(/^::ffff:(\d+)\.(\d+)\.(\d+)\.(\d+)$/i);
    if (v4MappedMatch) {
      const [, a, b] = v4MappedMatch.map(Number);
      if (a === 10) return true;
      if (a === 172 && b >= 16 && b <= 31) return true;
      if (a === 192 && b === 168) return true;
      if (a === 169 && b === 254) return true;
      if (a === 127) return true;
      if (a === 0) return true;
    }
    if (ipv6 === "::1" || ipv6 === "0:0:0:0:0:0:0:1") return true;
    if (ipv6.startsWith("fe8") || ipv6.startsWith("fe9") || ipv6.startsWith("fea") || ipv6.startsWith("feb")) return true;
    if (ipv6.startsWith("fc") || ipv6.startsWith("fd")) return true;
  }
  return false;
}

console.log("=== SSRF Protection Tests ===\n");

const ssrfTests = [
  // Should block (private)
  { host: "localhost", expected: true },
  { host: "127.0.0.1", expected: true },
  { host: "10.0.0.1", expected: true },
  { host: "172.16.0.1", expected: true },
  { host: "192.168.1.1", expected: true },
  { host: "169.254.169.254", expected: true },
  { host: "[::1]", expected: true },
  { host: "[::ffff:127.0.0.1]", expected: true },
  { host: "[::ffff:169.254.169.254]", expected: true },
  { host: "[fe80::1]", expected: true },
  { host: "[fc00::1]", expected: true },
  { host: "[fd00::1]", expected: true },
  { host: "test.local", expected: true },
  { host: "api.internal", expected: true },
  { host: "127.0.0.1.nip.io", expected: true },
  { host: "192.168.1.1.xip.io", expected: true },
  { host: "localtest.me", expected: true },
  // Should allow (public)
  { host: "google.com", expected: false },
  { host: "github.com", expected: false },
  { host: "8.8.8.8", expected: false },
  { host: "1.1.1.1", expected: false },
  { host: "[2607:f8b0:4004:800::200e]", expected: false }, // Google IPv6
];

let ssrfPassed = 0;
let ssrfFailed = 0;

for (const test of ssrfTests) {
  const result = isPrivateHost(test.host);
  const passed = result === test.expected;
  if (passed) {
    ssrfPassed++;
    console.log(`✓ ${test.host} → ${result ? "BLOCKED" : "ALLOWED"}`);
  } else {
    ssrfFailed++;
    console.log(`✗ ${test.host} → ${result ? "BLOCKED" : "ALLOWED"} (expected ${test.expected ? "BLOCKED" : "ALLOWED"})`);
  }
}

console.log(`\nSSRF Tests: ${ssrfPassed}/${ssrfTests.length} passed\n`);

// ============ PDF Test ============

console.log("=== PDF Extraction Test ===\n");

try {
  // Fetch a real PDF
  const pdfUrl = "https://pdfobject.com/pdf/sample.pdf";
  console.log(`Fetching PDF: ${pdfUrl}`);

  const response = await fetch(pdfUrl, {
    headers: {
      "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      "Accept": "application/pdf",
    },
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const arrayBuffer = await response.arrayBuffer();
  console.log(`Downloaded: ${(arrayBuffer.byteLength / 1024).toFixed(1)} KB`);

  const pdf = await getDocumentProxy(new Uint8Array(arrayBuffer));
  const { totalPages, text } = await extractText(pdf, { mergePages: true });

  console.log(`Pages: ${totalPages}`);
  console.log(`Text length: ${text.length} chars`);
  console.log(`Preview: "${text.slice(0, 200).replace(/\n/g, " ")}..."`);

  await pdf.destroy();
  console.log("\n✓ PDF extraction working\n");
} catch (err) {
  console.log(`\n✗ PDF extraction failed: ${err.message}\n`);
}

// ============ Basic Fetch Test ============

console.log("=== Basic Fetch Test ===\n");

try {
  const testUrl = "https://httpbin.org/json";
  console.log(`Fetching: ${testUrl}`);

  const response = await fetch(testUrl, {
    headers: {
      "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
      "Accept": "application/json",
    },
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const json = await response.json();
  console.log(`Response: ${JSON.stringify(json).slice(0, 100)}...`);
  console.log("\n✓ Basic fetch working\n");
} catch (err) {
  console.log(`\n✗ Basic fetch failed: ${err.message}\n`);
}

// ============ Summary ============

console.log("=== Summary ===\n");
if (ssrfFailed === 0) {
  console.log("✓ All SSRF protection tests passed");
} else {
  console.log(`✗ ${ssrfFailed} SSRF tests failed`);
}
console.log("\nTo test the full MCP server:");
console.log("1. Restart Claude Code to reload the MCP server");
console.log("2. Try: mcp__fetchaller__fetch({ url: 'https://example.com' })");
console.log("3. Try: mcp__fetchaller__fetch({ url: 'https://pdfobject.com/pdf/sample.pdf' })");
