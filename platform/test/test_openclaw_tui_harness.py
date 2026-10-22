"""OpenClaw TUI harness registration and config-injection tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from prepare_config import _FRAMEWORK_LAYOUTS, prepare_local_agent_config


SETTINGS_DIR = REPO_ROOT / "platform" / "settings"


class TestOpenclawTuiHarness(unittest.TestCase):
    def test_platform_metadata_and_layout(self) -> None:
        harnesses = json.loads(
            (SETTINGS_DIR / "harness_config.json").read_text(encoding="utf-8")
        )
        mappings = json.loads(
            (SETTINGS_DIR / "field_mappings.json").read_text(encoding="utf-8")
        )

        meta = harnesses["openclaw-tui"]
        self.assertEqual(meta["agent_config"], "openclaw_tui.json")
        self.assertIn("openclaw-tui", mappings)

        layout = _FRAMEWORK_LAYOUTS["openclaw-tui"]
        self.assertEqual(layout["harness_local_config"], "uploads/openclaw_tui.json")
        self.assertEqual(
            layout["harness_sandbox_config"],
            "/home/ma-user/.openclaw/openclaw.json",
        )
        self.assertEqual(layout["upload_paths"], ["/home/ma-user/.openclaw/agents"])

    def test_template_and_example_match(self) -> None:
        template = json.loads(
            (SETTINGS_DIR / "openclaw_tui.json").read_text(encoding="utf-8")
        )
        example = json.loads(
            (SETTINGS_DIR / "openclaw_tui.json.example").read_text(encoding="utf-8")
        )
        self.assertEqual(template, example)
        self.assertEqual(
            template["agents"]["defaults"]["model"]["primary"],
            "local/replace-with-model-id",
        )

    def test_prepare_config_uses_openclaw_injection_for_tui(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            local_config = temp / "openclaw_tui.json"
            model_config = temp / "user_proxy_model.json"
            data_config = temp / "task.json"
            local_config.write_text(
                (SETTINGS_DIR / "openclaw_tui.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            model_config.write_text(
                json.dumps(
                    {
                        "main": {
                            "model": "main-model",
                            "provider": "main-provider",
                            "base_url": "https://main.example/v1",
                            "api_key": "main-key",
                            "api": "openai-completions",
                        },
                        "evaluator": {
                            "model": "eval-model",
                            "provider": "eval-provider",
                            "base_url": "https://eval.example",
                            "api_key": "eval-key",
                            "api": "anthropic-messages",
                        },
                        "user_simulator": {
                            "model": "sim-model",
                            "provider": "sim-provider",
                            "base_url": "https://sim.example/v1",
                            "api_key": "sim-key",
                        },
                    }
                ),
                encoding="utf-8",
            )
            data_config.write_text(
                json.dumps({"agents": [{"name": "main"}, {"name": "reviewer"}]}),
                encoding="utf-8",
            )

            prepare_local_agent_config(
                "openclaw-tui",
                str(data_config),
                str(local_config),
                str(model_config),
                home="/home/ma-user",
                sandbox_type="x86_cpu",
                oc_agent_names=["main", "reviewer"],
            )

            result = json.loads(local_config.read_text(encoding="utf-8"))
            self.assertIn("main-provider", result["models"]["providers"])
            self.assertIn("eval-provider", result["models"]["providers"])
            self.assertNotIn("sim-provider", result["models"]["providers"])

            agents = {entry["id"]: entry for entry in result["agents"]["list"]}
            self.assertEqual(
                agents["main"]["model"]["primary"], "main-provider/main-model"
            )
            self.assertEqual(
                agents["evaluator"]["model"]["primary"],
                "eval-provider/eval-model",
            )
            self.assertEqual(
                agents["reviewer"]["workspace"],
                "/home/ma-user/.openclaw/workspace-reviewer",
            )
            self.assertEqual(
                agents["reviewer"]["agentDir"],
                "/home/ma-user/.openclaw/agents/reviewer",
            )


if __name__ == "__main__":
    unittest.main()
