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

# B4 auto-fallback: how long the relay keeps re-reading the same node for a
# recovered (non-truncated) reply after detecting a truncated shape. Env-
# tunable via GOVERLOOP_RECOVERY_SECONDS.
RECOVERY_SECONDS = 15.0


def _looks_truncated(text):
    """B4 truncation heuristic: does the reply look like a JSON-shaped message
    (review envelope) interrupted mid-string? Cheap structural signal only —
    brace balance on text that starts with '{'. Prose without braces is
    balanced and never triggers. Used to AUTO-enable the screenshot fallback
    (no consent, proactive) — a false positive only costs one small PNG plus a
    short re-read."""
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s or not s.startswith("{"):
        return False
    return s.count("{") > s.count("}")

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


# ── Chat composer targeting (two-phase safety model) ─────────────────────────
# Phase A (pre-mutation): enumerate editable surfaces and select the single
# trustworthy chat composer. A send control need NOT exist yet — newer ChatGPT
# renders the send button only after text is present, so Phase A never depends
# on it. Inject only into the selected node, verify the payload landed, and ROLL
# BACK to the pre-mutation content on verification failure (no orphan draft).
# Phase B (pre-send): after injection, re-enumerate and PROVE the resolved
# composer is STILL the same one that received the checkpoint, then locate the
# send control bound to that same composer container/form (excludes voice /
# dictation controls, must be enabled). Any identity drift, disappearance,
# reorder ambiguity, absent send, or voice-only control -> ROLL BACK the
# checkpoint and fail closed (NO click, no complex auto-recovery).
# Strict fail-closed: 0 or >1 trustworthy candidates -> zero mutation.

# Enumerate every editable surface and report, for each, a stable unique CSS
# selector (node_css), whether it is a writing-block/editor (excluded), and the
# send control (send_css) bound to its nearest container/form. Editables that
# share one composer-like FORM (e.g. ChatGPT's hidden fallback textarea + the
# visible ProseMirror in the same <form class="group/composer">) are collapsed
# to a single surface so the same composer is never seen as two candidates.
ENUMERATE_SURFACES_JS = r"""
(() => {
  function cssPath(el) {
    if (!el) return null;
    if (el.id) return '#' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== 'body') {
      let sel = node.tagName.toLowerCase();
      if (node.classList && node.classList.length) {
        const keep = Array.from(node.classList).filter(c => /[a-z]/i.test(c)).slice(0, 2);
        sel += keep.map(c => '.' + (window.CSS && CSS.escape ? CSS.escape(c) : c)).join('');
      }
      const parent = node.parentElement;
      if (parent) {
        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (sibs.length > 1) sel += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
      }
      parts.unshift(sel);
      node = parent;
    }
    return parts.join(' > ');
  }
  function inWritingBlock(el) {
    // ProseMirror is an editor technology, NOT proof of a writing-block, so it
    // is never used as identity. writing-block/editor markers must be LOCAL to
    // the container that also holds this editable: the container node itself is
    // a writing-block, or a marker (writing-block-header / magic-edit) is a
    // DIRECT child of a container on this editable's ancestor chain. Deep
    // descendants under a shared high-level ancestor are deliberately ignored,
    // so an unrelated sibling writing-block cannot contaminate classification.
    // Genuinely ambiguous identity -> treated as writing-block (fail closed).
    let n = el;
    while (n) {
      if (n.getAttribute) {
        const ti = (n.getAttribute('data-testid') || '').toLowerCase();
        const cls = (n.getAttribute('class') || '').toLowerCase();
        if (ti.includes('writing-block') || cls.includes('writing-block'))
          return true;
        for (const child of (n.children || [])) {
          if (!child || child.nodeType !== 1) continue;
          const cti = (child.getAttribute('data-testid') || '').toLowerCase();
          const ccls = (child.getAttribute('class') || '').toLowerCase();
          if (cti.includes('writing-block-header') || ccls.includes('magic-edit'))
            return true;
        }
      }
      n = n.parentElement;
    }
    return false;
  }
  // Voice / dictation controls are NEVER a send control. A real send button
  // may not exist yet during Phase A (newer ChatGPT renders it only after
  // text is present); Phase B re-enumerates after injection and validates it.
  function isVoiceControl(b) {
    const s = (((b.getAttribute('aria-label') || '') + ' ' +
                (b.getAttribute('class') || '') + ' ' +
                (b.getAttribute('data-testid') || '')).toLowerCase());
    return /voice|speech|dictat|microphone|mic-|\u8bed\u97f3|\u542c\u5199/.test(s);
  }
  function findSendControl(scope) {
    for (const b of Array.from(scope.querySelectorAll('button'))) {
      if (isVoiceControl(b)) continue;
      const tid = b.getAttribute('data-testid') || '';
      const aria = b.getAttribute('aria-label') || '';
      if (tid === 'send-button' || /send/i.test(aria) || /\u53d1\u9001/.test(aria))
        return b;
    }
    return null;
  }
  // Composer identity anchor: the nearest composer-like FORM wrapping the
  // editable. ChatGPT renders one composer as BOTH a hidden fallback <textarea>
  // and a visible contenteditable ProseMirror inside the same form; editables
  // sharing one anchor are the SAME composer and must collapse to a single
  // surface. Non-composer forms (no composer/prompt marker) yield null so
  // unrelated editables stay separate candidates -> ambiguity -> fail closed.
  function composerAnchor(el) {
    let n = el;
    while (n && n.tagName.toLowerCase() !== 'body') {
      if (n.tagName.toLowerCase() === 'form') {
        const cls = (n.getAttribute('class') || '');
        const tid = (n.getAttribute('data-testid') || '');
        if (/composer|prompt|chat|\u8f93\u5165|\u5bf9\u8bdd/.test(cls + ' ' + tid)) return n;
        if (n.querySelector('button[data-testid="composer-plus-btn"]')) return n;
        return null;
      }
      n = n.parentElement;
    }
    return null;
  }
  // Pick the canonical editable of a composer: visible contenteditable wins
  // over a hidden fallback textarea (ProseMirror over wcDTda_fallbackTextarea).
  function editableScore(e) {
    const ce = e.isContentEditable ? 1 : 0;
    const vis = !!(e.offsetWidth || e.offsetHeight || (e.getClientRects && e.getClientRects().length)) ? 1 : 0;
    return ce * 4 + vis * 2 + (ce ? 0 : 1);
  }
  const editables = Array.from(document.querySelectorAll('[contenteditable="true"], textarea'));
  // Key groups by DOM element identity (the anchor form, or the editable itself
  // when there is no composer form), never by cssPath: two structurally
  // identical sibling forms would otherwise collapse to the same selector.
  const order = [];
  const groups = new Map();
  for (const e of editables) {
    const anchor = composerAnchor(e);
    const key = anchor || e;
    if (groups.has(key)) {
      if (editableScore(e) > editableScore(groups.get(key))) groups.set(key, e);
    } else {
      groups.set(key, e);
      order.push(key);
    }
  }
  const surfaces = [];
  for (const key of order) {
    const e = groups.get(key);
    // Climb from the editable to the nearest ancestor container that itself
    // contains a send control; that control is the one bound to this composer's
    // container/form (not required to be a descendant of the editable).
    let scope = e;
    let sendBtn = null;
    let container_css = null;
    while (scope && scope.tagName.toLowerCase() !== 'body') {
      const sb = findSendControl(scope);
      if (sb) { sendBtn = sb; container_css = cssPath(scope); break; }
      scope = scope.parentElement;
    }
    const send_css = sendBtn ? cssPath(sendBtn) : null;
    const wb = inWritingBlock(e);
    const editable_kind = (e.tagName.toLowerCase() === 'textarea') ? 'textarea' : 'contenteditable';
    surfaces.push({
      kind: wb ? 'writing-block' : 'chat-composer',
      node_css: cssPath(e),
      has_send: !!sendBtn,
      send_enabled: sendBtn ? !sendBtn.disabled : false,
      send_css: send_css,
      send_control_type: sendBtn ? 'send' : null,
      container_css: container_css,
      editable_kind: editable_kind,
      is_writing_block: wb,
      is_editor: wb
    });
  }
  return JSON.stringify(surfaces);
})()
"""

# Read/write respect the editable kind: <textarea> uses .value, contenteditable
# uses innerText/innerHTML. Any other editable kind is unsupported -> null/false
# so callers fail closed (never pretend a surface is writable when it is not).
READ_NODE_JS = ("(()=>{const e=document.querySelector(%s);if(!e)return '';"
                "if(e.tagName==='TEXTAREA')return e.value||'';"
                "if(e.isContentEditable)return e.innerText||'';"
                "return null;})()")
WRITE_NODE_JS = ("(()=>{const e=document.querySelector(%s);if(!e)return false;e.focus();"
                 "if(e.tagName==='TEXTAREA'){e.value=%s;}"
                 "else if(e.isContentEditable){e.innerHTML='';e.innerText=%s;}"
                 "else{return false;}"
                 "e.dispatchEvent(new Event('input',{bubbles:true}));return true;})()")
CLICK_SEND_JS = "(()=>{const b=document.querySelector(%s); if(b && !b.disabled){b.click(); return true;} return false;})()"


def _js_str(s):
    """Quote a value as a JS string literal for embedding in the snippets."""
    return json.dumps(s)


def select_trustworthy_chat_composer(surfaces):
    """Pure, browser-free Phase A selection of the single trustworthy chat composer.

    Returns (ok, target, reason).
      ok=True  -> target = {"node_css", "send_css", "send_enabled",
                            "send_control_type", "container_css", "editable_kind",
                            "kind": "chat-composer"}
      ok=False -> caller MUST fail closed (zero mutation).

    Phase A does NOT require a send control: newer ChatGPT renders the send
    button only after text is present, so an empty-but-real composer is a valid
    target. Send-control trust is enforced later in Phase B (pre-send), after
    injection. Strict fail-closed: writing-block/editor surfaces are never
    targets; zero or >1 candidate composers fail closed.
    """
    if not isinstance(surfaces, list) or not surfaces:
        return False, None, "no-editable-surfaces"
    candidates = [s for s in surfaces
                  if not s.get("is_writing_block") and not s.get("is_editor")]
    if len(candidates) == 0:
        return False, None, "no-trustworthy-chat-composer"
    if len(candidates) > 1:
        return False, None, "ambiguous-multiple-chat-composers"
    t = candidates[0]
    node_css = t.get("node_css")
    if not node_css:
        return False, None, "selected-composer-missing-node-selector"
    return True, {
        "node_css": node_css,
        "send_css": t.get("send_css"),
        "send_enabled": t.get("send_enabled"),
        "send_control_type": t.get("send_control_type"),
        "container_css": t.get("container_css"),
        "editable_kind": t.get("editable_kind"),
        "kind": "chat-composer",
    }, None


def _same_target(a, b):
    """True when two resolved targets identify the SAME bound composer.

    node_css must match exactly. send_css / container_css are compared only when
    present on BOTH sides: during Phase A a send control typically does not
    exist yet (newer ChatGPT renders it only after text), so a later Phase B
    first appearance must not be treated as drift — but a send control that was
    already bound in Phase A must be unchanged. A missing node_css on either
    side means identity cannot be proven -> False (fail closed, no click).
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get("node_css") != b.get("node_css"):
        return False
    sa, sb = a.get("send_css"), b.get("send_css")
    if sa is not None and sb is not None and sa != sb:
        return False
    ca, cb = a.get("container_css"), b.get("container_css")
    if ca is not None and cb is not None and ca != cb:
        return False
    return True


# ── Canonical fail-closed status vocabulary ──────────────────────────────────
# Public, machine-discoverable relay statuses. The detailed `reason` strings
# (e.g. no-trustworthy-chat-composer, send-preflight-*) remain as the secondary
# diagnostic; these canonical tokens are what a checkpoint session / CI can
# assert on. The relay NEVER claims success unless the intended ChatGPT
# composer is positively identified AND the send is confirmed (fail closed).
STATUS_TARGET_NOT_CONFIRMED = "TARGET_NOT_CONFIRMED"            # composer identity ambiguous/absent before injection
STATUS_INJECTION_NOT_CONFIRMED = "INJECTION_NOT_CONFIRMED"      # intended composer identified but text not written/verified
STATUS_DELIVERY_CONFIRMED = "DELIVERY_CONFIRMED"                # composer cleared + user turn +1 (or auxiliary evidence)
# Evidence discriminator: NOT_SENT vs POSSIBLY_SENT_UNCONFIRMED.
STATUS_NOT_SENT = "NOT_SENT"                                    # zero mutation / rolled back / draft still present
STATUS_POSSIBLY_SENT_UNCONFIRMED = "POSSIBLY_SENT_UNCONFIRMED"  # composer cleared but thread not yet confirmed


def relay_injection_status(resolved, resolve_reason, injected, verified):
    """Pure mapping from the Phase-A injection decision to a canonical token.

    Returns one of the canonical tokens, or None when the relay should proceed
    to Phase B (composer positively identified AND text written AND verified).
    Deterministic and browser-free so it is unit-testable.
    """
    if not resolved:
        return STATUS_TARGET_NOT_CONFIRMED
    if not injected or not verified:
        return STATUS_INJECTION_NOT_CONFIRMED
    return None


def delivery_canonical_status(send_status):
    """Map a SendConfirmation status to the public canonical token.

    The DELIVERY_CONFIRMED_* family collapses to DELIVERY_CONFIRMED. The
    ambiguous SEND_PENDING_TIMEOUT (composer cleared, thread unconfirmed) is the
    only POSSIBLY_SENT_UNCONFIRMED evidence; SEND_NOT_CONFIRMED /
    SEND_BUTTON_UNAVAILABLE mean the draft never left the composer -> NOT_SENT.
    This preserves the distinction between "not sent" and "possibly injected but
    unconfirmed".
    """
    if send_status and send_status.startswith("DELIVERY_CONFIRMED"):
        return STATUS_DELIVERY_CONFIRMED
    if send_status == "SEND_PENDING_TIMEOUT":
        return STATUS_POSSIBLY_SENT_UNCONFIRMED
    return STATUS_NOT_SENT


class ChatComposerTarget:
    """Preflight + scoped injection/verification/rollback for the chat composer.

    CDP mechanics are injected as an async `js` callable (same pattern as
    AttachmentUploader / SendConfirmation) so the orchestration logic is
    unit-testable without a live browser. `enumerate_surfaces` may be injected
    to bypass the live DOM enumeration in tests.
    """

    def __init__(self, js, enumerate_surfaces=None):
        self.js = js
        self._enumerate = enumerate_surfaces or self._enumerate_live

    async def _enumerate_live(self):
        raw = await self.js(ENUMERATE_SURFACES_JS)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = []
        if not isinstance(raw, list):
            raw = []
        return raw

    async def resolve(self):
        """Enumerate + pure selection. Returns (ok, target, reason)."""
        surfaces = await self._enumerate()
        return select_trustworthy_chat_composer(surfaces)

    async def inject(self, target, text):
        """Write text into the selected node. Captures pre-mutation content for
        rollback. Returns (ok, pre_content)."""
        node_css = target["node_css"]
        pre = await self._read(node_css)
        target["pre_content"] = pre
        ok = await self._write(node_css, text)
        if ok:
            target["_injected"] = True
        return bool(ok), pre

    async def verify(self, target, text):
        """Confirm the injected text is present in the selected node."""
        cur = await self._read(target["node_css"]) or ""
        return text.strip() in cur

    async def rollback(self, target):
        """Restore the selected node to its pre-mutation content. An empty
        pre-content is written back as empty, so no orphan draft remains. A
        target that never reached injection is left untouched (no mutation)."""
        if not target.get("_injected"):
            return
        pre = target.get("pre_content") or ""
        await self._write(target["node_css"], pre)

    async def is_cleared(self, node_css):
        """True when the selected composer node holds no non-whitespace text."""
        cur = await self._read(node_css) or ""
        return cur.strip() == ""

    async def _phase_b_preflight(self, expected_target):
        """Pre-send preflight: re-enumerate the DOM and PROVE the resolved
        composer is STILL the same one that received + verified the checkpoint,
        then locate the trusted send control bound to that same composer
        container/form. A send control may first appear only after text was
        injected, so its presence is validated here, not in Phase A.

        Returns (ok, send_css, reason). On any failure the caller must roll back
        the checkpoint and fail closed (no click)."""
        surfaces = await self._enumerate()
        ok, current, reason = select_trustworthy_chat_composer(surfaces)
        if not ok:
            return False, None, f"send-preflight-{reason}"
        if not _same_target(current, expected_target):
            return False, None, "target-drift"
        if current.get("send_control_type") == "voice":
            return False, None, "voice-only-control"
        send_css = current.get("send_css")
        if not send_css or not current.get("send_enabled"):
            return False, None, "no-trusted-send-control"
        return True, send_css, None

    async def click_send(self, expected_target=None):
        """Phase B: preflight before EACH send and PROVE the resolved target is
        STILL the same composer that received + verified the checkpoint, then
        click the send control bound to that same composer container/form. The
        send control must be a real send control (voice/dictation excluded) and
        enabled. Any identity mismatch, target disappearance, reorder ambiguity,
        absent/disabled send control, voice-only control, OR a send control that
        vanishes / goes disabled at the instant of the click fails closed: the
        checkpoint is rolled back and NO click is dispatched (no complex
        auto-recovery)."""
        if expected_target is None:
            print("RELAY_TARGET_DRIFT: no bound target to preflight against; "
                  "refusing to click any send control.")
            return False
        ok, send_css, reason = await self._phase_b_preflight(expected_target)
        if not ok:
            print(f"RELAY_TARGET_DRIFT: {reason}; rolling back checkpoint, "
                  f"no click.")
            await self.rollback(expected_target)
            return False
        clicked = await self._click(send_css)
        if not clicked:
            # The preflight proved the send control a moment earlier, but the
            # click itself did not dispatch (button disappeared / became
            # disabled in between). The injected checkpoint must not be left in
            # the composer: roll back to the exact pre-mutation content.
            print("RELAY_TARGET_DRIFT: send control unavailable at click instant "
                  "(disappeared or disabled after preflight); rolling back "
                  "checkpoint, no click dispatched.")
            await self.rollback(expected_target)
            return False
        return True

    async def _read(self, node_css):
        return await self.js(READ_NODE_JS % _js_str(node_css)) or ""

    async def _write(self, node_css, text):
        # WRITE_NODE_JS fills the same text into both the <textarea> .value branch
        # and the contenteditable innerText branch.
        return await self.js(WRITE_NODE_JS % (_js_str(node_css), _js_str(text), _js_str(text)))

    async def _click(self, css):
        return await self.js(CLICK_SEND_JS % _js_str(css))


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
            """B4 (F6, token-free): PNG capture. ALWAYS available for anomaly
            paths (truncation detection, read-back timeout) — system-initiated,
            no user consent and no waiting for a request. GOVERLOOP_SCREENSHOT_DIR
            only adds extra forensics captures; production default is zero
            screenshots on the normal path. Never analysed automatically."""
            try:
                shot = await cmd("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, session=sid)
                data = (shot.get("result") or {}).get("data")
                if not data:
                    return None
                base = args.screenshot_dir or os.path.dirname(os.path.abspath(args.output_file))
                os.makedirs(base, exist_ok=True)
                p = os.path.join(base, f"{name}.png")
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

        # ── Chat composer targeting: Phase A pre-mutation preflight ─────────
        # Before ANY DOM mutation, enumerate editable surfaces and select the
        # single trustworthy chat composer (fail closed when 0 or >1). A send
        # control need NOT exist yet (newer ChatGPT renders it only after text).
        # Inject only into the selected node, verify the payload landed, and
        # ROLL BACK to the pre-mutation content on verification failure so no
        # orphan draft is left behind. Send-control trust is enforced in Phase B
        # (pre-send). See ChatComposerTarget / select_trustworthy_chat_composer.
        send_confirm_timeout = getattr(args, "send_confirm_timeout", SEND_CONFIRM_TIMEOUT)
        send_pending_timeout = getattr(args, "send_pending_timeout", SEND_PENDING_TIMEOUT)

        composer_target = ChatComposerTarget(js)
        resolved, target, reason = await composer_target.resolve()
        if not resolved:
            # Composer identity not positively confirmed -> fail closed, zero
            # mutation. Surface the canonical token (detailed reason kept).
            print(f"Error: {STATUS_TARGET_NOT_CONFIRMED} ({reason}). "
                  f"Refusing to mutate any composer.")
            return 1

        ok, _pre = await composer_target.inject(target, request_text)
        if not ok:
            print(f"Error: {STATUS_INJECTION_NOT_CONFIRMED} (write-failed). "
                  f"Refusing to send; {STATUS_NOT_SENT} (no mutation left behind).")
            return 1

        await asyncio.sleep(0.5)  # let the input event settle before verifying
        if not await composer_target.verify(target, request_text):
            # Intended composer was identified, but the injected text could not
            # be verified in it -> roll back so no orphan draft remains.
            print(f"Error: {STATUS_INJECTION_NOT_CONFIRMED} (verify-failed: text not "
                  f"present in selected composer). Rolling back to pre-mutation "
                  f"content; {STATUS_NOT_SENT}, no orphan draft left behind.")
            await composer_target.rollback(target)
            return 1

        node_css = target["node_css"]

        async def _click_send():
            # Phase B (pre-send): re-enumerate and prove the resolved target is
            # STILL the same composer that received + verified the checkpoint,
            # then click the send control bound to that same composer
            # container/form (voice/dictation excluded, must be enabled). Any
            # identity mismatch / disappearance / reorder ambiguity / absent
            # send / voice-only control -> rollback the checkpoint + fail closed
            # (no click, no complex auto-recovery).
            return await composer_target.click_send(expected_target=target)

        async def _composer_cleared():
            # Scoped to the selected composer node (no first-match heuristic).
            return await composer_target.is_cleared(node_css)

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
            # SEND_BUTTON_UNAVAILABLE / SEND_NOT_CONFIRMED / SEND_PENDING_TIMEOUT.
            # The SendConfirmation state machine already printed the detailed
            # reason. Surface the canonical evidence discriminator so a "not
            # sent" outcome is distinguishable from "possibly injected but
            # unconfirmed" (composer cleared, thread not yet confirmed). Never
            # resend from here.
            print(f"CHECKPOINT_NOT_DELIVERED: {delivery_canonical_status(send_status)} "
                  f"({send_status}).")
            return 1
        print(f"CHECKPOINT_DELIVERED: {STATUS_DELIVERY_CONFIRMED} ({send_status}).")

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
        diag_recovery = "n/a"
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
                    # B4 auto-fallback (F6, system-initiated — no user consent,
                    # no waiting for a request): if the reply still looks
                    # truncated (e.g. an envelope JSON cut mid-string), the relay
                    # proactively captures a token-free screenshot AND performs a
                    # short recovery re-read of the same node. This is the
                    # proactive remediation for GPT conversation truncation.
                    if _looks_truncated(final_text):
                        await _capture_screenshot(f"{req_id}-truncated")
                        diag_recovery = "truncated-evidence"
                        recovery_deadline = time.time() + RECOVERY_SECONDS
                        while time.time() < recovery_deadline:
                            await asyncio.sleep(2)
                            t2 = str((await _snapshot()).get("text") or "").strip()
                            if t2 and not _looks_truncated(t2):
                                final_text = t2
                                diag_recovery = "recovered"
                                break
                    else:
                        diag_recovery = "none"
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
                    "recovery": diag_recovery,
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
            await _capture_screenshot(f"{req_id}-timeout")  # B4 auto fallback (anomaly path)
            return 1

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
