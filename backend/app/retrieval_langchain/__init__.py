"""LangChain-facing retrieval wrappers over existing Supabase pgvector RPCs.

Deliberately NOT built on ``langchain_community.vectorstores.SupabaseVectorStore``
— its expected RPC signature (``match_documents``-style) doesn't match this
app's existing ``match_session_chunks`` RPC (custom params: ``p_session_id``,
``match_threshold``). These are thin wrappers around the same
``db.rpc("match_session_chunks", ...)`` call ``session.py`` already makes,
exposed as a LangChain ``Tool``.
"""

from app.retrieval_langchain.sp_retriever import build_sp_tool, retrieve_sp_chunks

__all__ = ["build_sp_tool", "retrieve_sp_chunks"]
