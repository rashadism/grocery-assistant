"""AgentCore Memory and Browser tools for the grocery assistant agent.

Memory: plain boto3 calls against the bedrock-agentcore data plane,
semantic search via retrieve_memory_records, no SDK-level session manager.

Browser: plain boto3 for session lifecycle (control_client), the
`bedrock_agentcore` SDK only for SigV4 WebSocket header signing on the
Playwright/CDP connection.
"""

import concurrent.futures
import os
import re
from datetime import datetime, timezone

import boto3
from bedrock_agentcore.tools.browser_client import BrowserClient
from playwright.sync_api import sync_playwright

def _id_from_arn(value: str) -> str:
    """bedrock-agentcore APIs want the bare resource id, not the full ARN.
    An ARN is a syntactically valid identifier but fails authorization
    regardless of policy."""
    return value.rsplit("/", 1)[-1]


REGION = os.environ.get("AWS_REGION", "us-east-1")
MEMORY_ID = _id_from_arn(os.environ.get("MEMORY_ID", ""))
BROWSER_ID = _id_from_arn(os.environ.get("BROWSER_ID", ""))

# Instacart defaults the delivery zip to wherever the browser's IP
# geolocates. AgentCore's remote browser runs in us-east-1 and lands on
# 20147 (Ashburn, VA), which has no inline "Add to cart" buttons on search
# results, forcing product-page visits that have no add-to-cart control
# either. This address resolves to 94105 (San Francisco), which has inline
# add buttons on every store shown (Safeway, Walmart, Costco, ...), so pin
# it once per session.
DELIVERY_ADDRESS = "1 Market St, San Francisco, CA 94105"

client = boto3.client("bedrock-agentcore", region_name=REGION)


def _browser_client_for(session_id: str) -> BrowserClient:
    browser_client = BrowserClient(region=REGION)
    browser_client.identifier = BROWSER_ID
    browser_client.session_id = session_id
    return browser_client


class BrowserSession:
    """One real AgentCore browser session, owned by exactly one app user.

    Must be instantiated per user (see agent.BoundAgent), never held as a
    module-level global - a shared global here means every user of the
    process drives the same literal browser tab: one user's browse() moves
    what another user is looking at, and one user's take_control() hands
    *their* session to a completely different person.
    """

    def __init__(self):
        self.session_id: str | None = None
        # Playwright's sync API is thread-affine, but each chat turn runs in
        # its own thread (see app.py's run_chat_job). A fresh CDP connection
        # per browse() call used to disconnect at the end of every turn -
        # and AgentCore resets the tab to a blank page on disconnect, so a
        # take_control() in the *next* turn landed the human on a blank tab,
        # not the page the agent had actually been looking at. Pinning every
        # Playwright call to one dedicated thread lets the connection (and
        # the tab) survive across turns, right up to the handoff.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._playwright = None
        self._browser = None
        self._page = None
        # uids minted by the most recent browse()/click()/fill() - click()
        # and fill() only accept a uid still valid against what the model
        # most recently saw, so a stale one (an earlier page, an element the
        # DOM already replaced) fails with a clear message instead of
        # silently acting on the wrong element or a dead handle.
        self._valid_uids: set[str] = set()

    def _get_or_create(self) -> str:
        if self.session_id is None:
            resp = client.start_browser_session(browserIdentifier=BROWSER_ID, sessionTimeoutSeconds=900)
            session_id = resp["sessionId"]
            # A fresh session's automation stream starts DISABLED. Without
            # this, the first browse() call 403s with "Stream is disabled
            # for this session".
            _browser_client_for(session_id).release_control()
            self.session_id = session_id
        return self.session_id

    def take_control(self) -> str:
        """Hand control of the live browser session to a human (pauses automation)."""
        _browser_client_for(self._get_or_create()).take_control()
        return "Control handed to the user."

    def release_control(self) -> str:
        """Resume automated control of the browser session."""
        _browser_client_for(self._get_or_create()).release_control()
        return "Automation resumed."

    def browse(self, url: str) -> tuple[str, list[dict]]:
        """Navigate to a URL and return (visible text, product cards found)."""
        return self._executor.submit(self._browse_on_worker_thread, url).result()

    def _browse_on_worker_thread(self, url: str) -> tuple[str, list[dict]]:
        try:
            page = self._ensure_page()
            return self._snapshot(page, url)
        except Exception as exc:
            # Strands catches tool exceptions and feeds the message back to
            # the model instead of crashing the turn, so without this print
            # the actual failure (timeout, CDP disconnect, session expiry,
            # ...) never reaches the pod logs - only the model's paraphrase
            # of it does.
            print(f"browse({url!r}) failed: {type(exc).__name__}: {exc}", flush=True)
            # A session has a 15-minute hard timeout. Retry once against a
            # brand-new session before giving up.
            self._drop_connection()
            self.session_id = None
            try:
                page = self._ensure_page()
                return self._snapshot(page, url)
            except Exception as retry_exc:
                print(f"browse({url!r}) retry on a fresh session also failed: {type(retry_exc).__name__}: {retry_exc}", flush=True)
                raise

    def click(self, uid: str) -> tuple[str, list[dict]]:
        """Click an element by its uid from the latest snapshot and return
        the page afterward (visible text, product cards found)."""
        return self._executor.submit(self._click_on_worker_thread, uid).result()

    def _click_on_worker_thread(self, uid: str) -> tuple[str, list[dict]]:
        self._require_valid_uid(uid)
        page = self._ensure_page()
        try:
            page.locator(f'[data-agent-uid="{uid}"]').click(timeout=10000)
        except Exception as exc:
            print(f"click({uid!r}) failed: {type(exc).__name__}: {exc}", flush=True)
            raise
        return self._snapshot(page, None)

    def fill(self, uid: str, value: str) -> tuple[str, list[dict]]:
        """Fill an input by its uid from the latest snapshot and return the
        page afterward (visible text, product cards found)."""
        return self._executor.submit(self._fill_on_worker_thread, uid, value).result()

    def _fill_on_worker_thread(self, uid: str, value: str) -> tuple[str, list[dict]]:
        self._require_valid_uid(uid)
        page = self._ensure_page()
        try:
            page.locator(f'[data-agent-uid="{uid}"]').fill(value, timeout=10000)
        except Exception as exc:
            print(f"fill({uid!r}) failed: {type(exc).__name__}: {exc}", flush=True)
            raise
        return self._snapshot(page, None)

    def _require_valid_uid(self, uid: str) -> None:
        if uid not in self._valid_uids:
            raise ValueError(
                f"Element {uid} isn't on the current page - call browse() again and use a uid from its latest result."
            )

    def _snapshot(self, page, url: str | None) -> tuple[str, list[dict]]:
        """Navigate if a url is given, else re-scan the page as it stands
        after a click/fill, and remember which uids are valid against this
        exact snapshot (see _valid_uids)."""
        text, products, valid_uids = _page_snapshot(page, url)
        self._valid_uids = valid_uids
        return text, products

    def _ensure_page(self):
        """Connect once, on this session's dedicated thread, and reuse the
        same tab for every browse() call until the session is replaced."""
        if self._page is not None:
            return self._page
        browser_client = _browser_client_for(self._get_or_create())
        ws_url, headers = browser_client.generate_ws_headers()
        self._playwright = sync_playwright().start()
        # No timeout here can hang indefinitely on a slow/stuck CDP
        # handshake, with no error raised even though the session stays
        # healthy and READY on AWS's side. 30s matches the other connection
        # timeouts.
        self._browser = self._playwright.chromium.connect_over_cdp(ws_url, headers=headers, timeout=30000)
        context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self._page = context.pages[0] if context.pages else context.new_page()
        self._set_delivery_address(self._page)
        return self._page

    def _set_delivery_address(self, page) -> None:
        """One-time per session, before the model ever calls browse() - see
        DELIVERY_ADDRESS. Best-effort: if Instacart's address picker ever
        changes shape, fall back silently to whatever zip the session would
        have defaulted to rather than failing the whole session."""
        try:
            # The bare /store collection page forces a login redirect on
            # AgentCore's browser, even though it loads as a normal guest
            # page locally. A /store/s search URL, the same shape every
            # browse() call already uses, doesn't hit that redirect, so
            # start there.
            page.goto("https://www.instacart.com/store/s?k=milk", wait_until="domcontentloaded", timeout=20000)
            page.get_by_role("button", name=re.compile("address", re.I)).first.click(timeout=20000)
            page.get_by_role("textbox", name="Enter your address").fill(DELIVERY_ADDRESS, timeout=20000)
            page.get_by_role("button", name="1 Market Street San Francisco, CA 94105").click(timeout=20000)
            page.get_by_role("button", name="Save Address").click(timeout=20000)
            page.wait_for_timeout(1500)
        except Exception as exc:
            print(f"_set_delivery_address failed, continuing with default address: {type(exc).__name__}: {exc}", flush=True)
            try:
                print(f"_set_delivery_address debug: url={page.url!r} title={page.title()!r} body={page.inner_text('body')[:500]!r}", flush=True)
            except Exception as debug_exc:
                print(f"_set_delivery_address debug capture also failed: {type(debug_exc).__name__}: {debug_exc}", flush=True)

    def _drop_connection(self):
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._page = None
        # Without this, every dropped connection orphans its AWS-side
        # session (left READY until the 15-minute timeout). Enough orphaned
        # sessions make a later start_browser_session call hang indefinitely
        # under AgentCore's per-identifier concurrency cap. Best-effort - a
        # failed stop here shouldn't block the retry already in progress.
        if self.session_id is not None:
            try:
                client.stop_browser_session(browserIdentifier=BROWSER_ID, sessionId=self.session_id)
            except Exception as exc:
                print(f"stop_browser_session({self.session_id!r}) failed, leaving it to time out: {type(exc).__name__}: {exc}", flush=True)


_EXTRACT_SNAPSHOT_JS = """
(limit) => {
  // Kept on window, not a local var, so it survives across repeated
  // snapshots of the same loaded document (e.g. the re-scan after a
  // click()/fill() that doesn't navigate). A counter that reset on every
  // evaluate() call would let two different snapshots assign the same uid
  // to two different elements, which breaks a locator that expects exactly
  // one match. A real navigation creates a fresh window, so this still
  // resets exactly when uids should.
  window.__agentUidCounter = window.__agentUidCounter || 0;
  const stamp = (el) => {
    const uid = String(++window.__agentUidCounter);
    el.setAttribute('data-agent-uid', uid);
    return uid;
  };
  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    // A background element covered by an open modal still has a nonzero
    // bounding box, so size alone isn't enough: a click on it would time
    // out because the modal intercepts pointer events at that exact spot.
    // elementFromPoint at the element's own center tells us what's
    // actually on top there.
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const topEl = document.elementFromPoint(cx, cy);
    return !!topEl && (topEl === el || el.contains(topEl) || topEl.contains(el));
  };
  const labelFor = (el) => {
    const aria = (el.getAttribute('aria-label') || '').trim();
    if (aria) return aria;
    const text = (el.innerText || el.value || el.getAttribute('placeholder') || '').trim();
    return text.replace(/\\s+/g, ' ').slice(0, 80);
  };

  const products = [];
  const elements = [];
  const seen = new Set();

  // Real photos and real per-product links for the UI's evidence carousel -
  // a presentational concern, kept separate from what's clickable. This
  // card shape (an anchor wrapping an image, a price nearby) isn't every
  // retailer's - sites that render a product as a bare button with no
  // dereferenceable URL just won't surface here, and that's fine: they
  // still show up as clickable elements below.
  for (const img of document.querySelectorAll('img[alt]')) {
    if (products.length >= limit) break;
    const alt = (img.getAttribute('alt') || '').trim();
    const src = img.getAttribute('src') || '';
    const altLower = alt.toLowerCase();
    if (!alt || !src || altLower.includes('logo') || altLower.includes('sponsored') || altLower.includes('advertisement')) continue;

    let anchor = null;
    let node = img;
    for (let i = 0; i < 6; i++) {
      node = node.parentElement;
      if (!node) break;
      if (node.tagName === 'A') { anchor = node; break; }
    }
    if (!anchor) continue;
    const href = anchor.getAttribute('href') || '';
    if (!href) continue;
    const url = new URL(href, window.location.href).href;
    if (seen.has(url)) continue;

    let card = anchor;
    for (let i = 0; i < 3; i++) {
      if (!card.parentElement) break;
      card = card.parentElement;
    }
    const match = (card.innerText || '').match(/Current price:\\s*(\\$[\\d.,]+)/);
    if (!match) continue; // no price nearby usually means an ad/banner, not a real product card
    seen.add(url);
    products.push({name: alt, price: match[1], image: src, url});
  }

  // Every visible interactive element, generically - not just ones tied to
  // a product card, and no assumption about which one is "the" add-to-cart
  // control for a given item. Different sites (and different retailers on
  // the same site) shape this differently; the model gets each element's
  // own label and matches it to what it wants itself, the way a person
  // reading the page would.
  const CAP = 60;
  for (const el of document.querySelectorAll('button, a[href], input, select, [role="button"]')) {
    if (elements.length >= CAP) break;
    if (el.hasAttribute('data-agent-uid') || !isVisible(el)) continue;
    const label = labelFor(el);
    if (!label) continue;
    const role = el.tagName === 'A' ? 'link' : (el.tagName === 'INPUT' || el.tagName === 'SELECT' ? 'input' : 'button');
    elements.push({uid: stamp(el), role, label});
  }

  return {products, elements};
}
"""


def _extract_snapshot(page, limit: int = 8) -> dict:
    """Pull structured product cards (name, price, image, link) for the UI's
    evidence carousel, plus a uid-addressed list of every visible
    clickable/fillable element on the page - real photos and real
    per-product links, not a screenshot of the whole cluttered results
    page. Each uid is stamped onto the live element as data-agent-uid,
    so click()/fill() can resolve it with a plain locator rather than
    holding a Playwright handle across tool calls. No attempt is made to
    pair a product with "its" button - different sites (and different
    retailers on the same site) shape that differently, and guessing by DOM
    proximity only holds for one shape. The model matches a product to the
    right element itself, by label. Runs as a single page.evaluate() so the
    whole DOM walk happens in-browser - walking the DOM element-by-element
    from Python was hundreds of separate CDP round-trips and took 90+
    seconds on a real search page."""
    return page.evaluate(_EXTRACT_SNAPSHOT_JS, limit)


def _render_snapshot(snapshot: dict) -> str | None:
    products = snapshot["products"]
    elements = snapshot["elements"]
    lines = []
    if products:
        # The model must only ever see exactly what the UI can link to,
        # otherwise it cites items (from raw page text, a promo banner,
        # prior knowledge) with no real product card behind them. The link
        # itself is handed over pre-formatted as markdown, so the model
        # reuses it verbatim rather than constructing or matching links
        # itself. This also resolves ambiguity when two sizes of the same
        # product share a name, since each line carries its own exact URL.
        lines.append("Products found on this page:")
        for i, p in enumerate(products):
            lines.append(f"{i + 1}. [{p['name']}]({p['url']}) - {p['price']}")
    if elements:
        lines.append("Clickable or fillable things on this page:")
        for e in elements:
            verb = "fill" if e["role"] == "input" else "click"
            lines.append(f"- {verb}({e['uid']}): {e['role']} \"{e['label']}\"")
    return "\n".join(lines) if lines else None


def _page_snapshot(page, url: str | None) -> tuple[str, list[dict], set[str]]:
    """Navigate if a url is given, else snapshot the page as it stands, and
    return (text for the model, product cards for the UI, the set of uids
    valid against this exact snapshot). Raw page text is only a fallback for
    pages with no extractable products or interactive elements at all
    (error/location-prompt pages)."""
    if url is not None:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        # Interactive elements (e.g. an "Add to cart" button) can still be
        # hydrating client-side right after domcontentloaded on a fresh
        # navigation, so a snapshot taken immediately can miss them.
        page.wait_for_timeout(800)
    snapshot = _extract_snapshot(page)
    text = _render_snapshot(snapshot)
    if text is None:
        text = page.inner_text("body")[:2000]
    valid_uids = {e["uid"] for e in snapshot["elements"]}
    return text, snapshot["products"], valid_uids


def retrieve_taste(actor_id: str, query: str) -> str:
    """Tool: recall this person's remembered taste/preferences relevant to `query`."""
    response = client.retrieve_memory_records(
        memoryId=MEMORY_ID,
        namespace=f"taste/{actor_id}/",
        searchCriteria={"searchQuery": query, "topK": 3},
        maxResults=20,
    )
    records = response.get("memoryRecordSummaries", [])
    if not records:
        return "No remembered preferences relevant to this yet."
    return "\n".join(r["content"]["text"] for r in records if r.get("content", {}).get("text"))


def remember_taste(actor_id: str, session_id: str, user_message: str, note: str) -> str:
    """Tool: save something learned about this person's taste for next time.

    Writes a real USER/ASSISTANT conversational pair, not a lone ASSISTANT
    note. AgentCore's memory extraction only pulls facts from a
    USER/ASSISTANT exchange, not a lone ASSISTANT-authored event."""
    response = client.create_event(
        memoryId=MEMORY_ID,
        actorId=actor_id,
        sessionId=session_id,
        eventTimestamp=datetime.now(timezone.utc),
        payload=[
            {"conversational": {"content": {"text": user_message}, "role": "USER"}},
            {"conversational": {"content": {"text": note}, "role": "ASSISTANT"}},
        ],
    )
    return f"Remembered (event {response['event']['eventId']})."
