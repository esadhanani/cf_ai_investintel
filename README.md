InvestIntel: AI Market Intelligence Agent

InvestIntel is an AI powered market intelligence application built entirely on Cloudflare Workers AI.
It summarises financial, macroeconomic, or company specific news into concise insights using Llama 3.3, and maintains short term context for users via lightweight in-memory state.

Key Features
	•	Workers AI (Llama 3.3 70B Instruct) for summarisation
	•	Short-term memory: retains the last five analyses per session
	•	Simple REST API:
	•	POST /query { topic }: generates a new summary
	•	GET /history: fetches recent insights
	•	Zero infrastructure: deployed entirely on Cloudflare’s edge (no servers, instant scale)

Tech Stack
	•	Cloudflare Workers AI
	•	TypeScript / Node.js compatibility
	•	In-memory state (Durable Objects optional)
# cf_ai_investintel
