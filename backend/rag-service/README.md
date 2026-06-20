---
title: Paperly RAG Service
emoji: 📄
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Paperly RAG Service-backend

FastAPI-based RAG (Retrieval-Augmented Generation) service for Paperly.

- Embeds documents using `all-MiniLM-L6-v2`
- Per-session FAISS indexes (in-memory)
- Adaptive similarity thresholding
- Endpoints: `/health`, `/index`, `/query`, `/report`, `/session/{id}`
