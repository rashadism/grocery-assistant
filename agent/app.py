import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

import agent
import tools

REGION = os.environ.get("AWS_REGION", "us-east-1")

# In-memory, single-process - fine for a demo. actor_id is just the raw
# username (identification, not authentication - see DEMO_PLAN.md).
sessions: dict[str, dict] = {}

# An agent turn can run well past a gateway's upstream timeout (multiple
# tool calls, a live browse()) - /api/chat hands the turn to a background
# thread and returns immediately so no single request has to survive it;
# the client polls /api/chat-result for the outcome.
jobs: dict[str, dict] = {}

_MARKDOWN_LINK_URL_RE = re.compile(r"\]\((https?://[^\s)]+)\)")


def _evidence_actually_recommended(reply: str, evidence: list[dict]) -> list[dict]:
    """browse() can surface up to 8 candidates per call, but the model only
    recommends 2-3 of them - showing every candidate in the carousel makes it
    look disconnected from the reply (far more cards than linked products).
    Keep only the products whose link the model actually kept in its reply,
    in the order they're mentioned."""
    linked_urls = _MARKDOWN_LINK_URL_RE.findall(reply)
    by_url = {e["url"]: e for e in evidence}
    return [by_url[u] for u in linked_urls if u in by_url]


def run_chat_job(job_id: str, bound: "agent.BoundAgent", message: str) -> None:
    bound.current_action = "Reading your message…"
    bound.browse_count = 0
    bound.action_count = 0
    bound.evidence = []
    bound.current_user_message = message
    try:
        result = bound.agent(message)
        reply = str(result).strip()
        if not reply:
            # The model's loop can end without a final text turn right
            # after the browse cap kicks in - str(result) comes back empty
            # with no exception raised, so this can't be caught above.
            print(f"Agent turn for job {job_id} produced an empty reply", flush=True)
            reply = "I wasn't able to put together a full answer that time - could you try asking again?"
        jobs[job_id] = {
            "status": "done",
            "reply": reply,
            "awaiting_handoff": bound.awaiting_handoff,
            "evidence": _evidence_actually_recommended(reply, bound.evidence),
        }
    except Exception as exc:  # noqa: BLE001 - surface as a clean error, log server-side
        print(f"Agent turn failed: {exc}", flush=True)
        jobs[job_id] = {"status": "error", "error": "Agent request failed. Check server logs."}
    finally:
        bound.current_action = None


def generate_live_view_url(session_id: str, expires: int = 300) -> str:
    endpoint = f"https://bedrock-agentcore.{REGION}.amazonaws.com"
    url = urlparse(f"{endpoint}/browser-streams/{tools.BROWSER_ID}/sessions/{session_id}/live-view")
    credentials = Credentials(
        os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"]
    )
    request = AWSRequest(method="GET", url=url.geturl(), headers={"host": url.hostname})
    SigV4QueryAuth(
        credentials=credentials, service_name="bedrock-agentcore", region_name=REGION, expires=expires
    ).add_auth(request)
    return request.url


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 16384:
            raise ValueError("Request must be at most 16 KB")
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, {"ok": True})
            return
        if self.path.startswith("/api/chat-result"):
            job_id = parse_qs(urlparse(self.path).query).get("job_id", [""])[0]
            job = jobs.get(job_id)
            if not job:
                self.send_json(404, {"error": "Unknown job"})
                return
            if job["status"] == "pending":
                bound = sessions.get(job["session_id"], {}).get("bound")
                self.send_json(200, {**job, "current_action": bound.current_action if bound else None})
                return
            self.send_json(200, job)
            return
        if self.path.startswith("/api/live-view-url"):
            app_session_id = parse_qs(urlparse(self.path).query).get("session_id", [""])[0]
            bound = sessions.get(app_session_id, {}).get("bound")
            browser_session_id = bound.browser.session_id if bound else None
            if not browser_session_id:
                self.send_json(404, {"error": "No browser session active yet"})
                return
            self.send_json(200, {"url": generate_live_view_url(browser_session_id)})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/session":
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                self.send_json(400, {"error": "Invalid JSON request"})
                return
            username = body.get("username", "").strip()
            if not username or len(username) > 64:
                self.send_json(400, {"error": "Enter a username of at most 64 characters"})
                return
            session_id = str(uuid.uuid4())
            sessions[session_id] = {
                "actor_id": username,
                "bound": agent.build_agent(actor_id=username, session_id=session_id),
            }
            self.send_json(200, {"session_id": session_id})
            return

        if self.path == "/api/chat":
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                self.send_json(400, {"error": "Invalid JSON request"})
                return
            session_id = body.get("session_id", "")
            message = body.get("message", "")
            if session_id not in sessions:
                self.send_json(404, {"error": "Unknown session - call /api/session first"})
                return
            if not isinstance(message, str) or not message.strip() or len(message) > 4000:
                self.send_json(400, {"error": "Enter a message of at most 4000 characters"})
                return
            bound = sessions[session_id]["bound"]
            job_id = str(uuid.uuid4())
            jobs[job_id] = {"status": "pending", "session_id": session_id}
            threading.Thread(
                target=run_chat_job, args=(job_id, bound, message.strip()), daemon=True
            ).start()
            self.send_json(202, {"job_id": job_id})
            return

        if self.path == "/api/release-control":
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError):
                self.send_json(400, {"error": "Invalid JSON request"})
                return
            session_id = body.get("session_id", "")
            bound = sessions.get(session_id, {}).get("bound")
            if not bound:
                self.send_json(404, {"error": "Unknown session"})
                return
            bound.awaiting_handoff = False
            self.send_json(200, {"message": bound.browser.release_control()})
            return

        self.send_error(404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
