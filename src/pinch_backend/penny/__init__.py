"""Penny — the AI assistant inside Pinch (PRD M9).

Three agents behind one instance-level model configuration (chat,
categorization, mapping), their capability bundles, and the availability
story. Penny is an API client: every tool bottoms out in in-process calls
to the public v1 API as the chatting user (ADR-0001's parity construction,
extended from the CLI).
"""
