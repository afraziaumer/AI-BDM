"""Central configuration for the RAG layer.

Everything tunable lives here so the other modules stay clean and you can
experiment (chunk size, k, model, dev vs prod store) by editing one file.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# --- Chunking -------------------------------------------------------------
# ~220 chars ≈ 55 tokens — well inside all-MiniLM-L6-v2's ~256-token window.
# Kept deliberately small (rather than the ~700 chars a raw token-budget
# argument would allow) so a short, distinctive one-line fact on a scraped
# page (e.g. "it has no technical app") doesn't get merged into the same
# chunk as unrelated surrounding text (address blocks, boat specs, etc.) --
# merging dilutes that fact's embedding toward the chunk's average meaning.
# Overlap keeps context across chunk boundaries.
CHUNK_SIZE_CHARS = 220
CHUNK_OVERLAP_CHARS = 40

# --- Embedding ------------------------------------------------------------
EMBEDDING_MODEL = "all-MiniLM-L6-v2"   # 384-dim, small, fast, CPU-friendly
EMBEDDING_DIM = 384
NORMALIZE_EMBEDDINGS = True            # makes cosine == dot product

# --- Vector store ---------------------------------------------------------
# "chroma" for local development (zero setup), "mongo" for production.
STORE_BACKEND = "chroma"

# Chroma (dev)
CHROMA_DIR = str(PACKAGE_DIR / ".chroma")  # local on-disk store
CHROMA_COLLECTION = "aibdm_chunks"

# MongoDB Atlas Vector Search (prod) — fill in when you migrate.
MONGO_URI = ""                         # e.g. os.getenv("MONGO_URI")
MONGO_DB = "aibdm"
MONGO_COLLECTION = "chunks"
MONGO_VECTOR_INDEX = "vector_index"

# --- Retrieval ------------------------------------------------------------
TOP_K = 3                              # how many chunks to feed the LLM

# --- Generation -----------------------------------------------------------
# Reuse the Groq setup the scraper already uses (see LLM_planner.py).
LLM_MODEL = "openai/gpt-oss-20b"

# --- Dev data source ------------------------------------------------------
RAW_STORE_CSV = str(PACKAGE_DIR / "dummy_source_docs.csv")
