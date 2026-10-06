import unittest
from unittest.mock import MagicMock, patch

import tools


class IdFromArnTest(unittest.TestCase):
    def test_extracts_the_bare_id_from_a_memory_arn(self):
        arn = "arn:aws:bedrock-agentcore:us-east-1:237632677974:memory/development_memory-GTvt8c4pgU"
        self.assertEqual(tools._id_from_arn(arn), "development_memory-GTvt8c4pgU")

    def test_extracts_the_bare_id_from_a_browser_arn(self):
        arn = "arn:aws:bedrock-agentcore:us-east-1:237632677974:browser-custom/development_browser-bSq6jgobeT"
        self.assertEqual(tools._id_from_arn(arn), "development_browser-bSq6jgobeT")

    def test_passes_through_a_value_that_is_already_a_bare_id(self):
        self.assertEqual(tools._id_from_arn("development_memory-GTvt8c4pgU"), "development_memory-GTvt8c4pgU")


class RetrieveTasteTest(unittest.TestCase):
    @patch("tools.client")
    def test_searches_the_actors_taste_namespace(self, mock_client):
        mock_client.retrieve_memory_records.return_value = {
            "memoryRecordSummaries": [
                {"content": {"text": '{"situation": "asked for running shoes", "assessment": "prefers true-to-size, budget under $100"}'}}
            ]
        }
        result = tools.retrieve_taste(actor_id="rashad", query="running shoes")
        mock_client.retrieve_memory_records.assert_called_once()
        _, kwargs = mock_client.retrieve_memory_records.call_args
        self.assertEqual(kwargs["namespace"], "taste/rashad/")
        self.assertEqual(kwargs["searchCriteria"]["searchQuery"], "running shoes")
        self.assertIn("true-to-size", result)

    @patch("tools.client")
    def test_returns_a_plain_message_when_nothing_is_found(self, mock_client):
        mock_client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}
        result = tools.retrieve_taste(actor_id="rashad", query="anything")
        self.assertIn("No", result)


class RememberTasteTest(unittest.TestCase):
    @patch("tools.client")
    def test_writes_an_event_to_the_actors_session(self, mock_client):
        mock_client.create_event.return_value = {"event": {"eventId": "evt-1"}}
        tools.remember_taste(
            actor_id="rashad",
            session_id="sess-1",
            user_message="I only ever stay at boutique hotels",
            note="prefers boutique hotels",
        )
        mock_client.create_event.assert_called_once()
        _, kwargs = mock_client.create_event.call_args
        self.assertEqual(kwargs["actorId"], "rashad")
        self.assertEqual(kwargs["sessionId"], "sess-1")

    @patch("tools.client")
    def test_writes_a_real_user_assistant_pair_not_a_lone_assistant_note(self, mock_client):
        # AgentCore's memory extraction only pulls facts from a real
        # USER/ASSISTANT exchange, not a lone ASSISTANT-authored note.
        mock_client.create_event.return_value = {"event": {"eventId": "evt-1"}}
        tools.remember_taste(
            actor_id="rashad",
            session_id="sess-1",
            user_message="I only ever stay at boutique hotels",
            note="prefers boutique hotels",
        )
        _, kwargs = mock_client.create_event.call_args
        roles = [item["conversational"]["role"] for item in kwargs["payload"]]
        self.assertEqual(roles, ["USER", "ASSISTANT"])
        self.assertEqual(
            kwargs["payload"][0]["conversational"]["content"]["text"],
            "I only ever stay at boutique hotels",
        )
        self.assertEqual(
            kwargs["payload"][1]["conversational"]["content"]["text"], "prefers boutique hotels"
        )


class RenderSnapshotTest(unittest.TestCase):
    """No pairing between a product and "its" button - different sites (and
    different retailers on the same site) shape that control differently,
    so the model matches a product to an element by label itself."""

    def test_products_carry_no_action_uid(self):
        text = tools._render_snapshot({
            "products": [{"name": "Milk", "price": "$3.30", "image": "img", "url": "url"}],
            "elements": [],
        })
        self.assertIn("[Milk](url) - $3.30", text)
        self.assertNotIn("click(", text)

    def test_elements_are_listed_with_their_own_real_label(self):
        text = tools._render_snapshot({
            "products": [],
            "elements": [{"uid": "9", "role": "button", "label": "Add 1 ct Lucerne Whole Milk"}],
        })
        self.assertIn('click(9): button "Add 1 ct Lucerne Whole Milk"', text)

    def test_an_input_element_is_offered_as_fill_not_click(self):
        text = tools._render_snapshot({
            "products": [],
            "elements": [{"uid": "4", "role": "input", "label": "Search"}],
        })
        self.assertIn('fill(4): input "Search"', text)

    def test_empty_snapshot_returns_none(self):
        self.assertIsNone(tools._render_snapshot({"products": [], "elements": []}))


class BrowserSessionTest(unittest.TestCase):
    """BrowserSession is instantiated once per app user (see agent.BoundAgent)
    - never a module-level global. Sharing one AgentCore browser session
    across every user of the process would mean one user's browse()/
    take_control() drives a different user's literal browser tab."""

    def setUp(self):
        self.browser = tools.BrowserSession()

        sync_pw_patcher = patch("tools.sync_playwright")
        self.mock_sync_playwright = sync_pw_patcher.start()
        self.addCleanup(sync_pw_patcher.stop)
        mock_browser = self.mock_sync_playwright.return_value.start.return_value.chromium.connect_over_cdp.return_value
        mock_browser.contexts = []
        mock_browser.new_context.return_value.pages = []
        self.mock_page = mock_browser.new_context.return_value.new_page.return_value

        bc_patcher = patch("tools.BrowserClient")
        self.mock_bc_cls = bc_patcher.start()
        self.addCleanup(bc_patcher.stop)
        self.mock_bc = self.mock_bc_cls.return_value
        self.mock_bc.generate_ws_headers.return_value = ("wss://fake", {})

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_starts_a_session_then_fetches_the_page(self, mock_client, mock_snapshot):
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        mock_snapshot.return_value = ("Running Shoes - $89.99 - In Stock", [], set())
        text, products = self.browser.browse("https://example.com/shoes")
        mock_client.start_browser_session.assert_called_once_with(
            browserIdentifier=tools.BROWSER_ID, sessionTimeoutSeconds=900
        )
        mock_snapshot.assert_called_once_with(self.mock_page, "https://example.com/shoes")
        self.assertEqual(text, "Running Shoes - $89.99 - In Stock")
        self.assertEqual(self.browser.session_id, "sess-123")

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_a_fresh_session_has_its_automation_stream_enabled(self, mock_client, mock_snapshot):
        """A brand-new session's automation stream starts DISABLED - confirmed
        live via get_browser_session - so the first browse() always 403s
        unless something explicitly enables it right after creation."""
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        mock_snapshot.return_value = ("text", [], set())
        self.browser.browse("https://example.com/shoes")
        self.mock_bc.release_control.assert_called_once()

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_returns_the_real_product_cards_found(self, mock_client, mock_snapshot):
        """The UI needs real proof the agent actually browsed - a product's
        real photo and a link to that exact product, not a screenshot of the
        whole page."""
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        products = [{
            "name": "Margherita",
            "price": "$11.50",
            "image": "https://example-pizzeria.test/images/margherita.jpg",
            "url": "https://example-pizzeria.test/#margherita",
        }]
        mock_snapshot.return_value = ("page text", products, set())
        text, result_products = self.browser.browse("https://example-pizzeria.test/")
        self.assertEqual(result_products, products)

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_reuses_an_already_started_session(self, mock_client, mock_snapshot):
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        mock_snapshot.return_value = ("page one", [], set())
        self.browser.browse("https://example.com/one")
        mock_snapshot.return_value = ("page two", [], set())
        self.browser.browse("https://example.com/two")
        mock_client.start_browser_session.assert_called_once()
        self.assertEqual(mock_snapshot.call_count, 2)

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_reuses_the_same_page_across_calls(self, mock_client, mock_snapshot):
        """The whole point of the fix: one Playwright connection, and one
        tab, for as long as the session lasts - so a take_control() in a
        later turn still shows the page the agent was just looking at,
        instead of a reconnect resetting it to a blank tab."""
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        mock_snapshot.return_value = ("page one", [], set())
        self.browser.browse("https://example.com/one")
        self.browser.browse("https://example.com/two")
        pages_used = [call.args[0] for call in mock_snapshot.call_args_list]
        self.assertEqual(pages_used, [self.mock_page, self.mock_page])
        self.mock_sync_playwright.return_value.start.assert_called_once()

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_retries_on_a_fresh_session_when_it_has_expired(self, mock_client, mock_snapshot):
        """Seen live: the session hits its 15-minute timeout and every
        browse() after that failed forever, because session_id was never
        cleared. One retry against a brand-new session fixes this."""
        mock_client.start_browser_session.side_effect = [{"sessionId": "sess-old"}, {"sessionId": "sess-new"}]
        mock_snapshot.side_effect = [Exception("Session has expired"), ("fresh page", [], set())]
        text, products = self.browser.browse("https://example.com/shoes")
        self.assertEqual(text, "fresh page")
        self.assertEqual(mock_client.start_browser_session.call_count, 2)
        mock_snapshot.assert_called_with(self.mock_page, "https://example.com/shoes")

    @patch("tools._page_snapshot")
    def test_two_browser_sessions_are_fully_independent(self, mock_snapshot):
        """The actual bug this fixes: two instances (i.e. two app users) must
        never share state - one user's session_id should never leak into or
        get clobbered by another's."""
        with patch("tools.client") as mock_client:
            mock_client.start_browser_session.side_effect = [{"sessionId": "sess-a"}, {"sessionId": "sess-b"}]
            mock_snapshot.return_value = ("text", [], set())
            browser_a = tools.BrowserSession()
            browser_b = tools.BrowserSession()
            browser_a.browse("https://example.com/a")
            browser_b.browse("https://example.com/b")
            self.assertEqual(browser_a.session_id, "sess-a")
            self.assertEqual(browser_b.session_id, "sess-b")

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_click_resolves_a_uid_from_the_latest_snapshot(self, mock_client, mock_snapshot):
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        mock_snapshot.return_value = ("Products found on this page:\n1. [Milk](url) - $3.30\nClickable or fillable things on this page:\n- click(5): button \"Add 1 ct Milk\"", [], {"5"})
        self.browser.browse("https://example.com/search")
        mock_snapshot.return_value = ("Added to cart.", [], set())
        text, _ = self.browser.click("5")
        self.mock_page.locator.assert_called_with('[data-agent-uid="5"]')
        self.mock_page.locator.return_value.click.assert_called_once()
        self.assertEqual(text, "Added to cart.")

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_click_rejects_a_uid_not_in_the_latest_snapshot(self, mock_client, mock_snapshot):
        """A uid from an earlier page (or one the DOM already replaced) must
        fail clearly, not silently click whatever that selector now matches
        or raise an opaque Playwright error."""
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        mock_snapshot.return_value = ("text", [], {"1", "2"})
        self.browser.browse("https://example.com/search")
        with self.assertRaises(ValueError):
            self.browser.click("99")
        self.mock_page.locator.assert_not_called()

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_click_failure_propagates_instead_of_being_swallowed(self, mock_client, mock_snapshot):
        """Seen live: a click on a real but unclickable element (covered by
        a banner, mid-hydration) must still reach Strands as an error so the
        model sees it failed - not disappear silently."""
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        mock_snapshot.return_value = ("text", [], {"5"})
        self.browser.browse("https://example.com/search")
        self.mock_page.locator.return_value.click.side_effect = Exception("Timeout 10000ms exceeded")
        with self.assertRaises(Exception):
            self.browser.click("5")

    @patch("tools._page_snapshot")
    @patch("tools.client")
    def test_fill_resolves_a_uid_from_the_latest_snapshot(self, mock_client, mock_snapshot):
        mock_client.start_browser_session.return_value = {"sessionId": "sess-123"}
        mock_snapshot.return_value = ("text", [], {"7"})
        self.browser.browse("https://example.com/search")
        mock_snapshot.return_value = ("filled", [], set())
        text, _ = self.browser.fill("7", "90210")
        self.mock_page.locator.assert_called_with('[data-agent-uid="7"]')
        self.mock_page.locator.return_value.fill.assert_called_once_with("90210", timeout=10000)
        self.assertEqual(text, "filled")

    def test_take_control_disables_the_automation_stream(self):
        self.browser.session_id = "sess-123"
        self.browser.take_control()
        self.mock_bc.take_control.assert_called_once()

    def test_release_control_re_enables_the_automation_stream(self):
        self.browser.session_id = "sess-123"
        self.browser.release_control()
        self.mock_bc.release_control.assert_called_once()


if __name__ == "__main__":
    unittest.main()
