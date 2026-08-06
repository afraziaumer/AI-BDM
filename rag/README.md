# RAG Layer - Developer B (Knowledge Processing)

This package is Developer B's part of AI-BDM. It is decoupled from the scraper:
it consumes the shared contract `{url, title, content}` and turns website text
into answerable knowledge.

```text
clean CSV/JSON text -> chunk -> embed -> store -> retrieve -> LLM answer
```

## The pipeline

| Step | File | What it does |
|------|------|--------------|
| 0 | `contract.py` | The input/output data shapes |
| 1 | `chunker.py` | Cuts `content` into small overlapping notes |
| 2 | `embedder.py` | Turns each note into a meaning-vector using all-MiniLM-L6-v2 |
| 3 | `store.py` | Saves notes and vectors in Chroma locally |
| 4 | `retriever.py` | Finds the top-k notes for a question |
| 5 | `generator.py` | Asks the LLM to answer using only those notes |
| - | `pipeline.py` | Wires it together: `ingest()` and `answer()` |
| - | `ingest_from_csv.py` | Loads dummy or scraped CSV data |
| - | `match_query.py` | One-command CSV chunking, embedding, and query matching |
| - | `llm_match_query.py` | Sends the best matched chunk to the LLM for explanation |
| - | `config.py` | All tunable settings in one place |

## Quick start with dummy data

1. Install dependencies:

```bash
pip install -r rag/requirements-rag.txt
```

2. Ingest the included dummy CSV:

```bash
python -m rag.ingest_from_csv
```

This reads `rag/dummy_source_docs.csv`, chunks the text, creates embeddings,
and stores them in local Chroma at `rag/.chroma`.

3. Ask a question:

```bash
python -m rag.ask "what dental services are offered?" --business example-dental.com
```

Or, to see only which chunks match your query without calling the LLM:

```bash
python -m rag.match_query "what dental services are offered?"
```

That command reads the CSV, chunks the text, creates embeddings, embeds your
query, and prints the chunks with the highest similarity scores.

To send the highest matched chunk to the LLM and get a 4-5 line explanation:

```bash
python -m rag.llm_match_query "what dental services are offered?"
```

For a guaranteed test match, use:

```bash
python -m rag.llm_match_query "What does AI-BDM do for business development managers?"
```

That query should match the `Perfect Match AI-BDM Test` row in
`rag/dummy_source_docs.csv`.

## CSV format

The dummy CSV uses:

```csv
url,title,content
```

Scraper output is also supported:

```csv
page_url,page_title,page_text
```

To ingest a different CSV:

```bash
python -m rag.ingest_from_csv --path scavenger_leads_cache.csv
```

## Key rules

- Never import the scraper's HTML logic. Only accept `SourceDoc`.
- Develop on local Chroma first, then swap to MongoDB Atlas later.
- Keep each stage testable: chunk, embed, store, retrieve, generate.
