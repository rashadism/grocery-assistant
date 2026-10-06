import re
import unittest
from pathlib import Path


class ApiRoutingTest(unittest.TestCase):
    def test_api_calls_target_the_backends_gateway_path_not_this_components_own(self):
        """This frontend and the agent backend Component are two separate
        Components under two different gateway path prefixes (same host, so
        no CORS issue) - a relative fetch('api/...') here would wrongly
        resolve against this component's OWN path instead of the backend's."""
        page = Path(__file__).with_name("index.html").read_text()
        paths = re.findall(r"fetch\(['\"]([^'\"]+)['\"]", page)
        self.assertTrue(paths, "expected at least one fetch() call")
        for path in paths:
            self.assertTrue(
                path.startswith("/agent-endpoint-1/"),
                f"{path!r} should be an absolute path under the backend's gateway prefix",
            )

    def test_static_assets_stay_relative_to_this_components_own_path(self):
        """Unlike the API calls, static/dcvjs assets are served by THIS
        component, so they must stay relative (not pinned to the backend)."""
        page = Path(__file__).with_name("index.html").read_text()
        self.assertIn('src="static/dcvjs/dcv.js"', page)
        self.assertIn("dcvWorkerPath = 'static/dcvjs/dcv/'", page)
        self.assertIn("baseUrl: 'static/dcvjs'", page)


if __name__ == "__main__":
    unittest.main()
