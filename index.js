#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import express from "express";
import crypto from "crypto";
import { createRequire } from "module";
import { z } from "zod";
import * as cheerio from "cheerio";
import TurndownService from "turndown";
import { extractText, getDocumentProxy } from "unpdf";

// Import version from package.json to keep in sync
const require = createRequire(import.meta.url);
const { version: VERSION } = require("./package.json");

// CLI args
const args = process.argv.slice(2);
const httpMode = args.includes("--http");

// =============================================================================
// OAuth 2.1 Implementation for Claude.ai Custom Connectors
// Implements: RFC 8414 (Server Metadata), RFC 7591 (DCR), OAuth 2.1 with PKCE
// =============================================================================

// Constants
const AUTH_CODE_TTL = 10 * 60 * 1000;      // 10 minutes
const ACCESS_TOKEN_TTL = 24 * 60 * 60 * 1000; // 24 hours
const CLIENT_TTL = 30 * 24 * 60 * 60 * 1000;  // 30 days

// Max entry limits to prevent unbounded memory growth
const MAX_OAUTH_CLIENTS = 1000;
const MAX_AUTH_CODES = 5000;
const MAX_ACCESS_TOKENS = 10000;

// OAuth stores are created inside startHttpServer() to avoid allocation in stdio mode
// These are set by createOAuthStores() when HTTP mode is started
let oauthClients = null;  // client_id -> { client_secret, redirect_uris, created_at }
let authCodes = null;     // code -> { client_id, code_challenge, redirect_uri, api_key_hash, expires_at }
let accessTokens = null;  // token -> { client_id, api_key_hash, expires_at }

// Create OAuth stores (called only in HTTP mode)
function createOAuthStores() {
  oauthClients = new Map();
  authCodes = new Map();
  accessTokens = new Map();
}

// Cleanup old entries periodically (returns cleanup interval ID for shutdown)
function startOAuthCleanup() {
  function cleanupOAuthStores() {
    if (!oauthClients) return; // Not in HTTP mode
    const now = Date.now();
    for (const [code, data] of authCodes) {
      if (data.expires_at < now) authCodes.delete(code);
    }
    for (const [token, data] of accessTokens) {
      if (data.expires_at < now) accessTokens.delete(token);
    }
    for (const [clientId, data] of oauthClients) {
      if (data.created_at + CLIENT_TTL < now) oauthClients.delete(clientId);
    }
  }
  return setInterval(cleanupOAuthStores, 60000);
}

// Hash API key for storage (avoid storing raw key in memory)
function hashApiKey(apiKey) {
  return crypto.createHash("sha256").update(apiKey).digest("hex");
}

// Generate secure random strings
function generateId(length = 32) {
  return crypto.randomBytes(length).toString("base64url");
}

// HTML entity encoding to prevent XSS
function escapeHtml(str) {
  return String(str || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

// Sanitize values for logging (remove control characters and newlines)
function sanitizeForLog(str) {
  return String(str || "").replace(/[\x00-\x1f\x7f]/g, "").slice(0, 100);
}

// PKCE: Verify code_challenge against code_verifier (S256 only)
// Uses timing-safe comparison to prevent timing attacks
function verifyPKCE(codeVerifier, codeChallenge) {
  const hash = crypto.createHash("sha256").update(codeVerifier).digest("base64url");
  // Use timing-safe comparison (both are base64url strings of same length for SHA256)
  const hashBuffer = Buffer.from(hash);
  const challengeBuffer = Buffer.from(codeChallenge || "");
  if (hashBuffer.length !== challengeBuffer.length) {
    return false;
  }
  return crypto.timingSafeEqual(hashBuffer, challengeBuffer);
}

// Simple JWT-like token (signed with server secret)
// Stores hash of API key instead of raw key for security
function createAccessToken(clientId, apiKeyHash, serverSecret) {
  const payload = {
    sub: clientId,
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor((Date.now() + ACCESS_TOKEN_TTL) / 1000),
  };
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const signature = crypto.createHmac("sha256", serverSecret).update(`${header}.${body}`).digest("base64url");
  const token = `${header}.${body}.${signature}`;

  // Enforce max entries to prevent memory exhaustion
  if (accessTokens.size >= MAX_ACCESS_TOKENS) {
    // Remove oldest expired entries first
    const now = Date.now();
    for (const [t, data] of accessTokens) {
      if (data.expires_at < now) accessTokens.delete(t);
    }
    // If still at limit, remove oldest entry
    if (accessTokens.size >= MAX_ACCESS_TOKENS) {
      const oldestKey = accessTokens.keys().next().value;
      accessTokens.delete(oldestKey);
    }
  }

  // Store the token with API key hash (not raw key)
  accessTokens.set(token, {
    client_id: clientId,
    api_key_hash: apiKeyHash,
    expires_at: Date.now() + ACCESS_TOKEN_TTL,
  });

  return token;
}

// Verify JWT token and return the stored API key hash
function verifyAccessToken(token, serverSecret) {
  if (!accessTokens) return null; // Not in HTTP mode

  // Check if in our store first
  const stored = accessTokens.get(token);
  if (!stored) return null;
  if (stored.expires_at < Date.now()) {
    accessTokens.delete(token);
    return null;
  }

  // Verify signature
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [header, body, signature] = parts;
  const expectedSig = crypto.createHmac("sha256", serverSecret).update(`${header}.${body}`).digest("base64url");
  if (signature !== expectedSig) return null;

  return stored.api_key_hash;
}

const DEFAULT_MAX_TOKENS = 25000;
const DEFAULT_TIMEOUT_SECONDS = 10;
const CHARS_PER_TOKEN = 4;
const MAX_PDF_SIZE = 50 * 1024 * 1024; // 50MB
const PDF_PROCESSING_TIMEOUT_MS = 30000; // 30s timeout for PDF parsing

// Pre-compiled Zod schemas (reused across requests to reduce GC pressure)
const fetchSchema = {
  url: z.string().describe("The URL to fetch"),
  maxTokens: z.number().optional().describe("Maximum tokens to return (default: 25000)"),
  timeout: z.number().optional().describe("Request timeout in seconds (default: 10)"),
};

const browseRedditSchema = {
  subreddit: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$/, "Invalid subreddit name").describe("Subreddit name without r/ prefix"),
  sort: z.enum(["hot", "new", "top", "rising"]).default("hot").describe("Sort order"),
  time: z.enum(["hour", "day", "week", "month", "year", "all"]).default("day")
    .describe("Time filter (only applies to 'top' sort)"),
  limit: z.number().min(1).max(25).default(10).describe("Number of posts (1-25)"),
  after: z.string().optional().describe("Pagination cursor from previous response"),
  timeout: z.number().optional().describe("Request timeout in seconds (default: 10)"),
};

const searchRedditSchema = {
  query: z.string().describe("Search query"),
  subreddit: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_]{0,20}$/, "Invalid subreddit name").optional().describe("Limit to subreddit (without r/)"),
  sort: z.enum(["relevance", "hot", "top", "new", "comments"]).default("relevance").describe("Sort order"),
  time: z.enum(["hour", "day", "week", "month", "year", "all"]).default("all").describe("Time filter"),
  limit: z.number().min(1).max(25).default(10).describe("Number of results (1-25)"),
  after: z.string().optional().describe("Pagination cursor from previous response"),
  timeout: z.number().optional().describe("Request timeout in seconds (default: 10)"),
};

// SSRF protection: block private/internal IP ranges
function isPrivateHost(hostname) {
  // Block localhost variants (including bracketed IPv6)
  if (hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1" || hostname === "[::1]") {
    return true;
  }
  // Block common internal hostnames
  if (hostname.endsWith(".local") || hostname.endsWith(".internal")) {
    return true;
  }
  // Block DNS rebinding services
  if (hostname.endsWith(".nip.io") || hostname.endsWith(".xip.io") || hostname.endsWith(".localtest.me") || hostname === "localtest.me") {
    return true;
  }
  // Check for private IPv4 ranges
  const ipv4Match = hostname.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (ipv4Match) {
    const [, a, b] = ipv4Match.map(Number);
    if (a === 10) return true; // 10.0.0.0/8
    if (a === 172 && b >= 16 && b <= 31) return true; // 172.16.0.0/12
    if (a === 192 && b === 168) return true; // 192.168.0.0/16
    if (a === 169 && b === 254) return true; // 169.254.0.0/16 (link-local, cloud metadata)
    if (a === 127) return true; // 127.0.0.0/8
    if (a === 0) return true; // 0.0.0.0/8
  }
  // Check for IPv6 private ranges (bracketed format from URL parsing)
  const ipv6 = hostname.startsWith("[") && hostname.endsWith("]") ? hostname.slice(1, -1).toLowerCase() : null;
  if (ipv6) {
    // IPv4-mapped IPv6 (::ffff:127.0.0.1)
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
    // Loopback (::1 and variants)
    if (ipv6 === "::1" || ipv6 === "0:0:0:0:0:0:0:1") return true;
    // Link-local (fe80::/10)
    if (ipv6.startsWith("fe8") || ipv6.startsWith("fe9") || ipv6.startsWith("fea") || ipv6.startsWith("feb")) return true;
    // Unique local addresses (fc00::/7 = fc00:: and fd00::)
    if (ipv6.startsWith("fc") || ipv6.startsWith("fd")) return true;
  }
  return false;
}

// Reddit URL handling: use old.reddit.com HTML (65-70% more compact than JSON/new Reddit)
function transformRedditUrl(url) {
  try {
    const parsed = new URL(url);
    if (!parsed.hostname.includes("reddit.com")) {
      return { url, isReddit: false };
    }

    // Already a JSON URL - leave it alone (user explicitly requested JSON)
    if (parsed.pathname.endsWith(".json")) {
      return { url, isReddit: true };
    }

    // Transform www.reddit.com or reddit.com → old.reddit.com
    // old.reddit.com HTML converts to ~65-70% smaller markdown than JSON or new Reddit
    if (parsed.hostname === "www.reddit.com" || parsed.hostname === "reddit.com") {
      parsed.hostname = "old.reddit.com";
    }

    // Add trailing slash to avoid 301 redirect (saves ~50-100ms latency)
    // Skip paths that already have slash or have extensions like .json
    if (!parsed.pathname.endsWith("/") && !parsed.pathname.includes(".")) {
      parsed.pathname += "/";
    }

    return { url: parsed.toString(), isReddit: true };
  } catch {
    return { url, isReddit: false };
  }
}

const turndown = new TurndownService({
  headingStyle: "atx",
  codeBlockStyle: "fenced",
});

async function fetchWithRetry(url, options, maxRetries = 1) {
  let lastError;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      const response = await fetch(url, options);
      // Retry on 5xx server errors (except on last attempt)
      if (response.status >= 500 && attempt < maxRetries) {
        lastError = new Error(`HTTP ${response.status}`);
        continue;
      }
      return response;
    } catch (err) {
      lastError = err;
      if (attempt === maxRetries) throw err;
    }
  }
  throw lastError;
}

async function processPdfContent(response, maxTokens) {
  // Check size limit before downloading
  const contentLength = parseInt(response.headers.get("content-length") || "0", 10);
  if (contentLength > MAX_PDF_SIZE) {
    return { error: `PDF too large: ${(contentLength / 1024 / 1024).toFixed(1)}MB (max 50MB)` };
  }

  let pdf;
  let timeoutId;
  try {
    const arrayBuffer = await response.arrayBuffer();
    // Double-check actual size (Content-Length may be missing or wrong)
    if (arrayBuffer.byteLength > MAX_PDF_SIZE) {
      return { error: `PDF too large: ${(arrayBuffer.byteLength / 1024 / 1024).toFixed(1)}MB (max 50MB)` };
    }

    // Wrap PDF parsing in timeout to prevent hangs on complex PDFs
    const pdfParsePromise = (async () => {
      pdf = await getDocumentProxy(new Uint8Array(arrayBuffer));
      return extractText(pdf, { mergePages: true });
    })();

    const timeoutPromise = new Promise((_, reject) => {
      timeoutId = setTimeout(() => reject(new Error("PDF_TIMEOUT")), PDF_PROCESSING_TIMEOUT_MS);
    });

    const { totalPages = 0, text = "" } = await Promise.race([pdfParsePromise, timeoutPromise]);

    // Handle empty/scanned PDFs (use regex to avoid creating trimmed copy)
    if (!text || /^\s*$/.test(text)) {
      return {
        content: `[PDF: ${totalPages} pages. No extractable text found - this may be a scanned document or image-based PDF.]`,
        contentType: "pdf",
      };
    }

    const header = `[PDF: ${totalPages} pages. Text extraction is approximate - complex layouts, tables, and formatting may not be preserved.]\n\n`;
    // Account for header and potential truncation suffix in token budget
    // Truncation suffix is ~"\n\n[Truncated at ~XXXXX tokens]" = ~35 chars = ~9 tokens
    const reservedTokens = Math.ceil(header.length / CHARS_PER_TOKEN) + 10;
    const availableTokens = Math.max(maxTokens - reservedTokens, 100);
    return { content: header + truncate(text, availableTokens), contentType: "pdf" };
  } catch (pdfErr) {
    // Detect timeout
    if (pdfErr.message === "PDF_TIMEOUT") {
      return { error: `PDF parsing timed out after ${PDF_PROCESSING_TIMEOUT_MS / 1000}s. The PDF may be too complex or large to process.` };
    }
    // Detect password-protected PDFs
    if (pdfErr.name === "PasswordException" || pdfErr.message?.includes("password")) {
      return { error: "PDF is password-protected and cannot be read." };
    }
    // Generic error without leaking internals
    return { error: "PDF parsing failed. The file may be corrupted, invalid, or use unsupported features. Try opening it in a browser to verify it's accessible." };
  } finally {
    // Clear timeout to prevent timer leak
    if (timeoutId) clearTimeout(timeoutId);
    // Wrap in try-catch to handle potential sync throws
    try {
      if (pdf?.destroy) {
        await pdf.destroy();
      }
    } catch {
      // Ignore cleanup errors
    }
  }
}

async function fetchUrlContent(url, maxTokens = DEFAULT_MAX_TOKENS, timeoutSeconds = DEFAULT_TIMEOUT_SECONDS) {
  // Validate URL
  let parsedUrl;
  try {
    parsedUrl = new URL(url);
    if (!["http:", "https:"].includes(parsedUrl.protocol)) {
      return { error: `Invalid protocol: ${parsedUrl.protocol}. Only http/https supported.` };
    }
  } catch {
    return { error: `Invalid URL format. Expected http:// or https:// URL.` };
  }

  // SSRF protection: block private/internal addresses
  if (isPrivateHost(parsedUrl.hostname)) {
    return { error: `Access to private/internal hosts is not allowed.` };
  }

  // Fetch with timeout
  const controller = new AbortController();
  const timeoutMs = timeoutSeconds * 1000;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchWithRetry(url, {
      signal: controller.signal,
      headers: {
        // Chrome 131 on macOS - headers ordered to match real Chrome
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      },
    });

    clearTimeout(timeout);

    // SSRF protection: check final URL after redirects
    if (response.url && response.url !== url) {
      try {
        const finalUrl = new URL(response.url);
        if (isPrivateHost(finalUrl.hostname)) {
          return { error: `Redirect to private/internal host is not allowed.` };
        }
      } catch {
        // If we can't parse the final URL, proceed cautiously
      }
    }

    const contentType = response.headers.get("content-type") || "";
    const status = response.status;

    // Handle 429 rate limiting with helpful message
    if (status === 429) {
      const retryAfter = response.headers.get("retry-after");
      const retryMsg = retryAfter ? ` Retry after ${retryAfter} seconds.` : "";
      return { error: `Rate limited (HTTP 429).${retryMsg}` };
    }

    // Handle non-200 responses
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      return {
        error: `HTTP ${status}`,
        body: body.slice(0, 1000),
      };
    }

    // Handle non-HTML content
    if (contentType.includes("application/json")) {
      const text = await response.text();
      return { content: truncate(text, maxTokens), contentType: "json" };
    }

    if (contentType.includes("text/plain")) {
      const text = await response.text();
      return { content: truncate(text, maxTokens), contentType: "text" };
    }

    if (contentType.includes("text/xml") || contentType.includes("application/xml") || contentType.includes("application/rss+xml") || contentType.includes("application/atom+xml")) {
      const text = await response.text();
      return { content: truncate(text, maxTokens), contentType: "xml" };
    }

    if (contentType.includes("text/csv")) {
      const text = await response.text();
      return { content: truncate(text, maxTokens), contentType: "csv" };
    }

    if (contentType.includes("application/pdf")) {
      return processPdfContent(response, maxTokens);
    }

    if (!contentType.includes("text/html") && !contentType.includes("application/xhtml")) {
      return { error: `Unsupported content type: ${contentType}` };
    }

    // Process HTML
    const html = await response.text();
    const $ = cheerio.load(html);

    // Remove junk elements
    $("script, style, nav, footer, iframe, noscript, svg, [role='navigation'], [role='banner'], [role='contentinfo'], .nav, .navbar, .footer, .sidebar, .ads, .advertisement").remove();

    // Reddit-specific cleanup (old.reddit.com sidebar, search UI, etc.)
    $(".side, .footer-parent, .listing-chooser, .search-page, .searchpane, .infobar, .premium-banner-outer, .morelink, .titlebox, .login-form-side, .promotedlink, .organic-listing").remove();

    // Get title
    const title = $("title").text().trim();

    // Convert to markdown
    const body = $("body").html() || $.html();
    let markdown = turndown.turndown(body);

    // Clean up excessive whitespace
    markdown = markdown.replace(/\n{3,}/g, "\n\n").trim();

    // Add title if present
    if (title) {
      markdown = `# ${title}\n\n${markdown}`;
    }

    return {
      content: truncate(markdown, maxTokens),
      contentType: "markdown",
      url: response.url, // Include final URL in case of redirects
    };

  } catch (err) {
    clearTimeout(timeout);
    if (err.name === "AbortError") {
      return { error: `Request timed out after ${timeoutSeconds}s. Try increasing the timeout parameter for slow servers.` };
    }
    if (err.code === "ENOTFOUND" || err.message?.includes("ENOTFOUND")) {
      return { error: `Host not found. Check the URL for typos or verify the site is accessible.` };
    }
    if (err.code === "ECONNREFUSED" || err.message?.includes("ECONNREFUSED")) {
      return { error: `Connection refused. The server may be down or blocking requests.` };
    }
    return { error: `Fetch failed: ${err.message}` };
  }
}

function truncate(text, maxTokens) {
  const maxChars = maxTokens * CHARS_PER_TOKEN;
  if (text.length <= maxChars) {
    return text;
  }
  return text.slice(0, maxChars) + `\n\n[Truncated at ~${maxTokens} tokens]`;
}

// Reddit JSON API helpers
async function fetchRedditJson(url, timeoutSeconds = DEFAULT_TIMEOUT_SECONDS) {
  const controller = new AbortController();
  const timeoutMs = timeoutSeconds * 1000;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchWithRetry(url, {
      signal: controller.signal,
      headers: {
        // Chrome 131 on macOS for JSON API requests
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
      },
    });

    clearTimeout(timeout);

    if (response.status === 429) {
      const retryAfter = response.headers.get("retry-after") || "60";
      return { error: `Rate limited. Reddit allows ~10 requests/min. Retry after ${retryAfter}s.` };
    }

    if (!response.ok) {
      return { error: `HTTP ${response.status}` };
    }

    const data = await response.json();
    return { data };
  } catch (err) {
    clearTimeout(timeout);
    if (err.name === "AbortError") {
      return { error: `Request timed out (${timeoutSeconds}s limit)` };
    }
    return { error: `Fetch failed: ${err.message}` };
  }
}

function formatRelativeTime(utcSeconds) {
  const now = Math.floor(Date.now() / 1000);
  const diff = now - utcSeconds;

  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} minutes ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hours ago`;
  if (diff < 2592000) return `${Math.floor(diff / 86400)} days ago`;
  if (diff < 31536000) return `${Math.floor(diff / 2592000)} months ago`;
  return `${Math.floor(diff / 31536000)} years ago`;
}

function formatRedditPost(post, index, includeSubreddit = false) {
  const { title, score, num_comments, author, created_utc, permalink, selftext, subreddit } = post.data;

  const url = `https://old.reddit.com${permalink}`;
  const preview = selftext ? selftext.slice(0, 200).replace(/\n/g, " ").trim() : "";
  const previewLine = preview ? `\n   > "${preview}${selftext.length > 200 ? "..." : ""}"` : "";
  const subLine = includeSubreddit ? `r/${subreddit} · ` : "";

  return `${index}. ${title}
   ${subLine}▲ ${score.toLocaleString()} · 💬 ${num_comments} · u/${author} · ${formatRelativeTime(created_utc)}
   ${url}${previewLine}`;
}

// Factory function to create MCP server with all tools
function createServer() {
  const server = new McpServer({
    name: "fetchaller",
    version: VERSION,
  });

  // Register the fetch tool (using pre-compiled schema)
  server.tool(
    "fetch",
    "Fetch any URL and return the page content as clean markdown. Handles HTML, JSON, XML, CSV, and PDF files. Use this tool for reading/fetching web pages - it has no domain restrictions. For discovering URLs via search, use WebSearch. For reading URL content, use this tool.",
    fetchSchema,
    async ({ url, maxTokens, timeout }) => {
      // Transform Reddit URLs (use old.reddit.com for better token efficiency)
      const { url: fetchUrl, isReddit } = transformRedditUrl(url);
      const result = await fetchUrlContent(fetchUrl, maxTokens, timeout);

      if (result.error) {
        return {
          isError: true,
          content: [
            {
              type: "text",
              text: result.body
                ? `Error: ${result.error}\n\nPartial content:\n${result.body}`
                : `Error: ${result.error}`,
            },
          ],
        };
      }

      let text = result.content;

      // Note if we transformed the URL
      if (isReddit && fetchUrl !== url) {
        text = `[Fetched via: ${fetchUrl}]\n\n${text}`;
      } else if (result.url && result.url !== fetchUrl) {
        text = `[Redirected to: ${result.url}]\n\n${text}`;
      }

      return {
        content: [{ type: "text", text }],
      };
    }
  );

  // Browse subreddit listings (using pre-compiled schema)
  server.tool(
    "browse_reddit",
    "Browse a subreddit's posts. Returns metadata and URLs. Use mcp__fetchaller__fetch to read full post content.",
    browseRedditSchema,
    async ({ subreddit, sort, time, limit, after, timeout }) => {
      // Build URL
      const params = new URLSearchParams();
      if (sort === "top") params.set("t", time);
      params.set("limit", String(limit));
      if (after) params.set("after", after);

      const url = `https://www.reddit.com/r/${subreddit}/${sort}.json?${params}`;
      const result = await fetchRedditJson(url, timeout);

      if (result.error) {
        return {
          isError: true,
          content: [{ type: "text", text: `Error: ${result.error}` }],
        };
      }

      const posts = result.data?.data?.children || [];
      const afterCursor = result.data?.data?.after;

      if (posts.length === 0) {
        return {
          content: [{
            type: "text",
            text: `r/${subreddit} · ${sort} · No posts found`,
          }],
        };
      }

      // Format output
      const lines = [`r/${subreddit} · ${sort} · ${posts.length} posts\n`];

      posts.forEach((post, i) => {
        lines.push(formatRedditPost(post, i + 1, false));
      });

      if (afterCursor) {
        lines.push(`\n[Next page: after=${afterCursor}]`);
      }

      lines.push(`\n---\nTo read full post: mcp__fetchaller__fetch({ url: "https://old.reddit.com/r/${subreddit}/comments/..." })`);

      return {
        content: [{ type: "text", text: lines.join("\n") }],
      };
    }
  );

  // Search Reddit posts (using pre-compiled schema)
  server.tool(
    "search_reddit",
    "Search Reddit posts. Returns metadata and URLs. Use mcp__fetchaller__fetch to read full post content.",
    searchRedditSchema,
    async ({ query, subreddit, sort, time, limit, after, timeout }) => {
      // Build URL
      const params = new URLSearchParams();
      params.set("q", query);
      params.set("sort", sort);
      params.set("t", time);
      params.set("limit", String(limit));
      if (after) params.set("after", after);

      let url;
      if (subreddit) {
        params.set("restrict_sr", "1");
        url = `https://www.reddit.com/r/${subreddit}/search.json?${params}`;
      } else {
        url = `https://www.reddit.com/search.json?${params}`;
      }

      const result = await fetchRedditJson(url, timeout);

      if (result.error) {
        return {
          isError: true,
          content: [{ type: "text", text: `Error: ${result.error}` }],
        };
      }

      const posts = result.data?.data?.children || [];
      const afterCursor = result.data?.data?.after;

      if (posts.length === 0) {
        return {
          content: [{
            type: "text",
            text: `Search: "${query}" · ${sort} · ${time} · No results found`,
          }],
        };
      }

      // Format output
      const subNote = subreddit ? ` in r/${subreddit}` : "";
      const lines = [`Search: "${query}"${subNote} · ${sort} · ${time} · ${posts.length} results\n`];

      posts.forEach((post, i) => {
        lines.push(formatRedditPost(post, i + 1, !subreddit));
      });

      if (afterCursor) {
        lines.push(`\n[Next page: after=${afterCursor}]`);
      }

      lines.push(`\n---\nTo read full post: mcp__fetchaller__fetch({ url: "https://old.reddit.com/r/.../comments/..." })`);

      return {
        content: [{ type: "text", text: lines.join("\n") }],
      };
    }
  );

  return server;
}

// =============================================================================
// Authorization Page HTML (fetchaller branding)
// =============================================================================
function getAuthorizePage(clientId, redirectUri, state, codeChallenge, error = null) {
  // Escape all user-controlled values to prevent XSS
  const safeClientId = escapeHtml(clientId);
  const safeRedirectUri = escapeHtml(redirectUri);
  const safeState = escapeHtml(state);
  const safeCodeChallenge = escapeHtml(codeChallenge);
  const safeError = escapeHtml(error);
  const errorHtml = error ? `<div class="error">${safeError}</div>` : "";
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Authorize | fetchaller</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: #000000;
      --bg-secondary: #0a0a0a;
      --bg-card: rgba(255, 255, 255, 0.03);
      --border-color: rgba(255, 255, 255, 0.08);
      --border-color-hover: rgba(255, 255, 255, 0.15);
      --text-primary: #f5f5f7;
      --text-secondary: #a1a1a6;
      --text-tertiary: #6e6e73;
      --accent: #ff2300;
      --accent-hover: #cc1c00;
      --purple: #62007f;
      --red: #ff453a;
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: 'Space Grotesk', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg-primary);
      color: var(--text-primary);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      -webkit-font-smoothing: antialiased;
    }
    .gradient-bg {
      position: fixed;
      top: 0; left: 0; right: 0;
      height: 100vh;
      background:
        radial-gradient(ellipse 60% 40% at 25% -10%, rgba(98, 0, 127, 0.15), transparent),
        radial-gradient(ellipse 50% 35% at 75% 20%, rgba(255, 35, 0, 0.12), transparent);
      pointer-events: none;
      z-index: -1;
    }
    .card {
      width: 100%;
      max-width: 400px;
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 16px;
      padding: 32px;
    }
    .logo {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 12px;
      margin-bottom: 24px;
    }
    .logo-icon {
      width: 40px;
      height: 40px;
      border-radius: 10px;
      overflow: hidden;
    }
    .logo-icon svg {
      width: 100%;
      height: 100%;
    }
    .logo-text {
      font-family: 'JetBrains Mono', monospace;
      font-size: 1.5rem;
      font-weight: 600;
    }
    h1 {
      font-family: 'JetBrains Mono', monospace;
      font-size: 1.25rem;
      font-weight: 600;
      text-align: center;
      margin-bottom: 8px;
    }
    .subtitle {
      color: var(--text-secondary);
      text-align: center;
      font-size: 0.9375rem;
      margin-bottom: 24px;
      line-height: 1.5;
    }
    .form-group { margin-bottom: 20px; }
    label {
      display: block;
      font-size: 0.875rem;
      font-weight: 500;
      margin-bottom: 8px;
      color: var(--text-secondary);
    }
    input[type="password"], input[type="text"] {
      width: 100%;
      padding: 14px 16px;
      background: var(--bg-secondary);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      color: var(--text-primary);
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.9375rem;
      transition: border-color 0.2s;
    }
    input:focus {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
      border-color: var(--accent);
    }
    input::placeholder {
      color: var(--text-tertiary);
    }
    .btn {
      width: 100%;
      padding: 14px 24px;
      background: var(--accent);
      border: none;
      border-radius: 10px;
      color: white;
      font-family: 'Space Grotesk', sans-serif;
      font-size: 1rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s;
    }
    .btn:hover {
      background: var(--accent-hover);
      transform: translateY(-1px);
    }
    .btn:focus {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    .btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
      transform: none;
    }
    .error {
      background: rgba(255, 69, 58, 0.1);
      border: 1px solid rgba(255, 69, 58, 0.3);
      color: var(--red);
      padding: 12px 16px;
      border-radius: 10px;
      font-size: 0.875rem;
      margin-bottom: 20px;
      text-align: center;
    }
    .info {
      margin-top: 20px;
      padding: 16px;
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border-color);
      border-radius: 10px;
    }
    .info-title {
      font-size: 0.8125rem;
      font-weight: 500;
      color: var(--text-secondary);
      margin-bottom: 8px;
    }
    .info-text {
      font-size: 0.8125rem;
      color: var(--text-tertiary);
      line-height: 1.5;
    }
    .info-text code {
      background: var(--bg-secondary);
      padding: 2px 6px;
      border-radius: 4px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.75rem;
    }
    .client-info {
      font-size: 0.75rem;
      color: var(--text-tertiary);
      text-align: center;
      margin-top: 16px;
    }
  </style>
</head>
<body>
  <div class="gradient-bg"></div>
  <div class="card">
    <div class="logo">
      <div class="logo-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><linearGradient id="lg" x1="33.29" y1="363.29" x2="478.71" y2="808.71" gradientTransform="translate(0 -330)" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#62007f"/><stop offset="1" stop-color="#ff2300"/></linearGradient></defs><rect fill="url(#lg)" width="512" height="512" rx="113.66" ry="113.66"/><path fill="#fff" d="M206.94,428.01v-32.65h-.01v-178.34h-57.99v-32.65h57.99v-13.64c0-30.53,7.96-52.63,23.88-66.27,15.91-13.64,38.65-20.47,68.22-20.47,10.72,0,20.3.66,28.75,1.95,8.44,1.3,17.54,3.9,27.29,7.8l-8.73,31.67c-8.41-3.57-16.5-5.92-24.26-7.07-7.77-1.13-14.88-1.7-21.34-1.7-21.35,0-35.91,5.28-43.67,15.84-7.77,10.56-11.64,27.86-11.64,51.9h100.87v32.65h-103.87l3,178.34h100.87v32.65h-139.35Z"/></svg></div>
      <span class="logo-text">fetchaller</span>
    </div>
    <h1>Connect to fetchaller</h1>
    <p class="subtitle">Enter your API key to authorize Claude to use fetchaller on your behalf.</p>
    ${errorHtml}
    <form method="POST" action="/authorize" id="authForm">
      <input type="hidden" name="client_id" value="${safeClientId}">
      <input type="hidden" name="redirect_uri" value="${safeRedirectUri}">
      <input type="hidden" name="state" value="${safeState}">
      <input type="hidden" name="code_challenge" value="${safeCodeChallenge}">
      <div class="form-group">
        <label for="api_key">API Key</label>
        <input type="password" id="api_key" name="api_key" placeholder="Enter your MCP_API_KEY" required autocomplete="off">
      </div>
      <button type="submit" class="btn" id="submitBtn">Authorize</button>
    </form>
    <script>
      document.getElementById('authForm').addEventListener('submit', function() {
        var btn = document.getElementById('submitBtn');
        btn.disabled = true;
        btn.textContent = 'Authorizing...';
      });
    </script>
    <div class="info">
      <div class="info-title">What is this?</div>
      <div class="info-text">
        This authorizes Claude to fetch web pages through your fetchaller server.
        Your API key is the <code>MCP_API_KEY</code> you set when deploying the server.
      </div>
    </div>
    <div class="client-info">
      Requesting access: Claude
    </div>
  </div>
</body>
</html>`;
}

// HTTP server for remote deployment
async function startHttpServer() {
  const port = parseInt(process.env.HTTP_PORT || "6000", 10);
  const apiKey = process.env.MCP_API_KEY;
  const rateLimit = parseInt(process.env.RATE_LIMIT_REQUESTS || "100", 10);
  const serverUrl = process.env.MCP_SERVER_URL || `http://localhost:${port}`;

  // Validate environment variables
  if (isNaN(port) || port < 1 || port > 65535) {
    console.error("Invalid HTTP_PORT. Must be 1-65535.");
    process.exit(1);
  }
  if (isNaN(rateLimit) || rateLimit < 1) {
    console.error("Invalid RATE_LIMIT_REQUESTS. Must be a positive integer.");
    process.exit(1);
  }

  // Initialize OAuth stores (only allocated in HTTP mode)
  createOAuthStores();

  // Server secret for signing JWT tokens
  // Use separate JWT_SECRET if provided, otherwise derive from API key (with warning)
  let serverSecret;
  if (process.env.JWT_SECRET) {
    serverSecret = crypto.createHash("sha256").update(process.env.JWT_SECRET).digest();
  } else if (apiKey) {
    console.error(`[${new Date().toISOString()}] WARNING: JWT_SECRET not set. Deriving from MCP_API_KEY. Set JWT_SECRET for better security.`);
    serverSecret = crypto.createHash("sha256").update(apiKey + "fetchaller-oauth-secret").digest();
  } else {
    serverSecret = crypto.randomBytes(32);
  }

  // Pre-compute API key buffer and hash for timing-safe comparison
  const apiKeyBuffer = apiKey ? Buffer.from(apiKey) : null;
  const apiKeyHashForComparison = apiKey ? hashApiKey(apiKey) : null;

  const app = express();
  app.use(express.json({ limit: "100kb" }));

  // JSON parse error handler
  app.use((err, req, res, next) => {
    if (err instanceof SyntaxError && "body" in err) {
      return res.status(400).json({
        jsonrpc: "2.0",
        error: { code: -32700, message: "Parse error: Invalid JSON" },
        id: null,
      });
    }
    next(err);
  });

  // Health check (no auth required, no version to avoid information disclosure)
  app.get("/health", (req, res) => {
    res.json({
      status: "healthy",
      service: "fetchaller-mcp",
      timestamp: new Date().toISOString(),
    });
  });

  // =============================================================================
  // OAuth 2.1 Endpoints (no auth required - these ARE the auth mechanism)
  // =============================================================================

  // Periodic cleanup (store interval ID for graceful shutdown)
  const oauthCleanupInterval = startOAuthCleanup();

  // OAuth Protected Resource Metadata (RFC 9728)
  // Claude looks for this to discover the authorization server
  app.get("/.well-known/oauth-protected-resource", (req, res) => {
    res.json({
      resource: serverUrl,
      authorization_servers: [serverUrl],
      bearer_methods_supported: ["header"],
      scopes_supported: ["fetchaller:read"],
    });
  });

  // OAuth Authorization Server Metadata (RFC 8414)
  // Claude uses this to discover OAuth endpoints
  app.get("/.well-known/oauth-authorization-server", (req, res) => {
    res.json({
      issuer: serverUrl,
      authorization_endpoint: `${serverUrl}/authorize`,
      token_endpoint: `${serverUrl}/token`,
      registration_endpoint: `${serverUrl}/register`,
      response_types_supported: ["code"],
      grant_types_supported: ["authorization_code"],
      code_challenge_methods_supported: ["S256"],
      token_endpoint_auth_methods_supported: ["none", "client_secret_post"],
      scopes_supported: ["fetchaller:read"],
    });
  });

  // Dynamic Client Registration (RFC 7591)
  // Claude registers itself as an OAuth client
  // Rate limited to prevent abuse
  app.post("/register", rateLimitMiddleware, (req, res) => {
    const { redirect_uris, client_name, token_endpoint_auth_method } = req.body || {};

    // Enforce max clients limit to prevent memory exhaustion
    if (oauthClients.size >= MAX_OAUTH_CLIENTS) {
      console.error(`[${new Date().toISOString()}] OAuth: Max clients limit reached (${MAX_OAUTH_CLIENTS})`);
      return res.status(503).json({
        error: "server_error",
        error_description: "Maximum number of clients reached. Try again later.",
      });
    }

    // Validate redirect_uris
    if (!redirect_uris || !Array.isArray(redirect_uris) || redirect_uris.length === 0) {
      return res.status(400).json({
        error: "invalid_client_metadata",
        error_description: "redirect_uris is required",
      });
    }

    // Validate redirect URIs (must be HTTPS or localhost)
    for (const uri of redirect_uris) {
      try {
        const parsed = new URL(uri);
        if (parsed.protocol !== "https:" && !parsed.hostname.match(/^(localhost|127\.0\.0\.1)$/)) {
          return res.status(400).json({
            error: "invalid_redirect_uri",
            error_description: "Redirect URIs must be HTTPS or localhost",
          });
        }
      } catch {
        return res.status(400).json({
          error: "invalid_redirect_uri",
          error_description: "Invalid redirect URI format",
        });
      }
    }

    // Generate client credentials
    const clientId = generateId(16);
    const clientSecret = generateId(32);

    // Store client
    oauthClients.set(clientId, {
      client_secret: clientSecret,
      redirect_uris,
      client_name: client_name || "Claude",
      created_at: Date.now(),
    });

    console.error(`[${new Date().toISOString()}] OAuth: Registered client ${sanitizeForLog(clientId)} (${sanitizeForLog(client_name) || "Claude"})`);

    res.status(201).json({
      client_id: clientId,
      client_secret: clientSecret,
      client_name: client_name || "Claude",
      redirect_uris,
      token_endpoint_auth_method: token_endpoint_auth_method || "none",
    });
  });

  // Authorization Endpoint - GET shows the login form
  app.get("/authorize", (req, res) => {
    const { client_id, redirect_uri, response_type, state, code_challenge, code_challenge_method } = req.query;

    // Validate required params - use RFC 6749 JSON error format
    if (!client_id) {
      return res.status(400).json({
        error: "invalid_request",
        error_description: "Missing client_id parameter",
      });
    }
    if (response_type !== "code") {
      return res.status(400).json({
        error: "unsupported_response_type",
        error_description: "Only response_type=code is supported",
      });
    }
    if (!code_challenge) {
      return res.status(400).json({
        error: "invalid_request",
        error_description: "PKCE code_challenge is required",
      });
    }
    if (code_challenge_method && code_challenge_method !== "S256") {
      return res.status(400).json({
        error: "invalid_request",
        error_description: "Only S256 code_challenge_method is supported",
      });
    }

    // Validate client exists
    const client = oauthClients.get(client_id);
    if (!client) {
      return res.status(400).json({
        error: "invalid_client",
        error_description: "Unknown client_id",
      });
    }

    // Validate redirect_uri
    if (redirect_uri && !client.redirect_uris.includes(redirect_uri)) {
      return res.status(400).json({
        error: "invalid_request",
        error_description: "Invalid redirect_uri",
      });
    }

    // Show the authorization form
    const actualRedirectUri = redirect_uri || client.redirect_uris[0];
    res.type("html").send(getAuthorizePage(client_id, actualRedirectUri, state, code_challenge));
  });

  // Authorization Endpoint - POST handles form submission
  app.post("/authorize", express.urlencoded({ extended: false }), (req, res) => {
    const { client_id, redirect_uri, state, code_challenge, api_key } = req.body;

    // Validate client
    const client = oauthClients.get(client_id);
    if (!client) {
      return res.status(400).json({
        error: "invalid_client",
        error_description: "Unknown client_id",
      });
    }

    // Validate redirect_uri against registered URIs (prevents redirect attacks)
    if (!redirect_uri || !client.redirect_uris.includes(redirect_uri)) {
      return res.status(400).json({
        error: "invalid_request",
        error_description: "Invalid redirect_uri",
      });
    }

    // Validate API key against configured MCP_API_KEY
    if (!apiKey) {
      return res.type("html").send(getAuthorizePage(
        client_id, redirect_uri, state, code_challenge,
        "Server not configured. MCP_API_KEY environment variable is not set."
      ));
    }

    // Timing-safe comparison of API key
    const providedKeyBuffer = Buffer.from(api_key || "");
    const expectedKeyBuffer = Buffer.from(apiKey);
    const keysMatch = providedKeyBuffer.length === expectedKeyBuffer.length &&
      crypto.timingSafeEqual(providedKeyBuffer, expectedKeyBuffer);

    if (!keysMatch) {
      console.error(`[${new Date().toISOString()}] OAuth: Invalid API key attempt for client ${sanitizeForLog(client_id)}`);
      return res.type("html").send(getAuthorizePage(
        client_id, redirect_uri, state, code_challenge,
        "Invalid API key. Please check your MCP_API_KEY and try again."
      ));
    }

    // Enforce max auth codes limit
    if (authCodes.size >= MAX_AUTH_CODES) {
      // Clean up expired codes first
      const now = Date.now();
      for (const [c, data] of authCodes) {
        if (data.expires_at < now) authCodes.delete(c);
      }
      // If still at limit, reject
      if (authCodes.size >= MAX_AUTH_CODES) {
        console.error(`[${new Date().toISOString()}] OAuth: Max auth codes limit reached`);
        return res.type("html").send(getAuthorizePage(
          client_id, redirect_uri, state, code_challenge,
          "Server busy. Please try again in a few minutes."
        ));
      }
    }

    // Generate authorization code
    // Store hash of API key instead of raw key for security
    const code = generateId(32);
    authCodes.set(code, {
      client_id,
      code_challenge,
      redirect_uri,
      api_key_hash: hashApiKey(api_key),
      expires_at: Date.now() + AUTH_CODE_TTL,
    });

    console.error(`[${new Date().toISOString()}] OAuth: Issued auth code for client ${sanitizeForLog(client_id)}`);

    // Redirect back to Claude with the code
    const redirectUrl = new URL(redirect_uri);
    redirectUrl.searchParams.set("code", code);
    if (state) redirectUrl.searchParams.set("state", state);

    res.redirect(redirectUrl.toString());
  });

  // Token Endpoint - Exchange code for access token
  app.post("/token", express.urlencoded({ extended: false }), (req, res) => {
    const { grant_type, code, redirect_uri, client_id, code_verifier } = req.body;

    // Validate grant type
    if (grant_type !== "authorization_code") {
      return res.status(400).json({
        error: "unsupported_grant_type",
        error_description: "Only authorization_code grant is supported",
      });
    }

    // Validate required parameters
    if (!code || !client_id) {
      return res.status(400).json({
        error: "invalid_request",
        error_description: "Missing required parameter: code and client_id are required",
      });
    }

    // Validate code exists
    const authCode = authCodes.get(code);
    if (!authCode) {
      return res.status(400).json({
        error: "invalid_grant",
        error_description: "Invalid or expired authorization code",
      });
    }

    // Check expiration
    if (authCode.expires_at < Date.now()) {
      authCodes.delete(code);
      return res.status(400).json({
        error: "invalid_grant",
        error_description: "Authorization code has expired",
      });
    }

    // Verify client_id matches - delete code on failure to prevent retry attacks
    if (authCode.client_id !== client_id) {
      authCodes.delete(code);
      return res.status(400).json({
        error: "invalid_grant",
        error_description: "Client ID mismatch",
      });
    }

    // Verify redirect_uri matches stored value
    if (authCode.redirect_uri !== redirect_uri) {
      authCodes.delete(code);
      return res.status(400).json({
        error: "invalid_grant",
        error_description: "Redirect URI mismatch",
      });
    }

    // Verify PKCE code_verifier - delete code on failure to prevent retry attacks
    if (!code_verifier || !verifyPKCE(code_verifier, authCode.code_challenge)) {
      authCodes.delete(code);
      return res.status(400).json({
        error: "invalid_grant",
        error_description: "Invalid code_verifier",
      });
    }

    // Delete the auth code (one-time use)
    authCodes.delete(code);

    // Generate access token using the stored API key hash
    const accessToken = createAccessToken(client_id, authCode.api_key_hash, serverSecret);

    console.error(`[${new Date().toISOString()}] OAuth: Issued access token for client ${sanitizeForLog(client_id)}`);

    res.json({
      access_token: accessToken,
      token_type: "Bearer",
      expires_in: ACCESS_TOKEN_TTL / 1000,
      scope: "fetchaller:read",
    });
  });

  // Rate limiting state with bounded size
  // Note: In-memory rate limiting. See CLAUDE.md for horizontal scaling limitations.
  const rateLimits = new Map();
  const MAX_RATE_LIMIT_ENTRIES = 10000; // Prevent unbounded growth

  // Get client IP (supports reverse proxy)
  function getClientIp(req) {
    const forwarded = req.headers["x-forwarded-for"];
    if (forwarded) {
      // Use rightmost IP (set by our trusted reverse proxy, harder to spoof)
      // Leftmost can be spoofed by clients; rightmost is what proxy actually sees
      const ips = forwarded.split(",");
      return ips[ips.length - 1].trim();
    }
    return req.socket?.remoteAddress || "unknown";
  }

  // Periodic cleanup of stale rate limit entries
  let lastCleanup = Date.now();
  function cleanupRateLimits() {
    const now = Date.now();
    const windowStart = now - 60000;

    // Only cleanup every 30 seconds
    if (now - lastCleanup < 30000) return;
    lastCleanup = now;

    for (const [ip, entry] of rateLimits) {
      // Remove entries with no recent requests
      if (entry.requests.length === 0 || entry.requests[entry.requests.length - 1] < windowStart) {
        rateLimits.delete(ip);
      }
    }
  }

  // Rate limiting middleware
  function rateLimitMiddleware(req, res, next) {
    const clientIp = getClientIp(req);
    const now = Date.now();
    const windowStart = now - 60000; // 1 minute window

    let entry = rateLimits.get(clientIp);
    if (!entry) {
      // Enforce max entries to prevent memory exhaustion
      if (rateLimits.size >= MAX_RATE_LIMIT_ENTRIES) {
        cleanupRateLimits();
        // If still at limit after cleanup, reject new IPs temporarily
        if (rateLimits.size >= MAX_RATE_LIMIT_ENTRIES) {
          console.error(`[${new Date().toISOString()}] Rate limit map full, rejecting new IP: ${clientIp}`);
          return res.status(503).json({
            jsonrpc: "2.0",
            error: { code: -32000, message: "Server busy. Try again later." },
            id: null,
          });
        }
      }
      entry = { requests: [] };
      rateLimits.set(clientIp, entry);
    }

    // Filter old requests (keep only timestamps in current window)
    // Use a simple loop instead of filter to avoid creating new arrays
    let writeIdx = 0;
    for (let i = 0; i < entry.requests.length; i++) {
      if (entry.requests[i] > windowStart) {
        entry.requests[writeIdx++] = entry.requests[i];
      }
    }
    entry.requests.length = writeIdx;

    // Check limit
    if (entry.requests.length >= rateLimit) {
      console.error(`[${new Date().toISOString()}] Rate limited: ${clientIp}`);
      return res.status(429).json({
        jsonrpc: "2.0",
        error: { code: -32000, message: `Rate limit exceeded (${rateLimit} requests/minute). Try again in 60 seconds.` },
        id: null,
      });
    }

    entry.requests.push(now);

    // Trigger periodic cleanup
    cleanupRateLimits();

    next();
  }

  // Pre-compute API key hash for truly constant-time comparison
  // Hashing ensures comparison is always same length regardless of input
  const apiKeyHash = apiKeyBuffer
    ? crypto.createHash("sha256").update(apiKeyBuffer).digest()
    : null;

  // Timing-safe token comparison to prevent timing attacks
  // Uses SHA-256 hashing to ensure constant-time comparison regardless of token length
  function safeTokenCompare(token) {
    if (!token || !apiKeyHash) return false;
    const tokenHash = crypto.createHash("sha256").update(token).digest();
    return crypto.timingSafeEqual(tokenHash, apiKeyHash);
  }

  // Bearer token auth middleware
  // Accepts EITHER:
  // 1. Raw API key (for direct Bearer token auth)
  // 2. OAuth JWT token (issued by /token endpoint)
  function authMiddleware(req, res, next) {
    // If no API key configured, deny all requests (secure by default in production)
    if (!apiKeyHash) {
      console.error(`[${new Date().toISOString()}] Auth failed: No MCP_API_KEY configured`);
      return res.status(401).json({
        jsonrpc: "2.0",
        error: { code: -32001, message: "Server authentication not configured. Contact the server administrator." },
        id: null,
      });
    }

    const authHeader = req.headers.authorization;
    if (!authHeader) {
      console.error(`[${new Date().toISOString()}] Auth failed: Missing Authorization header from ${getClientIp(req)}`);
      return res.status(401).json({
        jsonrpc: "2.0",
        error: { code: -32002, message: "Missing Authorization header" },
        id: null,
      });
    }

    // Parse auth header with regex to handle tokens containing spaces
    const match = authHeader.match(/^Bearer\s+(.+)$/i);
    if (!match) {
      console.error(`[${new Date().toISOString()}] Auth failed: Invalid Authorization header format from ${getClientIp(req)}`);
      return res.status(401).json({
        jsonrpc: "2.0",
        error: { code: -32003, message: "Invalid Authorization header format" },
        id: null,
      });
    }

    const token = match[1];

    // First, check if it's a raw API key
    if (safeTokenCompare(token)) {
      return next();
    }

    // Second, check if it's an OAuth JWT token
    const storedApiKeyHash = verifyAccessToken(token, serverSecret);
    if (storedApiKeyHash && apiKeyHashForComparison) {
      // Verify the OAuth token's API key hash matches current server config
      // Use timing-safe comparison of hashes
      const storedHashBuffer = Buffer.from(storedApiKeyHash);
      const expectedHashBuffer = Buffer.from(apiKeyHashForComparison);
      if (storedHashBuffer.length === expectedHashBuffer.length &&
          crypto.timingSafeEqual(storedHashBuffer, expectedHashBuffer)) {
        return next();
      }
    }

    console.error(`[${new Date().toISOString()}] Auth failed: Invalid token from ${getClientIp(req)}`);
    return res.status(401).json({
      jsonrpc: "2.0",
      error: { code: -32003, message: "Invalid Bearer token" },
      id: null,
    });
  }

  // MCP endpoint - stateless mode (new server + transport per request)
  app.post("/mcp", rateLimitMiddleware, authMiddleware, async (req, res) => {
    const server = createServer();
    let transport;

    try {
      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined, // stateless mode
      });

      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (error) {
      console.error(`[${new Date().toISOString()}] Error handling MCP request:`, error);
      if (!res.headersSent) {
        res.status(500).json({
          jsonrpc: "2.0",
          error: { code: -32603, message: "Internal server error" },
          id: null,
        });
      }
    } finally {
      // Cleanup with proper error handling
      try {
        await transport?.close?.();
      } catch {
        // Ignore cleanup errors
      }
      try {
        await server?.close?.();
      } catch {
        // Ignore cleanup errors
      }
    }
  });

  // HEAD for MCP protocol discovery (required by MCP spec 2025-06-18)
  app.head("/mcp", (req, res) => {
    res.set("MCP-Protocol-Version", "2025-06-18");
    res.set("Allow", "POST, HEAD");
    res.status(200).end();
  });

  // Reject other methods on /mcp (include Allow header per RFC 7231)
  app.get("/mcp", (req, res) => {
    res.set("Allow", "POST, HEAD").status(405).json({
      jsonrpc: "2.0",
      error: { code: -32000, message: "Method not allowed. Use POST or HEAD." },
      id: null,
    });
  });

  app.delete("/mcp", (req, res) => {
    res.set("Allow", "POST, HEAD").status(405).json({
      jsonrpc: "2.0",
      error: { code: -32000, message: "Method not allowed. Use POST or HEAD." },
      id: null,
    });
  });

  // Catch-all 404 handler (return JSON-RPC error, not HTML)
  app.use((req, res) => {
    res.status(404).json({
      jsonrpc: "2.0",
      error: { code: -32000, message: "Not found" },
      id: null,
    });
  });

  const server = app.listen(port, "0.0.0.0", () => {
    console.error(`[${new Date().toISOString()}] fetchaller MCP HTTP server v${VERSION} listening on port ${port}`);
    if (apiKeyHash) {
      console.error(`[${new Date().toISOString()}] Bearer token authentication enabled`);
      console.error(`[${new Date().toISOString()}] OAuth 2.1 endpoints enabled (for Claude.ai connectors)`);
      console.error(`[${new Date().toISOString()}]   - Authorization: ${serverUrl}/authorize`);
      console.error(`[${new Date().toISOString()}]   - Token: ${serverUrl}/token`);
      console.error(`[${new Date().toISOString()}]   - Register: ${serverUrl}/register`);
    } else {
      console.error(`[${new Date().toISOString()}] WARNING: No MCP_API_KEY set - all requests will be denied`);
    }
    console.error(`[${new Date().toISOString()}] Rate limit: ${rateLimit} requests/minute per IP`);
  });

  // Graceful shutdown handler
  function gracefulShutdown(signal) {
    console.error(`[${new Date().toISOString()}] Received ${signal}, shutting down gracefully...`);

    // Clear the OAuth cleanup interval
    if (oauthCleanupInterval) {
      clearInterval(oauthCleanupInterval);
    }

    server.close(() => {
      console.error(`[${new Date().toISOString()}] HTTP server closed`);
      process.exit(0);
    });

    // Force exit after 10 seconds if connections don't close
    // Use .unref() so this timer doesn't keep the process alive if server closes cleanly
    const forceExitTimer = setTimeout(() => {
      console.error(`[${new Date().toISOString()}] Forcing shutdown after timeout`);
      process.exit(1);
    }, 10000);
    forceExitTimer.unref();
  }

  process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
  process.on("SIGINT", () => gracefulShutdown("SIGINT"));
}

// Start server with proper error handling
async function main() {
  if (httpMode) {
    await startHttpServer();
  } else {
    // Stdio mode (default, for local use)
    const server = createServer();
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error("fetchaller MCP server running on stdio");
  }
}

main().catch((error) => {
  console.error("Fatal error:", error);
  process.exit(1);
});
