import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("BEDROCK_MODEL_ID", "test-model-arn")

import agent
import tools


class BuildAgentToolBindingTest(unittest.TestCase):
    """The LLM must never be able to choose whose memory it reads/writes -
    actor_id/session_id are server-bound, not model-fillable parameters."""

    @patch("tools.remember_taste")
    @patch("tools.retrieve_taste")
    def test_bound_tools_forward_the_servers_actor_and_session_not_the_models(
        self, mock_retrieve, mock_remember
    ):
        mock_retrieve.return_value = "prefers size 10, boutique brands"
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")

        recall_fn = built.recall_taste
        recall_fn("running shoes")
        mock_retrieve.assert_called_once_with(actor_id="rashad", query="running shoes")

        built.current_user_message = "I really like minimalist stuff"
        save_fn = built.save_taste
        save_fn("likes minimalist design")
        mock_remember.assert_called_once_with(
            actor_id="rashad",
            session_id="sess-1",
            user_message="I really like minimalist stuff",
            note="likes minimalist design",
        )

    @patch("tools.remember_taste")
    def test_save_taste_falls_back_to_its_own_note_with_no_turn_in_progress(self, mock_remember):
        # current_user_message is only set per-turn by run_chat_job; a direct
        # call (as in a test, or a stray tool call outside a turn) must not crash.
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        built.save_taste("likes minimalist design")
        mock_remember.assert_called_once_with(
            actor_id="rashad",
            session_id="sess-1",
            user_message="likes minimalist design",
            note="likes minimalist design",
        )

    def test_returns_a_strands_agent_with_all_seven_tools_registered(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        self.assertEqual(len(built.agent.tool_names), 7)

    def test_the_schema_shown_to_the_model_never_exposes_actor_or_session_id(self):
        """This is the test that should have caught the real bug: calling the
        bound wrapper directly in Python (as the other test does) bypasses
        whatever schema Strands actually generates for the model - a stray
        @wraps(tools.remember_taste) silently made inspect.signature() (which
        Strands' @tool decorator uses to build inputSchema) follow __wrapped__
        back to the original 3-arg function, so the model was actually being
        told session_id was a parameter it had to supply."""
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        for tool_name in ("recall_taste", "save_taste"):
            properties = built.agent.tool_registry.registry[tool_name].tool_spec["inputSchema"]["json"]["properties"]
            self.assertNotIn("actor_id", properties)
            self.assertNotIn("session_id", properties)
            self.assertNotIn("user_message", properties)


class HandoffTrackingTest(unittest.TestCase):
    """The UI can only reliably show/hide the take-over button if the backend
    tells it, per turn, whether take_control was actually called."""

    def test_awaiting_handoff_defaults_to_false(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        self.assertFalse(built.awaiting_handoff)


class BrowseLoopCapTest(unittest.TestCase):
    """A prompt instruction alone isn't a reliable bound on how many sites
    the model browses in one turn - the tool itself has to refuse once a
    turn has browsed enough."""

    def test_refuses_to_browse_past_the_cap(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        built.browser.browse = MagicMock(return_value=("page text", []))
        browse_tool = built.agent.tool_registry.registry["browse"]._tool_func
        for _ in range(agent.MAX_BROWSE_CALLS_PER_TURN):
            browse_tool(url="https://example-pizzeria.test/")
        self.assertEqual(built.browser.browse.call_count, agent.MAX_BROWSE_CALLS_PER_TURN)
        result = browse_tool(url="https://example-pizzeria.test/")
        self.assertIn("Stop browsing", result)
        self.assertEqual(built.browser.browse.call_count, agent.MAX_BROWSE_CALLS_PER_TURN)


class ActionLoopCapTest(unittest.TestCase):
    """click/fill mirror browse's cap - a model clicking in an unbounded loop
    should be stopped by the tool itself, not just told to via the prompt."""

    def test_click_refuses_past_the_cap(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        built.browser.click = MagicMock(return_value=("clicked", []))
        click_tool = built.agent.tool_registry.registry["click"]._tool_func
        for _ in range(agent.MAX_ACTION_CALLS_PER_TURN):
            click_tool(uid="3")
        self.assertEqual(built.browser.click.call_count, agent.MAX_ACTION_CALLS_PER_TURN)
        result = click_tool(uid="4")
        self.assertIn("Stop", result)
        self.assertEqual(built.browser.click.call_count, agent.MAX_ACTION_CALLS_PER_TURN)

    def test_click_and_fill_share_the_same_cap(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        built.browser.click = MagicMock(return_value=("clicked", []))
        built.browser.fill = MagicMock(return_value=("filled", []))
        click_tool = built.agent.tool_registry.registry["click"]._tool_func
        fill_tool = built.agent.tool_registry.registry["fill"]._tool_func
        for _ in range(agent.MAX_ACTION_CALLS_PER_TURN):
            click_tool(uid="3")
        result = fill_tool(uid="4", value="90210")
        self.assertIn("Stop", result)
        built.browser.fill.assert_not_called()


class ClickAndFillEvidenceTest(unittest.TestCase):
    def test_click_records_the_product_cards_the_post_click_snapshot_found(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        products = [{"name": "Milk", "price": "$3.30", "image": "img", "url": "url"}]
        built.browser.click = MagicMock(return_value=("Added to cart.", products))
        click_tool = built.agent.tool_registry.registry["click"]._tool_func
        click_tool(uid="5")
        self.assertEqual(built.evidence, products)


class EvidenceTrackingTest(unittest.TestCase):
    """The UI showed no proof the agent ever browsed anything - no links, no
    product photos, just prose. Each successful browse() call should record
    the real product cards (photo + link) the UI can render as a carousel."""

    def test_a_successful_browse_records_the_products_it_found(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        products = [
            {"name": "Margherita", "price": "$11.50", "image": "https://example-pizzeria.test/images/margherita.jpg", "url": "https://example-pizzeria.test/#margherita"}
        ]
        built.browser.browse = MagicMock(return_value=("page text", products))
        browse_tool = built.agent.tool_registry.registry["browse"]._tool_func
        browse_tool(url="https://example-pizzeria.test/")
        self.assertEqual(built.evidence, products)

    def test_evidence_accumulates_across_multiple_browse_calls_in_one_turn(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        built.browser.browse = MagicMock(side_effect=[
            ("text", [{"name": "A", "price": "$1", "image": "img-a", "url": "url-a"}]),
            ("text", [{"name": "B", "price": "$2", "image": "img-b", "url": "url-b"}]),
        ])
        browse_tool = built.agent.tool_registry.registry["browse"]._tool_func
        browse_tool(url="https://example-pizzeria.test/")
        browse_tool(url="https://example-pizzeria.test/")
        self.assertEqual(len(built.evidence), 2)
        self.assertEqual(built.evidence[0]["name"], "A")
        self.assertEqual(built.evidence[1]["name"], "B")

    def test_evidence_starts_empty(self):
        built = agent.build_agent(actor_id="rashad", session_id="sess-1")
        self.assertEqual(built.evidence, [])


class PerUserBrowserSessionTest(unittest.TestCase):
    """The actual bug: a module-level browser session shared by every user of
    the process meant one user's browse()/take_control() drove a different
    user's literal browser tab. Each BoundAgent must own its own
    BrowserSession instance."""

    def test_each_bound_agent_gets_its_own_browser_session(self):
        built_a = agent.build_agent(actor_id="alice", session_id="sess-a")
        built_b = agent.build_agent(actor_id="bob", session_id="sess-b")
        self.assertIsInstance(built_a.browser, tools.BrowserSession)
        self.assertIsNot(built_a.browser, built_b.browser)


if __name__ == "__main__":
    unittest.main()
