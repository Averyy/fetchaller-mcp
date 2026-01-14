#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import * as cheerio from "cheerio";
import TurndownService from "turndown";

const DEFAULT_MAX_TOKENS = 25000;
const DEFAULT_TIMEOUT_SECONDS = 10;
const CHARS_PER_TOKEN = 4;

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

async function fetchUrlContent(url, maxTokens = DEFAULT_MAX_TOKENS, timeoutSeconds = DEFAULT_TIMEOUT_SECONDS) {
  // Validate URL
  let parsedUrl;
  try {
    parsedUrl = new URL(url);
    if (!["http:", "https:"].includes(parsedUrl.protocol)) {
      return { error: `Invalid protocol: ${parsedUrl.protocol}. Only http/https supported.` };
    }
  } catch {
    return { error: `Invalid URL: ${url}` };
  }

  // Fetch with timeout
  const controller = new AbortController();
  const timeoutMs = timeoutSeconds * 1000;
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchWithRetry(url, {
      signal: controller.signal,
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
      },
    });

    clearTimeout(timeout);

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
      return { error: `Request timed out (${timeoutSeconds}s limit)` };
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
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
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

// Create server
const server = new McpServer({
  name: "fetchaller",
  version: "1.0.0",
});

// Register the fetch tool
server.tool(
  "fetch",
  "Fetch any URL and return the page content as clean markdown. Use this tool for reading/fetching web pages - it has no domain restrictions. For discovering URLs via search, use WebSearch. For reading URL content, use this tool.",
  {
    url: z.string().describe("The URL to fetch"),
    maxTokens: z.number().optional().describe("Maximum tokens to return (default: 25000)"),
    timeout: z.number().optional().describe("Request timeout in seconds (default: 10)"),
  },
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

// Browse subreddit listings
server.tool(
  "browse_reddit",
  "Browse a subreddit's posts. Returns metadata and URLs. Use mcp__fetchaller__fetch to read full post content.",
  {
    subreddit: z.string().describe("Subreddit name without r/ prefix"),
    sort: z.enum(["hot", "new", "top", "rising"]).default("hot").describe("Sort order"),
    time: z.enum(["hour", "day", "week", "month", "year", "all"]).default("day")
      .describe("Time filter (only applies to 'top' sort)"),
    limit: z.number().min(1).max(25).default(10).describe("Number of posts (1-25)"),
    after: z.string().optional().describe("Pagination cursor from previous response"),
    timeout: z.number().optional().describe("Request timeout in seconds (default: 10)"),
  },
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

// Search Reddit posts
server.tool(
  "search_reddit",
  "Search Reddit posts. Returns metadata and URLs. Use mcp__fetchaller__fetch to read full post content.",
  {
    query: z.string().describe("Search query"),
    subreddit: z.string().optional().describe("Limit to subreddit (without r/)"),
    sort: z.enum(["relevance", "hot", "top", "new", "comments"]).default("relevance").describe("Sort order"),
    time: z.enum(["hour", "day", "week", "month", "year", "all"]).default("all").describe("Time filter"),
    limit: z.number().min(1).max(25).default(10).describe("Number of results (1-25)"),
    after: z.string().optional().describe("Pagination cursor from previous response"),
    timeout: z.number().optional().describe("Request timeout in seconds (default: 10)"),
  },
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

// Start server with proper error handling
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);

  // Log to stderr (stdout is reserved for MCP protocol)
  console.error("fetchaller MCP server running on stdio");
}

main().catch((error) => {
  console.error("Fatal error:", error);
  process.exit(1);
});
