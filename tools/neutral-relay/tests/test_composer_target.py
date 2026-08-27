import asyncio
import json
import re
import sys
import os
import unittest

# Ensure neutral_relay can be imported (tests live in tools/neutral-relay/tests/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import neutral_relay as nr


def chat_surface(node_css="#chat-0", send_css="#send-0", has_send=True,
                 send_enabled=True, is_wb=False):
    return {
        "kind": "writing-block" if is_wb else "chat-composer",
        "node_css": node_css,
        "has_send": has_send,
        "send_enabled": send_enabled,
        "send_css": send_css,
        "is_writing_block": is_wb,
        "is_editor": is_wb,
    }


class FakeBrowser:
    """js() shim that implements the REAL enumerate/read/write/click snippets
    against an in-memory node store, so ChatComposerTarget's orchestration is
    exercised end-to-end (snippets included) without a live browser."""

    def __init__(self, surfaces, *, send_present=True, send_enabled=True,
                 corrupt_read=False):
        self._surfaces = json.dumps(surfaces) if isinstance(surfaces, list) else surfaces
        self.nodes = {}          # node_css -> text
        self.send_present = send_present
        self.send_enabled = send_enabled
        self.corrupt_read = corrupt_read
        self.clicks = 0
        self.clicked_css = []

    async def js(self, expr):
        if expr == nr.ENUMERATE_SURFACES_JS or expr.startswith(nr.ENUMERATE_SURFACES_JS.strip()):
            return self._surfaces
        if "innerHTML=''" in expr:  # WRITE
            m = re.search(
                r"document\.querySelector\((['\"])(.*?)\1\);if\(!e\)return false;e\.focus\(\);"
                r"e\.innerHTML='';e\.innerText=(.*?);e\.dispatchEvent", expr, re.S)
            if m:
                css = m.group(2)
                text = json.loads(m.group(3))
                self.nodes[css] = text
                return True
            return False
        if "b.click()" in expr:  # CLICK
            m = re.search(r"document\.querySelector\((['\"])(.*?)\1\); if\(b && !b\.disabled\)", expr)
            if m:
                css = m.group(2)
                if self.send_present and self.send_enabled:
                    self.clicks += 1
                    self.clicked_css.append(css)
                    return True
                return False
            return False
        # READ (fallback)
        m = re.search(r"document\.querySelector\((['\"])(.*?)\1\);return e\?\(e\.innerText", expr)
        if m:
            css = m.group(2)
            if self.corrupt_read:
                return ""  # simulate the injected text never landing
            return self.nodes.get(css, "")
        return None


async def run_inject_phase(fake, text):
    """Mirror the run_relay decision sequence: resolve -> inject -> verify ->
    rollback-on-fail. Returns (status, target, fake)."""
    target = nr.ChatComposerTarget(fake.js)
    ok, t, reason = await target.resolve()
    if not ok:
        return ("fail-closed-resolve:" + reason, None, fake)
    ok2, _pre = await target.inject(t, text)
    if not ok2:
        return ("fail-inject", t, fake)
    if not await target.verify(t, text):
        await target.rollback(t)
        return ("fail-verify-rolledback", t, fake)
    return ("ok", t, fake)


class TestSelectTrustworthy(unittest.TestCase):
    def test_normal_chat_passes(self):
        ok, t, reason = nr.select_trustworthy_chat_composer([chat_surface()])
        self.assertTrue(ok)
        self.assertEqual(t["node_css"], "#chat-0")
        self.assertEqual(t["kind"], "chat-composer")
        self.assertIsNone(reason)

    def test_writing_block_plus_chat_only_chat_selected(self):
        surfaces = [chat_surface(node_css="#wb", is_wb=True, has_send=False),
                    chat_surface(node_css="#chat-0")]
        ok, t, _ = nr.select_trustworthy_chat_composer(surfaces)
        self.assertTrue(ok)
        self.assertEqual(t["node_css"], "#chat-0")  # writing-block excluded

    def test_writing_block_only_fail_closed(self):
        ok, t, reason = nr.select_trustworthy_chat_composer(
            [chat_surface(node_css="#wb", is_wb=True, has_send=False)])
        self.assertFalse(ok)
        self.assertIsNone(t)
        self.assertEqual(reason, "no-trustworthy-chat-composer")

    def test_ambiguous_multiple_chat_fail_closed(self):
        surfaces = [chat_surface(node_css="#a"), chat_surface(node_css="#b")]
        ok, t, reason = nr.select_trustworthy_chat_composer(surfaces)
        self.assertFalse(ok)
        self.assertEqual(reason, "ambiguous-multiple-chat-composers")

    def test_stale_send_selector_fail_closed(self):
        # chat candidate exists but its send control is missing/disabled
        # (the stale send-button selector matched 0) -> fail closed.
        ok, t, reason = nr.select_trustworthy_chat_composer(
            [chat_surface(has_send=False, send_enabled=False, send_css=None)])
        self.assertFalse(ok)
        self.assertEqual(reason, "no-trustworthy-chat-composer")

    def test_empty_surfaces_fail_closed(self):
        ok, t, reason = nr.select_trustworthy_chat_composer([])
        self.assertFalse(ok)
        self.assertEqual(reason, "no-editable-surfaces")

    def test_missing_node_selector_fail_closed(self):
        s = chat_surface(node_css=None)
        ok, t, reason = nr.select_trustworthy_chat_composer([s])
        self.assertFalse(ok)
        self.assertEqual(reason, "selected-composer-missing-node-selector")

    def test_send_bound_to_container_not_editable(self):
        # The send control is bound to the composer container/form, and must
        # NOT be required to be a descendant of the editable node.
        s = chat_surface(node_css="#editable", send_css="#container > form button.send")
        ok, t, _ = nr.select_trustworthy_chat_composer([s])
        self.assertTrue(ok)
        self.assertNotEqual(t["send_css"], t["node_css"])
        self.assertEqual(t["send_css"], "#container > form button.send")


class TestComposerTargetOrchestration(unittest.TestCase):
    def test_normal_inject_verify_pass(self):
        fake = FakeBrowser([chat_surface()])
        status, t, fake = asyncio.run(run_inject_phase(fake, "CHECKPOINT TEXT"))
        self.assertEqual(status, "ok")
        self.assertEqual(fake.nodes["#chat-0"], "CHECKPOINT TEXT")

    def test_writing_block_only_zero_mutation(self):
        # Fail closed at resolve -> inject is NEVER called -> zero DOM mutation.
        fake = FakeBrowser([chat_surface(node_css="#wb", is_wb=True, has_send=False)])
        status, t, fake = asyncio.run(run_inject_phase(fake, "X"))
        self.assertTrue(status.startswith("fail-closed-resolve"))
        self.assertEqual(fake.nodes, {})  # zero mutation

    def test_ambiguous_zero_mutation(self):
        fake = FakeBrowser([chat_surface(node_css="#a"), chat_surface(node_css="#b")])
        status, t, fake = asyncio.run(run_inject_phase(fake, "X"))
        self.assertTrue(status.startswith("fail-closed-resolve"))
        self.assertEqual(fake.nodes, {})

    def test_stale_send_zero_mutation(self):
        fake = FakeBrowser([chat_surface(has_send=False, send_enabled=False, send_css=None)])
        status, t, fake = asyncio.run(run_inject_phase(fake, "X"))
        self.assertTrue(status.startswith("fail-closed-resolve"))
        self.assertEqual(fake.nodes, {})

    def test_verify_failed_rollback_no_orphan_draft(self):
        # NEW requirement: after injection, if verification fails, restore the
        # composer to its pre-mutation content; never leave an orphan draft.
        fake = FakeBrowser([chat_surface()], corrupt_read=True)
        status, t, fake = asyncio.run(run_inject_phase(fake, "CHECKPOINT TEXT"))
        self.assertEqual(status, "fail-verify-rolledback")
        # the selected node is restored to pre-mutation (empty) -> no orphan draft
        self.assertEqual(fake.nodes.get("#chat-0", ""), "")
        self.assertNotIn("CHECKPOINT TEXT", fake.nodes.get("#chat-0", ""))

    def test_verify_failed_rollback_restores_prior_content(self):
        # If the composer already had content, rollback must restore THAT, not
        # blank it.
        fake = FakeBrowser([chat_surface()])
        fake.nodes["#chat-0"] = "PRIOR DRAFT"
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        ok2, pre = asyncio.run(target.inject(t, "NEW TEXT"))
        self.assertTrue(ok2)
        self.assertEqual(pre, "PRIOR DRAFT")
        # force a verify failure by corrupting the readback
        fake.corrupt_read = True
        self.assertFalse(await_wrap(target.verify(t, "NEW TEXT")))
        asyncio.run(target.rollback(t))
        self.assertEqual(fake.nodes["#chat-0"], "PRIOR DRAFT")

    def test_click_send_uses_bound_send_control(self):
        fake = FakeBrowser([chat_surface(node_css="#editable", send_css="#container > button.send")])
        target = nr.ChatComposerTarget(fake.js)
        asyncio.run(target.resolve())
        self.assertTrue(await_wrap(target.click_send()))
        self.assertEqual(fake.clicks, 1)
        self.assertEqual(fake.clicked_css, ["#container > button.send"])

    def test_click_send_missing_send_css_fails(self):
        fake = FakeBrowser([chat_surface(has_send=True, send_enabled=True, send_css=None)])
        target = nr.ChatComposerTarget(fake.js)
        asyncio.run(target.resolve())
        self.assertFalse(await_wrap(target.click_send()))
        self.assertEqual(fake.clicks, 0)

    def test_click_send_repreflight_drift_fail_closed(self):
        # PO: re-preflight before EACH send; dynamic DOM reorder -> fail closed,
        # no complex auto-recovery.
        fake = FakeBrowser([chat_surface()])
        target = nr.ChatComposerTarget(fake.js)
        asyncio.run(target.resolve())
        self.assertTrue(await_wrap(target.click_send()))
        self.assertEqual(fake.clicks, 1)
        # Simulate a DOM reorder: the trustworthy target is gone on the next send.
        fake._surfaces = json.dumps(
            [chat_surface(node_css="#c", has_send=False, send_enabled=False, send_css=None)])
        self.assertFalse(await_wrap(target.click_send()))
        self.assertEqual(fake.clicks, 1)  # no extra click dispatched after drift

    def test_is_cleared_scoped_to_selected_node(self):
        fake = FakeBrowser([chat_surface()])
        target = nr.ChatComposerTarget(fake.js)
        fake.nodes["#chat-0"] = "still here"
        self.assertFalse(await_wrap(target.is_cleared("#chat-0")))
        fake.nodes["#chat-0"] = "   "
        self.assertTrue(await_wrap(target.is_cleared("#chat-0")))


def await_wrap(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
