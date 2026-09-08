"""Deterministic, admin-authored reply rules.

This package contains the pure decision seam. Provider intake and sending are
adapters around it; they must not reimplement matching or safety gates.
"""
