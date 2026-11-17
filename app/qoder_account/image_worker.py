"""One Qoder CN ImageGen operation inside a dedicated empty workspace."""
import asyncio
import json
import sys
from pathlib import Path

from qodercn_agent_sdk import (
    AssistantMessage, PermissionResultAllow, PermissionResultDeny, QoderAgentOptions,
    QoderSDKClient, ResultMessage, UserMessage, qodercli_auth,
)
from worker import runtime_info


async def generate(payload):
    root = Path.cwd().resolve()
    invoked = set()
    images = []

    async def allow_image(name, params, context):
        if name == "ImageGen":
            return PermissionResultAllow(updated_input=params)
        return PermissionResultDeny(message="本会话仅允许图像生成。")

    options = QoderAgentOptions(
        auth=qodercli_auth(), cli_path=runtime_info()["cli_path"], cwd=str(root),
        model=payload["model"], tools=["ImageGen"], allowed_tools=["ImageGen"],
        can_use_tool=allow_image, permission_mode="default", setting_sources=[],
        skills=[], strict_mcp_config=True, mcp_servers={}, max_turns=2,
        stderr=lambda _: None, extra_args={"max-model-request-retries": "0"},
    )
    async with QoderSDKClient(options) as client:
        await client.query(payload["prompt"])
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if getattr(block, "name", None) == "ImageGen":
                        invoked.add(block.id)
            if isinstance(message, UserMessage) and isinstance(message.content, list):
                for block in message.content:
                    if getattr(block, "tool_use_id", None) not in invoked or getattr(block, "is_error", False):
                        continue
                    content = getattr(block, "content", "")
                    if not isinstance(content, str):
                        continue
                    for line in content.splitlines():
                        _, marker, filename = line.partition("Saved to:")
                        if marker:
                            path = Path(filename.strip()).resolve()
                            if path.parent == root / "vibe_images" and path.is_file():
                                images.append(path.relative_to(root).as_posix())
            if isinstance(message, ResultMessage):
                if not images:
                    raise RuntimeError("QODER_IMAGE_NOT_RETURNED")
                print(json.dumps({"image": images[0], "credits": message.total_credits,
                                  "cost_usd": message.total_cost_usd}), flush=True)
                return
    raise RuntimeError("QODER_IMAGE_NO_TERMINAL")


if __name__ == "__main__":
    try:
        asyncio.run(generate(json.load(sys.stdin)))
    except Exception:
        sys.exit(1)
