"""Separate metadata scan budgets from interactive business query limits."""
import os


def scan_timeout_ms():
    return max(100, int(os.getenv('SCAN_TIMEOUT_MS', '60000')))


def query_timeout_ms():
    return max(100, int(os.getenv('QUERY_TIMEOUT_MS', '15000')))
