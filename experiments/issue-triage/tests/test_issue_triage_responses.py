from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import issue_triage_responses as responses  # noqa: E402


class IssueTriageResponsesTests(unittest.TestCase):
    def test_request_is_context_allowlisted_and_does_not_expose_api_key(self) -> None:
        config = responses.RuntimeConfig(
            model="test-model",
            provider_name="test-provider",
            base_url="https://example.invalid/v1",
            api_key="secret-key",
        )

        body, request_hash = responses.build_request_body(
            config,
            developer_prompt="角色合同",
            task_data={"issue": "事实"},
            schema_name="test_schema",
            schema={"type": "object"},
            reasoning_effort="high",
            max_output_tokens=1_000,
        )

        serialized = json.dumps(body, ensure_ascii=False)
        self.assertFalse(body["store"])
        self.assertEqual(body["tools"], [])
        self.assertNotIn("secret-key", serialized)
        self.assertEqual(len(request_hash), 64)

    def test_runtime_config_loads_only_responses_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                """
model = "test-model"
model_provider = "test-provider"

[model_providers.test-provider]
base_url = "https://example.invalid/v1"
wire_api = "responses"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (home / "auth.json").write_text(
                json.dumps({"OPENAI_API_KEY": "secret-key"}),
                encoding="utf-8",
            )

            config = responses.load_runtime_config(home)

        self.assertEqual(config.model, "test-model")
        self.assertEqual(config.provider_name, "test-provider")
        self.assertEqual(config.api_key, "secret-key")

    def test_output_parser_rejects_multiple_text_results(self) -> None:
        response = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "{}"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "{}"}]},
            ]
        }

        with self.assertRaises(responses.RunnerError):
            responses._extract_output_json(response)


if __name__ == "__main__":
    unittest.main()
