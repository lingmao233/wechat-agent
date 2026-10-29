"""Keep the offline test suite independent from operator .env credentials."""

import os
from pathlib import Path

from wechat_agent.config import Settings


os.environ.setdefault("EMBEDDING_ENABLED", "false")
os.environ.setdefault("OPENVIKING_ENABLED", "false")
os.environ.setdefault(
    "WECHAT_STOP_FILE",
    str(Path(os.environ.get("TEMP", ".")) / "wechat-agent-pytest-stop-does-not-exist"),
)

# The suite must not inherit the operator's live .env: allowlists, send flags and
# model endpoints there would change what each test actually exercises.  Tests
# that load a config file pass ``_env_file=...`` explicitly, which still works.
Settings.model_config["env_file"] = None
