"""
llm.py  -  Module 6: talking to the LOCAL Llama model through Ollama

Nothing is sent to the internet: Ollama runs the models on this Mac.
  chat()   -> llama3.2 writes text (SQL, answers, summaries)
  embed()  -> nomic-embed-text turns text into 768 numbers ("embeddings") for RAG
"""
import os
import time

import ollama

from connectors import PROJECT_ROOT  # noqa: F401  (loads .env)

CHAT_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")


def chat(system, user, temperature=0.0, max_tokens=600):
    """Ask Llama. temperature=0 makes the answers as consistent (repeatable) as possible."""
    start = time.time()
    response = ollama.chat(
        model=CHAT_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        options={"temperature": temperature, "num_ctx": 4096, "num_predict": max_tokens},
    )
    return response["message"]["content"].strip(), round(time.time() - start, 2)


def embed(texts, kind="document"):
    """Turn a list of texts into embeddings. nomic-embed-text expects a short prefix
    telling it whether the text is a document to store or a question to search with."""
    prefix = "search_document: " if kind == "document" else "search_query: "
    response = ollama.embed(model=EMBED_MODEL, input=[prefix + t for t in texts])
    return response["embeddings"]
