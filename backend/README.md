# Growth Gradual — Python FastAPI Backend

Pure-Python backend replacing all Next.js API routes. No browser automation, no Node.js dependencies.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | SSE streaming chat (Groq → Gemini fallback + Tavily search) |
| POST | `/api/chat/report` | Research report generation (JSON) |
| POST | `/api/chat/report/pdf` | PDF export via ReportLab (no Puppeteer) |
| GET | `/health` | Health check |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEYS` | Comma-separated Groq API keys |
| `TAVILY_API_KEY` | Comma-separated Tavily search API keys |
| `GEMINI_API_KEY` | Comma-separated Gemini API keys |
| `DATAWRAPPER_API_TOKEN` | Datawrapper API token (Settings → API Tokens). Powers the report's bar/line/pie charts in both the chat view and PDF export. If unset, charts fall back to the built-in lightweight SVG renderer. |
| `LOGO_B64` | (Optional) Base64-encoded logo image for PDF cover |

## Local Development

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Render Deployment

1. Create a new **Web Service** in Render, point to the `backend/` folder.
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables above.
5. After deploy, copy the service URL (e.g. `https://growth-gradual-backend.onrender.com`).

## Frontend Integration

In your **frontend** Render service (or `.env.local`), set:

```
BACKEND_URL=https://growth-gradual-backend.onrender.com
```

The Next.js API routes (`/api/chat`, `/api/chat/report`, `/api/chat/report/pdf`) are now thin proxies — they forward all requests to this backend unchanged.

## PDF Notes

PDF generation uses **ReportLab** (pure Python). Features:
- Branded cover page with logo, domain tag, key stats
- Full report body with headings, paragraphs, bullet lists, tables
- Bar / line / pie charts rendered natively (no browser needed)
- Footer with disclaimer on every page

To include your logo, either:
- Place `logo.jpg` / `logo.png` in the working directory, **or**
- Set `LOGO_B64` env var to the base64-encoded image bytes.
