"""HIVE-341 — tests for the pinned model config + the fixed-helper guard."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import models


class ModelConfigTests(unittest.TestCase):
    def test_pinned_config_loads_and_is_valid(self):
        cfg = models.load()
        self.assertEqual(len(cfg["agent_models"]), 4)
        self.assertIn("anthropic/claude-sonnet-5", models.agent_model_slugs(cfg))

    def test_helper_is_fixed_glm46(self):
        cfg = models.load()
        self.assertEqual(cfg["smart_prompts_rewriter"]["openrouter"], models.FIXED_HELPER)
        self.assertEqual(cfg["reflect_model"]["openrouter"], models.FIXED_HELPER)

    def test_rejects_unpinned_rewriter(self):
        bad = models.load()
        bad["smart_prompts_rewriter"]["openrouter"] = "z-ai/glm-5.2"  # would be a confound
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "models.json"
            p.write_text(json.dumps(bad))
            with self.assertRaises(models.ModelConfigError):
                models.load(p)


if __name__ == "__main__":
    unittest.main()
