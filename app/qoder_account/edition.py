"""Explicit vendor editions. Never turn a global runtime into CN by changing its URL."""
from __future__ import annotations
import os

EDITIONS = {
    "global": {"id": "global", "label": "Qoder", "module": "qoder_agent_sdk",
        "distribution": "qoder-agent-sdk", "cli": "qodercli", "config_env": "QODER_CONFIG_DIR",
        "state_dir": "qoder_account", "login_hosts": ("qoder.com",),
        "credential_source": "qoder_official_local_login", "transport": "qoder_official_sdk"},
    "cn": {"id": "cn", "label": "Qoder CN", "module": "qodercn_agent_sdk",
        "distribution": "qodercn-agent-sdk", "cli": "qoderclicn", "config_env": "QODERCN_CONFIG_DIR",
        "state_dir": "qoder_account_cn", "login_hosts": ("qoder.cn", "qoder.com.cn"),
        "credential_source": "qodercn_official_local_login", "transport": "qodercn_official_sdk"},
}


def edition_settings() -> dict:
    key = os.environ.get("MMF_QODER_ACCOUNT_EDITION", "global").strip().lower()
    if key not in EDITIONS:
        raise ValueError("Qoder版本必须明确选择global或cn，不自动切换。")
    return dict(EDITIONS[key])
