/**
 * cf_ai_investintel — Cloudflare AI Assignment
 * Esa Dhanani, Nov 2025
 *
 * Minimal agent that answers macro-finance questions
 * using Workers AI (Llama 3.3 70B Instruct).
 */

import { Ai } from "@cloudflare/ai";

export interface Env {
  AI: Ai;
}

const memory: { topic: string; summary: string; time: string }[] = [];

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    // ── POST /query → summarize topic ──────────────────────────────
    if (url.pathname === "/query" && req.method === "POST") {
      const { topic } = await req.json();

      // Cast model + output to bypass type narrowing
      const ai = env.AI as any;

      const result = await ai.run(
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast" as any,
        {
          messages: [
            {
              role: "system",
              content:
                "You are a world-class macro analyst. Summarize global market trends, catalysts, and risks in three concise bullet points.",
            },
            { role: "user", content: topic },
          ],
        }
      );

      const summary =
        typeof result === "object" && "response" in result
          ? (result.response as string)
          : JSON.stringify(result);

      const entry = { topic, summary, time: new Date().toISOString() };
      memory.push(entry);

      return new Response(JSON.stringify(entry), {
        headers: { "content-type": "application/json" },
      });
    }

    // ── GET /history → last 5 summaries ─────────────────────────────
    if (url.pathname === "/history") {
      const lastFive = memory.slice(-5);
      return new Response(JSON.stringify(lastFive), {
        headers: { "content-type": "application/json" },
      });
    }

    // ── default root ────────────────────────────────────────────────
    return new Response(
      JSON.stringify({
        status: "InvestIntel Agent online ✅",
        usage: "POST /query { topic } or GET /history",
      }),
      { headers: { "content-type": "application/json" } }
    );
  },
};