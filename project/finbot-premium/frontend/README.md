# Frontend — Next.js 16 + TypeScript

AI-powered Indian financial markets chatbot with report generation, inline charts, and PDF export.

## Setup

```bash
cp ../.env.example .env.local   # fill in your API keys
npm install
npm run dev
```

## Key Routes

| Route | Description |
|---|---|
| `/` | Full-page AI chat (GrowthGradualChat) |
| `/stocks` `/banks` `/finance` `/mutual_funds` | Category feed pages |
| `/article` | Article viewer |

## API Routes

| Route | Description |
|---|---|
| `POST /api/chat` | Streaming chat — Tavily top-25 + Groq/Gemini |
| `POST /api/chat/report` | Full report with charts from top-25 pages |
| `POST /api/chat/report/pdf` | Download report as branded PDF |
| `GET /api/market` | NSE/BSE market data |
| `GET /api/scrape` | News feed (proxies to Python backend) |
| `GET /api/article` | Article scraper |

## Environment

See `../.env.example` for all required variables. Copy to `.env.local`.
