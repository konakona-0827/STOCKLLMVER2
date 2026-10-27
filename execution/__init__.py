"""STOCKLLM broker execution boundary.

Nothing in this package should call the LLM.
Only capital_executor.py (added in a later step) will be allowed to submit broker orders.
"""
