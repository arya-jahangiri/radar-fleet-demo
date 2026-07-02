"use strict";

const CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,OPTIONS",
  "access-control-allow-headers": "content-type",
};

function intEnv(env, name, fallback, lo, hi) {
  const raw = Number(env[name] ?? fallback);
  if (!Number.isFinite(raw)) return fallback;
  return Math.min(hi, Math.max(lo, Math.floor(raw)));
}

function withCors(response) {
  const headers = new Headers(response.headers);
  for (const [key, value] of Object.entries(CORS_HEADERS)) headers.set(key, value);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function jsonError(status, message) {
  return new Response(JSON.stringify({ type: "error", message }), {
    status,
    headers: {
      ...CORS_HEADERS,
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

async function fetchUpstreamSnapshot(env) {
  if (!env.AWS_SNAPSHOT_URL) {
    throw new Error("AWS_SNAPSHOT_URL is not configured");
  }
  const response = await fetch(env.AWS_SNAPSHOT_URL, {
    headers: { accept: "application/json" },
    cf: { cacheTtl: 0 },
  });
  if (!response.ok) {
    throw new Error(`snapshot upstream returned HTTP ${response.status}`);
  }
  const text = await response.text();
  JSON.parse(text);
  return text;
}

async function snapshotText(request, env, ctx) {
  const cacheSeconds = intEnv(env, "SNAPSHOT_CACHE_SECONDS", 3, 0, 60);
  const cacheKey = new Request(new URL("/__snapshot-cache", request.url).toString(), { method: "GET" });
  if (cacheSeconds > 0) {
    const cached = await caches.default.match(cacheKey);
    if (cached) return cached.text();
  }

  const text = await fetchUpstreamSnapshot(env);
  if (cacheSeconds > 0) {
    const response = new Response(text, {
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": `public, max-age=${cacheSeconds}`,
      },
    });
    ctx.waitUntil(caches.default.put(cacheKey, response));
  }
  return text;
}

async function snapshotResponse(request, env, ctx) {
  try {
    const text = await snapshotText(request, env, ctx);
    return new Response(text, {
      headers: {
        ...CORS_HEADERS,
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
      },
    });
  } catch (error) {
    return jsonError(502, error instanceof Error ? error.message : "snapshot unavailable");
  }
}

function streamResponse(request, env, ctx) {
  const intervalMs = intEnv(env, "STREAM_INTERVAL_MS", 3000, 1000, 30000);
  const encoder = new TextEncoder();
  let stopped = false;
  request.signal.addEventListener("abort", () => {
    stopped = true;
  });

  const stream = new ReadableStream({
    async start(controller) {
      while (!stopped) {
        try {
          const text = await snapshotText(request, env, ctx);
          controller.enqueue(encoder.encode(`event: snapshot\ndata: ${text}\n\n`));
        } catch (error) {
          const message = error instanceof Error ? error.message : "snapshot unavailable";
          controller.enqueue(encoder.encode(`event: error\ndata: ${JSON.stringify({ message })}\n\n`));
        }
        await new Promise((resolve) => setTimeout(resolve, intervalMs));
      }
      controller.close();
    },
    cancel() {
      stopped = true;
    },
  });

  return new Response(stream, {
    headers: {
      ...CORS_HEADERS,
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-store, no-transform",
      "x-accel-buffering": "no",
    },
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== "GET") {
      return jsonError(405, "method not allowed");
    }
    if (url.pathname === "/api/snapshot") {
      return snapshotResponse(request, env, ctx);
    }
    if (url.pathname === "/api/stream") {
      return streamResponse(request, env, ctx);
    }
    return env.ASSETS.fetch(request);
  },
};
