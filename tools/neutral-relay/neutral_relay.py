#!/usr/import/env python3
import argparse
import base64
import json
import os
import sys
import time
import urllib.request
import websockets
import asyncio
from collections import deque

# Canonical local routing authority for GovernLoop Minimal Transport.
DEFAULT_CONFIG_PATH = os.path.expanduser("~/.governloop/relay/config.json")

# Two-stage finalization settle.
#   NORMAL       (no soft streaming/busy markers): short settle.
#   CONSERVATIVE (one or more soft markers remain): long settle so a brief
#                generation pause cannot be mistaken for "done".
# B4 (GPT-reply-truncation fix): the settle window is the BACKSTOP only — the
# authoritative completion gate is the UI feature set (stop button gone AND
# copy/rate icons present), see ResponseCompletionTracker.observe(). Thresholds
# are env-overridable (GOVERLOOP_STABLE_READS / GOVERLOOP_SETTLE_SECONDS) for
# safe tuning without code changes.
NORMAL_STABLE_READS = 4
NORMAL_SETTLE_SECONDS = 8.0
CONSERVATIVE_STABLE_READS = 6
CONSERVATIVE_SETTLE_SECONDS = 30.0

# F3: post-finalize confirmation — after a candidate finalize, the same node
# must stay text-identical across these extra reads before the response is
# written (revocable finalize; a resumed stream cancels it).
CONFIRM_READS = 3
CONFIRM_INTERVAL_SECONDS = 2.0

# Strong delivery confirmation (see run_relay): "send button clicked" is NOT
# "message delivered". A click that lands while ChatGPT is still processing
# freshly-uploaded attachments can be silently swallowed, leaving the draft in
# the composer. Delivery is confirmed only when BOTH signals hold:
#   (1) composer content cleared, and
#   (2) the thread's user-turn count increased by exactly 1.
SEND_CONFIRM_TIMEOUT = 30         # seconds to wait for the two confirmation signals
SEND_CONFIRM_MAX_ATTEMPTS = 2     # 1 initial click + 1 safe re-click (no re-upload / re-inject)
SEND_UI_TRANSITION_SECONDS = 2.0  # wait after each click for the UI to transition
SEND_PENDING_TIMEOUT = 90         # SEND_PENDING window: composer cleared but thread not yet
                                  # confirmed (never re-click in this state)


class ResponseCompletionTracker:
    """Two-stage finalization settle for a correlated Assistant turn.

    Stage selection is driven by the *current* soft-generation signal (leftover
    stop-button / streaming / aria-busy DOM markers). Those markers are SOFT
    only and are NEVER a hard veto. A text change is the hard live-generation
    signal and always blocks finalization, resetting both the settle timer and
    the stable-read counter.

    NORMAL (no soft markers present):
        - assistant text stable for >= NORMAL_SETTLE_SECONDS
        - >= NORMAL_STABLE_READS consecutive identical reads
        => finalize

    CONSERVATIVE (one or more soft markers remain):
        - assistant text stable for >= CONSERVATIVE_SETTLE_SECONDS
        - >= CONSERVATIVE_STABLE_READS consecutive identical reads
        => finalize

    Rules:
        - any assistant text change resets the settle timer + stable-read count
        - if soft markers clear, NORMAL settle applies to the already-stable text
        - stale markers must not block forever (CONSERVATIVE still finalizes)
        - text still changing always blocks
        - timeout (enforced by the caller) remains fail-closed
        - the response is NOT required to echo REVIEW_REQUEST_ID (generic transport)
    """

    def __init__(
        self,
        normal_stable_reads=NORMAL_STABLE_READS,
        normal_settle_seconds=NORMAL_SETTLE_SECONDS,
        conservative_stable_reads=CONSERVATIVE_STABLE_READS,
        conservative_settle_seconds=CONSERVATIVE_SETTLE_SECONDS,
    ):
        self.normal_stable_reads = normal_stable_reads
        self.normal_settle_seconds = normal_settle_seconds
        self.conservative_stable_reads = conservative_stable_reads
        self.conservative_settle_seconds = conservative_settle_seconds
        self.last_text = None
        self.stable_reads = 0
        self.stable_since = None

    def reset(self):
        self.last_text = None
        self.stable_reads = 0
        self.stable_since = None

    def observe(self, snapshot, user_count_before, req_id, now=None):
        now = time.monotonic() if now is None else now
        snapshot = snapshot if isinstance(snapshot, dict) else {}

        try:
            user_count = int(snapshot.get("userCount") or 0)
        except (TypeError, ValueError):
            user_count = 0

        last_user_text = str(snapshot.get("lastUserText") or "").strip()
        text = str(snapshot.get("text") or "").strip()
        has_assistant = bool(snapshot.get("hasAssistant"))
        soft_generating = bool(snapshot.get("softGenerating"))

        # B4 hard completion gate (F1): ChatGPT renders the action bar (copy /
        # rate icons) only after it finalizes an assistant message, and removes
        # the stop button while streaming. finalize is therefore allowed only
        # when the text is stable AND the stop button is gone AND copy/rate
        # icons are present. This replaces the fragile "text stable alone"
        # signal that mistook a streaming pause for completion (A-class false
        # truncation). Soft markers only choose the settle window.
        stop_present = bool(snapshot.get("stopPresent"))
        has_copy_rate = bool(snapshot.get("hasCopyRate"))
        features_done = (not stop_present) and has_copy_rate

        # Correlation: this send must have produced a new user turn followed by
        # an assistant turn. The response itself is NOT required to echo
        # REVIEW_REQUEST_ID (supports generic transport).
        user_added = (user_count > user_count_before) or (
            bool(req_id) and req_id in last_user_text
        )

        if not (user_added and has_assistant and text):
            self.reset()
            return False, ""

        if text != self.last_text:
            # Hard live-generation evidence: reset settle + stable-read count.
            self.last_text = text
            self.stable_reads = 1
            self.stable_since = now
            return False, ""

        self.stable_reads += 1
        stable_for = max(0.0, now - (self.stable_since if self.stable_since is not None else now))

        if soft_generating:
            required_reads = self.conservative_stable_reads
            required_settle = self.conservative_settle_seconds
        else:
            required_reads = self.normal_stable_reads
            required_settle = self.normal_settle_seconds

        if self.stable_reads >= required_reads and stable_for >= required_settle and features_done:
            return True, text

        return False, ""


class AttachmentUploader:
    """Upload evidence attachments through the conversation file input.

    CDP mechanics are injected as async callables so the decision logic is
    unit-testable without a live browser:

      find_input()                 -> file-input node id (or None)
      set_files(node_id, abs_path) -> awaitable; raises on transport failure
      is_visible(filename)         -> awaitable bool (name visible in composer)

    Fail-closed: a missing file, absent file input, upload error, or a file
    name that never becomes visible all yield (False, reason). The caller MUST
    NOT send the request text or write a response when any attachment fails.
    """

    def __init__(
        self,
        find_input,
        set_files,
        is_visible,
        visibility_retries=15,
        retry_delay=1.0,
    ):
        self.find_input = find_input
        self.set_files = set_files
        self.is_visible = is_visible
        self.visibility_retries = visibility_retries
        self.retry_delay = retry_delay

    async def upload(self, path):
        """Return (ok, reason). reason is None on success."""
        if not os.path.exists(path):
            return False, "missing-file"
        node_id = await self.find_input()
        if not node_id:
            return False, "no-file-input"
        try:
            await self.set_files(node_id, os.path.abspath(path))
        except Exception as exc:  # fail-closed on transport/upload errors
            return False, f"upload-error:{exc}"
        base = os.path.basename(path)
        for _ in range(self.visibility_retries):
            await asyncio.sleep(self.retry_delay)
            if await self.is_visible(base):
                return True, None
        return False, "not-visible"


class SendConfirmation:
    """Three-state strong delivery confirmation for a user-turn send.

    "Send button clicked" is NOT "message delivered": a click that lands while
    ChatGPT is still processing freshly-uploaded attachments can be silently
    swallowed, leaving the draft in the composer. Delivery is modelled as three
    states (per reviewer design):

      - Draft still present (composer non-empty, user turn unchanged within
        confirm_timeout) -> the send was not accepted -> ONE safe re-click
        (never re-upload attachments or re-inject text -- duplicate risk) ->
        if the composer is still non-empty -> SEND_NOT_CONFIRMED (fail closed).
      - Composer cleared + user turn unchanged -> SEND_PENDING: the message
        left the composer but the thread has not confirmed it yet -> NEVER
        re-click / re-upload / re-inject from this state -> within
        pending_timeout, confirm via user-turn +1 (canonical, PRIMARY) or a NEW
        assistant turn with no assistant streaming before the send (AUXILIARY)
        -> timeout yields SEND_PENDING_TIMEOUT (no resend; manual verification).
      - Composer cleared + user turn +1 -> DELIVERY_CONFIRMED_PRIMARY.

    All CDP interaction is injected as async callables so the state machine is
    unit-testable without a live browser (same pattern as AttachmentUploader).

    Returned status is one of: DELIVERY_CONFIRMED_PRIMARY,
    DELIVERY_CONFIRMED_AUXILIARY, DELIVERY_CONFIRMED_RECONCILED,
    SEND_NOT_CONFIRMED, SEND_PENDING_TIMEOUT,
    SEND_BUTTON_UNAVAILABLE. `delivered` is True only for the three
    DELIVERY_* statuses.
    """

    def __init__(
        self,
        click_send,
        composer_cleared,
        turn_counts,
        assistant_streaming,
        confirm_timeout=SEND_CONFIRM_TIMEOUT,
        pending_timeout=SEND_PENDING_TIMEOUT,
        ui_transition_seconds=SEND_UI_TRANSITION_SECONDS,
        sleep=asyncio.sleep,
        now=time.time,
        snapshot=None,
        req_id=None,
    ):
        self.click_send = click_send
        self.composer_cleared = composer_cleared
        self.turn_counts = turn_counts
        self.assistant_streaming = assistant_streaming
        self.confirm_timeout = confirm_timeout
        self.pending_timeout = pending_timeout
        self.ui_transition_seconds = ui_transition_seconds
        self.sleep = sleep
        self.now = now
        self.snapshot = snapshot
        self.req_id = req_id

    async def confirm(self, user_count_before):
        """Run the state machine. Returns (delivered, primary, status)."""
        # Baselines for the auxiliary SEND_PENDING signal: a NEW assistant turn
        # appearing after our send proves server-side acceptance -- but only if
        # no assistant turn was actively streaming before the send (a late-
        # rendered node from a previous turn could otherwise false-positive).
        _, assistant_count_before = await self.turn_counts()
        assistant_streaming_before = await self.assistant_streaming()

        if not await self.click_send():
            print("Error: Send button not found or disabled.")
            return False, False, "SEND_BUTTON_UNAVAILABLE"
        await self.sleep(self.ui_transition_seconds)

        delivered, primary, cleared, user_now, assistant_now = await self._await_confirm(
            user_count_before)
        if not delivered and not cleared:
            # Draft still present: one safe re-click is allowed.
            print("SEND_DRAFT_STILL_PRESENT: composer not cleared after first send attempt. "
                  "Re-clicking send once (no re-upload, no re-inject).")
            if await self.click_send():
                await self.sleep(self.ui_transition_seconds)
                delivered, primary, cleared, user_now, assistant_now = await self._await_confirm(
                    user_count_before)
        if not delivered and not cleared:
            print("SEND_NOT_CONFIRMED: composer still non-empty after "
                  f"{SEND_CONFIRM_MAX_ATTEMPTS} attempts "
                  f"(user_turn={user_count_before}->{user_now}, composer_cleared={cleared}). "
                  "Failing closed. Manual recovery: "
                  "1) verify the draft + attachments are still present in the composer; "
                  "2) click Send ONCE; "
                  "3) confirm the composer cleared and the user turn appeared in the thread; "
                  "4) DO NOT re-run the send path for the same request (duplicate-delivery risk); "
                  "5) continue via manual readback, or stop and record "
                  "DELIVERY_MODE=MANUAL_SEND_RECOVERY.")
            return False, False, "SEND_NOT_CONFIRMED"
        if not delivered:
            # SEND_PENDING: composer cleared, thread not yet confirmed. From
            # here on: NEVER re-click / re-upload / re-inject.
            print("SEND_PENDING: draft has left the composer; awaiting thread confirmation "
                  "(no auto re-click to avoid duplicate delivery).")
            user_now = assistant_now = 0
            # B1 reconciliation: delivery may also be confirmed when the
            # REQUEST-CORRELATED read-back is observed — our REVIEW_REQUEST_ID
            # present in the thread's last user message AND the corresponding
            # assistant reply read back (settled). This binds delivery proof to
            # THIS request/turn; an unrelated new assistant message never counts
            # (the tracker requires req_id in lastUserText + has_assistant +
            # non-empty settled text).
            completion = ResponseCompletionTracker()
            pending_deadline = self.now() + self.pending_timeout
            while self.now() < pending_deadline:
                user_now, assistant_now = await self.turn_counts()
                if user_now == user_count_before + 1:
                    print("DELIVERY_CONFIRMED_PRIMARY: PASS (composer cleared, user turn +1)")
                    return True, True, "DELIVERY_CONFIRMED_PRIMARY"
                if (not assistant_streaming_before) and assistant_now > assistant_count_before:
                    # Auxiliary evidence: a NEW assistant turn appeared after
                    # our send and nothing was streaming before the send, which
                    # can only happen if the server accepted the user message.
                    print("DELIVERY_CONFIRMED_AUXILIARY: PASS (composer cleared; new assistant "
                          "turn after send; no assistant streaming before send)")
                    return True, False, "DELIVERY_CONFIRMED_AUXILIARY"
                if self.snapshot is not None and self.req_id:
                    ok, _text = completion.observe(
                        await self.snapshot(),
                        user_count_before=user_count_before,
                        req_id=self.req_id,
                    )
                    if ok:
                        print("DELIVERY_CONFIRMED_RECONCILED: PASS (request-correlated read-back "
                              "observed: REVIEW_REQUEST_ID present in the thread + corresponding "
                              "assistant reply read back)")
                        return True, False, "DELIVERY_CONFIRMED_RECONCILED"
                await self.sleep(1)
            print("SEND_PENDING_TIMEOUT: draft has left the composer but the thread has "
                  f"not confirmed delivery within {self.pending_timeout}s "
                  f"(user_turn={user_count_before}->{user_now}, "
                  f"assistant_turn={assistant_count_before}->{assistant_now}). "
                  "The relay will NOT retry Send to avoid duplicate delivery. "
                  "Manual verification required: check whether the user message appeared "
                  "in the thread; if yes, continue without resending; if no, inspect the "
                  "conversation before taking any further send action. Record "
                  "DELIVERY_MODE=MANUAL_SEND_RECOVERY / SEND_PENDING_TIMEOUT.")
            return False, False, "SEND_PENDING_TIMEOUT"
        if primary:
            print("DELIVERY_CONFIRMED_PRIMARY: PASS (composer cleared, user turn +1)")
        else:
            print("DELIVERY_CONFIRMED_AUXILIARY: PASS (composer cleared; new assistant turn "
                  "after send; no assistant streaming before send)")
        return True, primary, "DELIVERY_CONFIRMED_PRIMARY" if primary else "DELIVERY_CONFIRMED_AUXILIARY"

    async def _await_confirm(self, user_count_before):
        """Wait up to confirm_timeout for composer-clear (+user+1) or
        composer-clear alone (-> SEND_PENDING). Returns (delivered, primary,
        cleared, user_now, assistant_now)."""
        user_now = assistant_now = 0
        deadline = self.now() + self.confirm_timeout
        while self.now() < deadline:
            user_now, assistant_now = await self.turn_counts()
            cleared = await self.composer_cleared()
            if cleared and user_now == user_count_before + 1:
                return True, True, True, user_now, assistant_now
            if cleared:
                return False, False, True, user_now, assistant_now   # -> SEND_PENDING
            await self.sleep(1)
        return False, False, False, user_now, assistant_now


async def upload_attachments(uploader, paths):
    """Upload every evidence attachment; stop at the first failure.

    Returns (ok, failed_path, reason). Never returns ok=True when any
    attachment failed to upload - callers must treat failure as
    CHECKPOINT_DELIVERY_INCOMPLETE and must not proceed to send text or write
    a response (no false COMPLETE).
    """
    for ap in paths:
        ok, reason = await uploader.upload(ap)
        if not ok:
            print(f"ATTACH_FAIL {reason}: {ap}")
            return False, ap, reason
        print(f"ATTACHED: {ap}")
    return True, None, None


async def run_relay(args):
    # 1. Read request file
    if not os.path.exists(args.request_file):
        print(f"Error: Request file {args.request_file} not found.")
        return 1
        
    with open(args.request_file, "r") as f:
        request_text = f.read()

    # Extract REPO and REVIEW_REQUEST_ID for routing and anti-crosstalk
    repo = None
    req_id = None
    for line in request_text.split('\n'):
        if line.startswith("REPO:"):
            repo = line.split("REPO:")[1].strip()
        elif line.startswith("REVIEW_REQUEST_ID:"):
            req_id = line.split("REVIEW_REQUEST_ID:")[1].strip()

    if not repo:
        print("Error: REPO field not found in request file. Fail closed.")
        return 1
    if not req_id:
        print("Error: REVIEW_REQUEST_ID field not found in request file. Fail closed.")
        return 1

    # 2. Config Routing (Trusted routing only)
    config_file = args.config_file
    if not os.path.exists(config_file):
        print(f"Error: Config file {config_file} not found.")
        return 1
        
    with open(config_file, "r") as f:
        config = json.load(f)
        
    route = config.get("routes", {}).get(repo)
    if not route:
        print(f"Error: No trusted route configured for repo {repo}. Fail closed.")
        return 1

    # Session-level overrides (never written back to config): the conversation
    # URL is task/session state. The repo must still be a trusted configured
    # route, but its conversation target may be overridden for this run only
    # (ask the user once per session; never persist a permanent binding).
    gpt_url = args.conversation_url or route.get("conversation_url")
    cdp_port = args.cdp_port or route.get("cdp_port")

    if not gpt_url or not cdp_port:
        print("Error: Incomplete route configuration. Need conversation_url and cdp_port.")
        return 1

    # In DRY-RUN mode, just print what we would do and simulate success
    if args.dry_run:
        print(f"[DRY-RUN] Would route {repo} to CDP port {cdp_port} at URL {gpt_url}")
        print(f"[DRY-RUN] Sending Payload:\n{request_text}")
        print(f"[DRY-RUN] Waiting for response with ID: {req_id}")
        
        # Simulate an external response write
        mock_response = (
            f"REVIEW_REQUEST_ID: {req_id}\n"
            "VERDICT: PASS\n"
            f"REPO: {repo}\n"
            "PR: mock\n"
            "HEAD: mock\n"
            "SUMMARY: Dry run test\n"
            "ACTIONS: None\n"
        )
        with open(args.output_file, "w") as f:
            f.write(mock_response)
        return 0

    # 3. Transport via CDP
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=8)
        ws_url = json.loads(req.read().decode()).get("webSocketDebuggerUrl", "")
    except Exception as e:
        print(f"Error connecting to CDP on port {cdp_port}: {e}")
        return 1
        
    async with websockets.connect(ws_url, max_size=2**30, open_timeout=10) as ws:
        _id = 0
        # B4 (F4 observability, opt-in): ring buffer of SSE events received
        # during the run, to prove whether the server stopped the stream
        # (response.incomplete / output_item.done) or the tracker mis-judged.
        sse_diag = bool(args.sse_diag)
        sse_events = deque(maxlen=100) if sse_diag else None
        async def cmd(method, params=None, session=None):
            nonlocal _id
            _id += 1
            mid = _id
            msg = {"id": mid, "method": method}
            if params is not None:
                msg["params"] = params
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                data = json.loads(raw)
                if data.get("method") == "Network.eventSourceMessageReceived" and sse_events is not None:
                    p = data.get("params", {})
                    sse_events.append({
                        "name": p.get("eventName"),
                        "data": str(p.get("data"))[:200],
                    })
                if data.get("id") == mid:
                    return data

        # Find specific conversation tab
        r = await cmd("Target.getTargets")
        target = next((t for t in r.get("result", {}).get("targetInfos", [])
                       if t.get("type") == "page" and gpt_url in (t.get("url") or "")), None)
                       
        if not target:
            print("Error: Target conversation URL not open in browser.")
            return 1
            
        at = await cmd("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        sid = at.get("result", {}).get("sessionId")

        async def js(expr):
            ev = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=sid)
            return ev.get("result", {}).get("result", {}).get("value")

        await cmd("Page.enable", {}, session=sid)
        if sse_diag:
            try:
                await cmd("Network.enable", {}, session=sid)
            except Exception:
                print("WARN: Network.enable failed; SSE diagnostics disabled for this run.")

        async def _capture_screenshot(name):
            """B4 (F6, token-free fallback): capture a PNG of the page when a
            screenshot dir is configured. NEVER analysed automatically."""
            if not args.screenshot_dir:
                return None
            try:
                shot = await cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, session=sid)
                data = (shot.get("result") or {}).get("data")
                if not data:
                    return None
                os.makedirs(args.screenshot_dir, exist_ok=True)
                p = os.path.join(args.screenshot_dir, f"{name}.png")
                with open(p, "wb") as f:
                    f.write(base64.b64decode(data))
                return p
            except Exception as exc:
                print(f"WARN: screenshot capture failed: {exc}")
                return None

        # ── OPTIONAL: upload evidence attachments before sending text ────────
        # Each --attachment is uploaded through the ChatGPT file input via CDP
        # DOM.setFileInputFiles (no user gesture needed). Attachment readiness is
        # verified by waiting for the file name to appear in the composer DOM.
        # All uploads happen inside this single attached session, so text and
        # attachments always go to the SAME bound conversation.
        async def _find_file_input():
            await cmd("DOM.enable", {}, session=sid)
            doc = await cmd("DOM.getDocument", {"depth": -1}, session=sid)
            root = doc.get("result", {}).get("root", {}).get("nodeId")
            q = await cmd("DOM.querySelector",
                          {"nodeId": root, "selector": "input[type=file]"},
                          session=sid)
            return q.get("result", {}).get("nodeId")

        async def _set_files(node_id, abs_path):
            await cmd("DOM.setFileInputFiles",
                      {"nodeId": node_id, "files": [abs_path]},
                      session=sid)

        async def _is_visible(base):
            seen = await js("(()=>{const t=(document.querySelector('[contenteditable=true]')||{}).innerText||'';const b=document.body.innerText||'';return t+' '+b;})()")
            return base in (seen or "")

        uploader = AttachmentUploader(_find_file_input, _set_files, _is_visible)
        ok, failed_path, reason = await upload_attachments(uploader, args.attachment or [])
        if not ok:
            # CHECKPOINT_DELIVERY_INCOMPLETE: never proceed to send the text or
            # write a response when any required attachment failed to upload.
            return 1
        await asyncio.sleep(1)

        # Capture the existing user-turn count before sending. The response is
        # correlated to the assistant turn that follows the user turn created
        # by this send, so the pre-send state that must change is the count of
        # user turns (not assistant turns).
        user_count_before = await js("(()=>document.querySelectorAll('[data-message-author-role=\\'user\\']').length)()")
        try:
            user_count_before = int(user_count_before or 0)
        except (TypeError, ValueError):
            user_count_before = 0

        # Inject request text using exact DOM interactions
        esc_text = json.dumps(request_text)
        await js(f"(()=>{{const e=document.querySelector('[contenteditable=true]');if(!e)return false;e.focus();e.innerHTML='';e.innerText={esc_text};e.dispatchEvent(new Event('input',{{bubbles:true}}));return true}})()")
        await asyncio.sleep(1)
        
        # Click send and STRONGLY confirm delivery before waiting for the
        # assistant turn. "Send button clicked" is NOT "message delivered": if
        # the click lands while ChatGPT is still processing freshly-uploaded
        # attachments, the send can be silently swallowed and the draft stays
        # in the composer. The three-state delivery state machine lives in
        # SendConfirmation (unit-tested); here we wire it to the live CDP js()
        # helpers. See SendConfirmation for the full state model.
        send_confirm_timeout = getattr(args, "send_confirm_timeout", SEND_CONFIRM_TIMEOUT)
        send_pending_timeout = getattr(args, "send_pending_timeout", SEND_PENDING_TIMEOUT)

        async def _click_send():
            return await js("(()=>{const b=document.querySelector('button[data-testid=\\'send-button\\']'); if(b && !b.disabled){b.click(); return true;} return false;})()")

        async def _composer_cleared():
            return bool(await js("(()=>{const e=document.querySelector('[contenteditable=true]');return !e || ((e.innerText||'').trim().length===0);})()"))

        async def _turn_counts():
            n = await js("(()=>{const r=document.querySelectorAll('[data-message-author-role]');let u=0,a=0;r.forEach(x=>{const v=x.getAttribute('data-message-author-role');if(v==='user')u++;else if(v==='assistant')a++;});return JSON.stringify({u:u,a:a});})()")
            try:
                c = json.loads(n or '{"u":0,"a":0}')
                return int(c.get("u", 0)), int(c.get("a", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                return 0, 0

        async def _assistant_streaming():
            return bool(await js("(()=>{let s=false;document.querySelectorAll('[data-message-author-role=\\'assistant\\']').forEach(x=>{if(x.matches('.streaming-animation')||x.querySelector('.streaming-animation')||x.getAttribute('data-is-streaming')==='true'||x.getAttribute('aria-busy')==='true')s=true;});const st=document.querySelector('button[data-testid=\\'stop-button\\'],button[data-testid=\\'stop-generation\\'],button[aria-label*=\\'Stop\\'],button[aria-label*=\\'停止\\']');return s||!!st;})()"))

        async def _snapshot():
            return await js("""(()=>{
                const roles = Array.from(document.querySelectorAll('[data-message-author-role]'));
                const users = roles.filter(n => n.getAttribute('data-message-author-role') === 'user');
                const lastUser = users.length ? users[users.length - 1] : null;
                const lastUserText = lastUser ? ((lastUser.innerText || lastUser.textContent || '').trim()) : '';
                let assistant = null;
                if (lastUser) {
                    const idx = roles.indexOf(lastUser);
                    for (let i = idx + 1; i < roles.length; i++) {
                        if (roles[i].getAttribute('data-message-author-role') === 'assistant') {
                            assistant = roles[i];
                            break;
                        }
                    }
                }
                const text = assistant ? ((assistant.innerText || assistant.textContent || '').trim()) : '';
                const stop = document.querySelector('button[data-testid="stop-button"], button[data-testid="stop-generation"], button[aria-label*="Stop"], button[aria-label*="停止"]');
                const streaming = !!(assistant && (
                    assistant.matches('.streaming-animation') ||
                    assistant.querySelector('.streaming-animation') ||
                    assistant.getAttribute('data-is-streaming') === 'true' ||
                    assistant.getAttribute('aria-busy') === 'true'
                ));
                // B4 (F1): ChatGPT renders the action bar (copy / rate icons)
                // only after the message is finalized. Multi-selector fallback
                // in case ChatGPT renames these controls.
                const copyRate = document.querySelector(
                    'button[aria-label*="Copy"], button[aria-label*="复制"], ' +
                    '[data-testid*="copy"], [data-testid*="like"], [data-testid*="thumbs"], ' +
                    'button[aria-label*="评价"], button[aria-label*="点赞"], button[aria-label*="点踩"], ' +
                    'button[aria-label*="Like"], button[aria-label*="Thumbs"]'
                );
                return {
                    userCount:users.length,
                    lastUserText:lastUserText,
                    text:text,
                    hasAssistant:!!assistant,
                    softGenerating:(!!stop || streaming),
                    stopPresent:!!stop,
                    streamingMarker:streaming,
                    hasCopyRate:!!copyRate,
                    visibilityState:document.visibilityState
                };
            })()""")

        confirmation = SendConfirmation(
            click_send=_click_send,
            composer_cleared=_composer_cleared,
            turn_counts=_turn_counts,
            assistant_streaming=_assistant_streaming,
            confirm_timeout=send_confirm_timeout,
            pending_timeout=send_pending_timeout,
            snapshot=_snapshot,
            req_id=req_id,
        )
        delivered, _primary, send_status = await confirmation.confirm(user_count_before)
        if not delivered:
            # SEND_BUTTON_UNAVAILABLE / SEND_NOT_CONFIRMED / SEND_PENDING_TIMEOUT
            # (state machine already printed the detailed reason).
            return 1

        # Poll for the assistant response following the user turn created by
        # this send. Correlation remains user-turn -> following Assistant turn.
        # Completion is text-first: a text change is hard evidence that output
        # is still live and restarts the settle window. ChatGPT DOM stop/busy/
        # streaming markers are only soft evidence because they may remain stale
        # after a visibly complete response. Soft markers therefore require a
        # longer stable-text settle window, but cannot block finalization forever.
        deadline = time.time() + args.wait_timeout
        found_response = False
        final_text = ""
        # B4 (F2): thresholds are env-tunable for safe rollback without code
        # changes; defaults moved to 8s / 4 reads (streaming-pause tolerance).
        completion = ResponseCompletionTracker(
            normal_stable_reads=int(os.environ.get("GOVERLOOP_STABLE_READS", NORMAL_STABLE_READS)),
            normal_settle_seconds=float(os.environ.get("GOVERLOOP_SETTLE_SECONDS", NORMAL_SETTLE_SECONDS)),
        )
        diag_path = args.output_file + ".diag.jsonl"
        while time.time() < deadline:
            snapshot = await _snapshot()

            complete, settled_text = completion.observe(
                snapshot,
                user_count_before=user_count_before,
                req_id=req_id,
            )
            if complete:
                # B4 (F3): post-finalize confirmation — the same node must stay
                # text-identical across CONFIRM_READS more reads before we write
                # the response. A resumed stream cancels the finalize (revocable).
                confirmed = True
                confirm_text = settled_text
                for _ in range(CONFIRM_READS):
                    await asyncio.sleep(CONFIRM_INTERVAL_SECONDS)
                    snap2 = await _snapshot()
                    c2, t2 = completion.observe(snap2, user_count_before, req_id)
                    if not c2:
                        confirmed = False
                        break
                    confirm_text = t2
                if confirmed:
                    final_text = confirm_text
                    found_response = True
                    break
                # else: text resumed -> keep waiting on the outer loop

            await asyncio.sleep(2)

        # B4 (F4): diagnostics — finalize/timeout state snapshot + optional SSE
        # tail, for proving A-class (tracker) vs B-class (server) truncation.
        try:
            with open(diag_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "req_id": req_id,
                    "status": "finalized" if found_response else "timeout",
                    "ts": time.time(),
                    "text_len": len(final_text) if found_response else None,
                    "text_head": (final_text or "")[:200] if found_response else None,
                    "snapshot": {
                        "stopPresent": bool((await _snapshot()).get("stopPresent")),
                        "hasCopyRate": bool((await _snapshot()).get("hasCopyRate")),
                        "visibilityState": (await _snapshot()).get("visibilityState"),
                        "userCount": (await _snapshot()).get("userCount"),
                    },
                    "sse_tail": list(sse_events) if sse_events is not None else None,
                }, ensure_ascii=False) + "\n")
        except Exception as exc:
            print(f"WARN: diagnostics write failed: {exc}")

        if not found_response:
            print(f"Error: Timed out after {args.wait_timeout}s waiting for a new stable Assistant response to settle.")
            await _capture_screenshot(f"{req_id}-timeout")  # F6 evidence
            return 1

        await _capture_screenshot(f"{req_id}-finalized")  # F6 evidence (opt-in)

        with open(args.output_file, "w") as f:
            f.write(final_text)

        print(f"Success: Wrote response to {args.output_file}")
        return 0

def main():
    parser = argparse.ArgumentParser(description="GovernLoop Neutral Relay Transport")
    parser.add_argument("--request-file", required=True, help="Path to the review request payload file")
    parser.add_argument("--output-file", required=True, help="Path to write the GPT review response")
    parser.add_argument("--config-file", default=DEFAULT_CONFIG_PATH, help=f"Path to the routing config.json (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--wait-timeout", type=int, default=900, help="Seconds to wait for the new Assistant turn to finish streaming and stabilize (default: 900)")
    parser.add_argument("--send-confirm-timeout", type=int, default=SEND_CONFIRM_TIMEOUT,
                        help=f"Seconds to wait for strong delivery confirmation (composer cleared "
                             f"+ user turn +1) before the safe re-click / fail-closed path "
                             f"(default: {SEND_CONFIRM_TIMEOUT})")
    parser.add_argument("--send-pending-timeout", type=int, default=SEND_PENDING_TIMEOUT,
                        help=f"Seconds to wait in the SEND_PENDING state (composer cleared but "
                             f"thread not yet confirmed; no re-click) before SEND_PENDING_TIMEOUT "
                             f"(default: {SEND_PENDING_TIMEOUT})")
    parser.add_argument("--dry-run", action="store_true", help="Simulate routing without CDP execution")
    parser.add_argument("--attachment", action="append", default=[],
                        help="evidence file to upload to the conversation before sending "
                             "the request text (repeatable). The file is uploaded through the "
                             "ChatGPT file input via CDP and its readiness is verified.")
    parser.add_argument("--conversation-url", default=None,
                        help="session-level ChatGPT conversation URL override for this run "
                             "(ask the user once per session; never written to config)")
    parser.add_argument("--cdp-port", type=int, default=None,
                        help="session-level CDP port override for this run "
                             "(never written to config)")
    parser.add_argument("--screenshot-dir", default=os.environ.get("GOVERLOOP_SCREENSHOT_DIR"),
                        help="B4 F6: when set, capture token-free PNG evidence at finalize/timeout "
                             "(never analysed automatically)")
    parser.add_argument("--sse-diag", action="store_true",
                        default=os.environ.get("GOVERLOOP_SSE_DIAG") == "1",
                        help="B4 F4: record SSE event tail into the diagnostics file "
                             "to distinguish tracker vs server truncation")
    args = parser.parse_args()
    sys.exit(asyncio.run(run_relay(args)))

if __name__ == "__main__":
    main()
