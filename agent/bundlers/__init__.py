"""Non-AI context bundlers.

Each `get_<skill>_bundle(agent_name)` returns a dataclass with everything that
skill type needs to operate, composed from existing functions in db/store.py,
ibkr/account.py, and agent/prompt_builder.py. The Jinja templates render these
dataclasses into the user-message portion of the prompt.
"""
