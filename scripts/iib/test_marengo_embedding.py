"""
Focused tests for the optional TwelveLabs Marengo embedding backend.

Run from the repo root:

    python -m unittest scripts.iib.test_marengo_embedding

The live test hits the TwelveLabs API and is skipped unless
TWELVELABS_API_KEY is set in the environment.
"""

import os
import sys
import types
import unittest

# Allow running from the repo root without installing the package.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from scripts.iib.marengo_embedding import is_marengo_model, marengo_text_embeddings


class IsMarengoModelTest(unittest.TestCase):
    def test_selects_marengo_models(self):
        self.assertTrue(is_marengo_model("marengo3.0"))
        self.assertTrue(is_marengo_model("Marengo3.0"))
        self.assertTrue(is_marengo_model("  marengo-retrieval-2.7 "))

    def test_ignores_other_models(self):
        self.assertFalse(is_marengo_model("text-embedding-3-small"))
        self.assertFalse(is_marengo_model("nomic-embed-text"))
        self.assertFalse(is_marengo_model(""))
        self.assertFalse(is_marengo_model(None))


class MarengoTextEmbeddingsNoNetworkTest(unittest.TestCase):
    """Verify request wiring and response parsing without hitting the network."""

    def _install_fake_sdk(self, captured):
        class _Seg:
            float_ = [0.1, 0.2, 0.3]

        class _TextEmbedding:
            segments = [_Seg()]

        class _Resp:
            text_embedding = _TextEmbedding()

        class _Embed:
            def create(self, *, model_name, text):
                captured.append((model_name, text))
                return _Resp()

        class _Client:
            def __init__(self, api_key):
                captured.append(("api_key_len", len(api_key)))
                self.embed = _Embed()

        fake = types.ModuleType("twelvelabs")
        fake.TwelveLabs = _Client
        sys.modules["twelvelabs"] = fake

    def setUp(self):
        self._saved = sys.modules.get("twelvelabs")

    def tearDown(self):
        if self._saved is not None:
            sys.modules["twelvelabs"] = self._saved
        else:
            sys.modules.pop("twelvelabs", None)

    def test_returns_one_vector_per_input(self):
        captured = []
        self._install_fake_sdk(captured)
        out = marengo_text_embeddings(
            inputs=["a cat", "a dog"], model="marengo3.0", api_key="secret"
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], [0.1, 0.2, 0.3])
        # Both inputs forwarded with the configured model.
        models = [m for (m, _t) in captured if m == "marengo3.0"]
        self.assertEqual(len(models), 2)

    def test_empty_input_replaced_with_placeholder(self):
        captured = []
        self._install_fake_sdk(captured)
        marengo_text_embeddings(inputs=[""], model="marengo3.0", api_key="secret")
        texts = [t for (m, t) in captured if m == "marengo3.0"]
        self.assertEqual(texts, [" "])


class MarengoTextEmbeddingsLiveTest(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("TWELVELABS_API_KEY"), "TWELVELABS_API_KEY not set"
    )
    def test_real_embedding_has_expected_dim(self):
        out = marengo_text_embeddings(
            inputs=["a cat sitting on a sofa"],
            model="marengo3.0",
            api_key=os.environ["TWELVELABS_API_KEY"],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]), 512)


if __name__ == "__main__":
    unittest.main()
