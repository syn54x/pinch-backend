"""Auth: identity, sessions, and the login-method seam (PRD M2, ADR-0005).

Hand-reviewed module — boring patterns only. Pinch owns identity and
sessions; login methods are pluggable and terminate in one session issuance.
"""
