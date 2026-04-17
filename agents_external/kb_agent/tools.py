"""
kb_agent/tools.py — Tool definitions for the knowledge base agent.
"""

from __future__ import annotations

from typing import Any

KB_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "store_document",
            "description": (
                "Store a markdown document in the user's knowledge base. "
                "The document is chunked, embedded, and indexed for hybrid search. "
                "The file_path should point to a .md file in the workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the .md file to store",
                    },
                    "title": {
                        "type": "string",
                        "description": "Document title (defaults to filename)",
                    },
                    "collection": {
                        "type": "string",
                        "description": "Collection name for grouping (default: 'default')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Brief description of the document",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                    },
                    "date": {
                        "type": "string",
                        "description": "Document date in YYYY-MM-DD format",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Search the user's knowledge base using hybrid search "
                "(semantic vectors + full-text BM25). Returns relevant text chunks "
                "with metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results (default 5)",
                    },
                    "collection": {
                        "type": "string",
                        "description": "Filter by collection name",
                    },
                    "title": {
                        "type": "string",
                        "description": "Filter by document title",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Filter by tag",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["hybrid", "vector", "fts"],
                        "description": "Search mode (default: hybrid)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "List all documents in the user's knowledge base with their metadata.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_document",
            "description": "Permanently remove a document from the knowledge base by its exact title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Exact document title to remove",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_document_metadata",
            "description": "Update metadata (collection, description, date, tags) for a document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Exact document title to modify",
                    },
                    "collection": {"type": "string", "description": "New collection name"},
                    "description": {"type": "string", "description": "New description"},
                    "date": {"type": "string", "description": "New date (YYYY-MM-DD)"},
                    "tags": {"type": "string", "description": "New comma-separated tags"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_document",
            "description": "Change the title of an existing document. The new title is deduplicated if it conflicts with another document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_title": {
                        "type": "string",
                        "description": "Current exact title of the document to rename",
                    },
                    "new_title": {
                        "type": "string",
                        "description": "New title for the document",
                    },
                },
                "required": ["old_title", "new_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to read",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write",
                    },
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Directory path (default: workspace root)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to delete",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
]
