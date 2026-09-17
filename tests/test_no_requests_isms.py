"""requests-era calls that httpx does not have, and that silently shipped.

The README told readers to stream with `iter_lines(decode_unicode=True)`.
httpx's `iter_lines()` takes no arguments, so the documented loop raised
TypeError for anyone who copied it.
"""

import inspect
import json
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent

#: The changelog names the bug to explain it, which is not the same as telling
#: anyone to write it.
HISTORY = {"CHANGELOG.md"}


def sources():
    for pattern in ("*.md", "dify_client/**/*.py", "examples/*.py", ".claude/**/*.md"):
        for path in ROOT.glob(pattern):
            if path.name in HISTORY:
                continue
            yield path, path.read_text(encoding="utf-8")


class TestNoRequestsIsmsSurvive(unittest.TestCase):
    def test_iter_lines_takes_no_arguments(self):
        self.assertEqual(
            list(inspect.signature(httpx.Response.iter_lines).parameters), ["self"]
        )

    def test_nothing_passes_decode_unicode_anywhere(self):
        offenders = [
            str(p.relative_to(ROOT))
            for p, text in sources()
            if "decode_unicode" in text
        ]
        self.assertEqual(offenders, [])

    def test_the_documented_streaming_loop_actually_runs(self):
        response = httpx.Response(
            200, text='data: {"answer": "he"}\n\ndata: {"answer": "llo"}\n\n'
        )
        answer = ""
        for line in response.iter_lines():
            line = line.split("data:", 1)[-1]
            if line.strip():
                answer += json.loads(line.strip()).get("answer", "")
        self.assertEqual(answer, "hello")

    def test_no_requests_only_response_attributes_are_documented(self):
        """`.ok` and `.iter_content` exist on requests responses, not httpx ones."""
        for name in ("iter_content", "ok"):
            self.assertFalse(hasattr(httpx.Response, name), name)
            offenders = [
                str(p.relative_to(ROOT))
                for p, text in sources()
                if f".{name}(" in text or (name == "ok" and ".ok\n" in text)
            ]
            self.assertEqual(offenders, [], name)

    def test_the_package_no_longer_depends_on_requests(self):
        pyproject = (ROOT / "pyproject.toml").read_text()
        dependencies = pyproject.split("dependencies = [")[1].split("]")[0]
        self.assertNotIn("requests", dependencies)
