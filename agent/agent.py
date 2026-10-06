"""Builds a per-request grocery assistant Agent.

actor_id/session_id are server-bound into closures over the tool functions -
never exposed as LLM-fillable parameters, so the model can't choose whose
memory it reads or writes. Only the LLM-relevant arguments (query, note, url)
are part of each tool's signature.
"""

import os
from dataclasses import dataclass, field

from strands import Agent, tool
from strands.models import BedrockModel

import tools

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# A hard cap, not just a prompt instruction - prompt wording alone isn't a
# reliable bound on model behavior. 5 allows a multi-item request plus one
# retry per item without letting the turn run away.
MAX_BROWSE_CALLS_PER_TURN = 5
# click/fill are cheap DOM actions, not page loads, so this allows adding
# several items in one turn while still bounding runaway clicking.
MAX_ACTION_CALLS_PER_TURN = 8

SYSTEM_PROMPT = """\
You are a personal grocery shopping assistant for Instacart. Each turn, give the user concrete picks, item, price, store, a one-line reason each, based on what they asked for and their remembered taste. Do what they ask on the page yourself instead of describing what they could do. Let the count follow the request, a specific ask may have one real match, a broad one several. Never pad a list to hit a target number.

If the request is missing a detail that would change what you look for, a quantity, a brand, a preference recall_taste didn't cover, ask one quick question before browsing instead of guessing.

Before researching, check the user's remembered taste with recall_taste (dietary preferences, favorite brands, budget habits). Search and compare real prices live with browse on instacart.com, always on Instacart, never another retailer. Use exactly this URL shape to search: https://www.instacart.com/store/s?k=<query> (e.g. https://www.instacart.com/store/s?k=taco+shells). Other URL shapes 404. One good search is usually enough. Stop browsing and act or answer as soon as you have enough, rather than comparing exhaustively.

browse/click/fill results come back as a list of products, each already formatted as a markdown link, [Product Name](url), plus every other clickable or fillable thing on the page, each with its own uid and its own real label, whatever that control is actually called there. Only recommend or act on products you actually saw on the page, never invent one, and keep a product's exact markdown link around its name when you recommend it, so it stays clickable. A uid is only valid against the single most recent browse/click/fill result. If click or fill says a uid isn't on the current page, call browse again and use a fresh one.

To add an item to the cart, find and click its own add-to-cart control right there on the search results. Only open the product's own page if the search results truly have no way to add it. Product pages are where a sign-in wall is most likely to show up, search results themselves usually don't need one. An item is only added to the cart once you've actually clicked its add-to-cart control and the click succeeded. Never say "added to cart" for something you only found or recommended. If you ran out of turns before clicking it, say exactly that.

If the page shows a sign-in, sign-up, or any other modal asking for an account, personal details, or payment, call take_control immediately. Tell the user exactly what the page is asking for, in plain terms a shopper would use, never uids, tool names, or your own reasoning about why you couldn't click something. Recognize the blocker and hand off right away rather than trying several clicks first. Once control is handed off, the user is driving the browser directly, not talking to you. Tell them to finish that step themselves, then click "I'm done, continue" in the Live Browser panel, never to click a page control like "Add to cart", that's your job once you're back in control. Only call release_control once they've confirmed here that they clicked it. Never call take_control just to add an item to the cart or click something you already have a uid for, and never claim to complete an order yourself.

When you learn something worth remembering about the user's taste, call save_taste.
"""


@dataclass
class BoundAgent:
    agent: Agent
    recall_taste: callable
    save_taste: callable
    # One real AgentCore browser session per user; see tools.BrowserSession.
    browser: "tools.BrowserSession" = field(default_factory=tools.BrowserSession)
    # save_taste needs the raw turn text too - AgentCore's semantic strategy
    # only extracts facts from USER/ASSISTANT pairs, not a lone note.
    current_user_message: str | None = None
    awaiting_handoff: bool = False
    # Human-readable label for the tool call in flight, polled by the
    # frontend so "Thinking..." can show real progress.
    current_action: str | None = None
    browse_count: int = 0
    action_count: int = 0
    # Product cards found across this turn's browse() calls, shown in the
    # UI alongside the text response.
    evidence: list = field(default_factory=list)


def build_agent(actor_id: str, session_id: str) -> BoundAgent:
    bound = BoundAgent(agent=None, recall_taste=None, save_taste=None)

    @tool
    def recall_taste(query: str) -> str:
        """Recall this user's remembered taste/preferences relevant to a query."""
        bound.current_action = f"Recalling what you like about “{query}”…"
        return tools.retrieve_taste(actor_id=actor_id, query=query)

    @tool
    def save_taste(note: str) -> str:
        """Save something learned about this user's taste for next time."""
        bound.current_action = "Saving a preference…"
        return tools.remember_taste(
            actor_id=actor_id,
            session_id=session_id,
            user_message=bound.current_user_message or note,
            note=note,
        )

    @tool
    def browse(url: str) -> str:
        """Navigate to a URL in the live browser and return the visible page text."""
        bound.browse_count += 1
        if bound.browse_count > MAX_BROWSE_CALLS_PER_TURN:
            return (
                "You've already browsed several pages this turn. Stop browsing. "
                "If you haven't actually clicked an add-to-cart control yet, you "
                "haven't added anything - tell the user what you found and that "
                "you still need to add it, never say you added something you "
                "only looked at."
            )
        bound.current_action = f"Browsing {url}…"
        text, products = bound.browser.browse(url)
        bound.evidence.extend(products)
        return text

    @tool
    def click(uid: str) -> str:
        """Click a button or link on the current page by its uid from the most recent browse/click/fill result. Use this to add an item to the cart or press any other control yourself - never ask the user to click something you can click."""
        bound.action_count += 1
        if bound.action_count > MAX_ACTION_CALLS_PER_TURN:
            return "You've already clicked several things this turn. Stop and give the user your best answer now based on what you've done so far."
        bound.current_action = "Clicking…"
        text, products = bound.browser.click(uid)
        bound.evidence.extend(products)
        return text

    @tool
    def fill(uid: str, value: str) -> str:
        """Type text into an input on the current page by its uid from the most recent browse/click/fill result."""
        bound.action_count += 1
        if bound.action_count > MAX_ACTION_CALLS_PER_TURN:
            return "You've already clicked several things this turn. Stop and give the user your best answer now based on what you've done so far."
        bound.current_action = "Filling in a field…"
        text, products = bound.browser.fill(uid, value)
        bound.evidence.extend(products)
        return text

    @tool
    def take_control() -> str:
        """Pause automation and hand the live browser session to the user."""
        bound.current_action = "Handing the browser over to you…"
        result = bound.browser.take_control()
        bound.awaiting_handoff = True
        return result

    @tool
    def release_control() -> str:
        """Resume automated control of the browser session."""
        bound.current_action = "Resuming automated browsing…"
        result = bound.browser.release_control()
        bound.awaiting_handoff = False
        return result

    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    bound.agent = Agent(
        model=model,
        tools=[recall_taste, save_taste, browse, click, fill, take_control, release_control],
        system_prompt=SYSTEM_PROMPT,
    )
    bound.recall_taste = recall_taste
    bound.save_taste = save_taste
    return bound
