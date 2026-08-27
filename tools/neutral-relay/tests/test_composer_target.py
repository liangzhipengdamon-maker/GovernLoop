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
                 send_enabled=True, is_wb=False, editable_kind="contenteditable",
                 container_css=None, send_control_type=None):
    if send_control_type is None:
        send_control_type = "send" if has_send else None
    return {
        "kind": "writing-block" if is_wb else "chat-composer",
        "node_css": node_css,
        "has_send": has_send,
        "send_enabled": send_enabled,
        "send_css": send_css,
        "send_control_type": send_control_type,
        "container_css": container_css,
        "editable_kind": editable_kind,
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

    def _editable_kind_of(self, css):
        try:
            s_list = json.loads(self._surfaces) if isinstance(self._surfaces, str) else self._surfaces
        except (TypeError, ValueError, json.JSONDecodeError):
            return "contenteditable"
        for s in s_list:
            if s.get("node_css") == css:
                k = s.get("editable_kind")
                if k:
                    return k
        return "contenteditable"

    async def js(self, expr):
        if expr == nr.ENUMERATE_SURFACES_JS or expr.startswith(nr.ENUMERATE_SURFACES_JS.strip()):
            return self._surfaces
        if "e.focus()" in expr and "dispatchEvent(new Event('input'" in expr:  # WRITE
            m = re.search(
                r"document\.querySelector\((['\"])(.*?)\1\);if\(!e\)return false;e\.focus\(\);",
                expr, re.S)
            if not m:
                return False
            css = m.group(2)
            kind = self._editable_kind_of(css)
            if kind == "textarea":
                m2 = re.search(r"\{e\.value=(.*?);\}", expr, re.S)
                if not m2:
                    return False
                self.nodes[css] = json.loads(m2.group(1))
                return True
            if kind == "contenteditable":
                m2 = re.search(r"e\.innerHTML='';e\.innerText=(.*?);", expr, re.S)
                if not m2:
                    return False
                self.nodes[css] = json.loads(m2.group(1))
                return True
            return False  # unsupported editable kind -> write refuses (fail closed)
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
        # READ
        m = re.search(r"document\.querySelector\((['\"])(.*?)\1\);if\(!e\)return '';", expr)
        if m:
            css = m.group(2)
            if self.corrupt_read:
                return ""  # simulate the injected text never landing
            kind = self._editable_kind_of(css)
            if kind not in ("textarea", "contenteditable"):
                return None  # unsupported editable kind -> fail closed
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

    def test_empty_composer_without_send_is_phase_a_target(self):
        # Newer ChatGPT renders the send button only after text is present, so
        # Phase A must NOT require a send control: an empty-but-real composer is
        # a valid target. Send-control trust is enforced in Phase B (pre-send).
        ok, t, reason = nr.select_trustworthy_chat_composer(
            [chat_surface(has_send=False, send_enabled=False, send_css=None)])
        self.assertTrue(ok)
        self.assertEqual(t["node_css"], "#chat-0")
        self.assertIsNone(reason)
        self.assertIsNone(t["send_css"])

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
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        self.assertTrue(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 1)
        self.assertEqual(fake.clicked_css, ["#container > button.send"])

    def test_click_send_missing_send_css_fails(self):
        fake = FakeBrowser([chat_surface(has_send=True, send_enabled=True, send_css=None)])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        self.assertFalse(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 0)

    def test_click_send_repreflight_drift_fail_closed(self):
        # PO: re-preflight before EACH send; dynamic DOM reorder -> fail closed,
        # no complex auto-recovery.
        fake = FakeBrowser([chat_surface()])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        self.assertTrue(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 1)
        # Simulate a DOM reorder: the trustworthy target is gone on the next send.
        fake._surfaces = json.dumps(
            [chat_surface(node_css="#c", has_send=False, send_enabled=False, send_css=None)])
        self.assertFalse(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 1)  # no extra click dispatched after drift

    def test_is_cleared_scoped_to_selected_node(self):
        fake = FakeBrowser([chat_surface()])
        target = nr.ChatComposerTarget(fake.js)
        fake.nodes["#chat-0"] = "still here"
        self.assertFalse(await_wrap(target.is_cleared("#chat-0")))
        fake.nodes["#chat-0"] = "   "
        self.assertTrue(await_wrap(target.is_cleared("#chat-0")))


class TestClickSendSameTargetIdentity(unittest.TestCase):
    """Issue 2 regressions: send re-preflight must prove the target is STILL the
    same composer that received + verified the checkpoint, else fail closed
    (no click, no complex auto-recovery)."""

    def test_same_target_after_repreflight_send_allowed(self):
        fake = FakeBrowser([chat_surface(node_css="#a", send_css="#send-a", container_css="#cont-a")])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        self.assertTrue(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 1)
        self.assertEqual(fake.clicked_css, ["#send-a"])

    def test_original_disappears_and_b_unique_fail_closed(self):
        # original composer A disappears; B becomes the only trustworthy composer
        # -> re-preflight resolves a DIFFERENT target -> fail closed, no click.
        fake = FakeBrowser([chat_surface(node_css="#a", send_css="#send-a")])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        fake._surfaces = json.dumps([chat_surface(node_css="#b", send_css="#send-b")])
        self.assertFalse(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 0)

    def test_send_control_changed_fail_closed(self):
        # same composer node but the send control moved to a different
        # container/control -> identity mismatch -> fail closed, no click.
        fake = FakeBrowser([chat_surface(node_css="#a", send_css="#send-a", container_css="#cont-a")])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        fake._surfaces = json.dumps(
            [chat_surface(node_css="#a", send_css="#send-b", container_css="#cont-b")])
        self.assertFalse(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 0)

    def test_send_control_changed_same_node_fail_closed(self):
        # node_css matches but send_css differs -> still an identity mismatch.
        fake = FakeBrowser([chat_surface(node_css="#a", send_css="#send-a")])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        fake._surfaces = json.dumps([chat_surface(node_css="#a", send_css="#send-b")])
        self.assertFalse(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 0)

    def test_no_bound_target_fail_closed(self):
        # clicking without a bound target cannot prove same-target identity.
        fake = FakeBrowser([chat_surface()])
        target = nr.ChatComposerTarget(fake.js)
        self.assertFalse(await_wrap(target.click_send()))
        self.assertEqual(fake.clicks, 0)


class TestEditableKindReadWrite(unittest.TestCase):
    """Issue 3 regressions: textarea reads/writes via .value, contenteditable
    unchanged, unsupported editable kind fails closed."""

    def test_textarea_read_write_verify(self):
        fake = FakeBrowser([chat_surface(node_css="#ta", editable_kind="textarea")])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        ok2, _pre = asyncio.run(target.inject(t, "CHECKPOINT TEXT"))
        self.assertTrue(ok2)
        self.assertTrue(await_wrap(target.verify(t, "CHECKPOINT TEXT")))
        self.assertEqual(fake.nodes["#ta"], "CHECKPOINT TEXT")

    def test_textarea_rollback_restores_prior_value(self):
        fake = FakeBrowser([chat_surface(node_css="#ta", editable_kind="textarea")])
        fake.nodes["#ta"] = "PRIOR DRAFT"
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        ok2, pre = asyncio.run(target.inject(t, "NEW TEXT"))
        self.assertTrue(ok2)
        self.assertEqual(pre, "PRIOR DRAFT")
        fake.corrupt_read = True
        self.assertFalse(await_wrap(target.verify(t, "NEW TEXT")))
        asyncio.run(target.rollback(t))
        self.assertEqual(fake.nodes["#ta"], "PRIOR DRAFT")

    def test_contenteditable_behavior_unchanged(self):
        fake = FakeBrowser([chat_surface()])  # default editable_kind=contenteditable
        status, t, fake = asyncio.run(run_inject_phase(fake, "CHECKPOINT TEXT"))
        self.assertEqual(status, "ok")
        self.assertEqual(fake.nodes["#chat-0"], "CHECKPOINT TEXT")

    def test_unsupported_editable_kind_fails_closed(self):
        fake = FakeBrowser([chat_surface(node_css="#unknown", editable_kind="unknown")])
        status, t, fake = asyncio.run(run_inject_phase(fake, "TEXT"))
        self.assertEqual(status, "fail-inject")  # write refused, zero mutation
        self.assertEqual(fake.nodes, {})


class TestTwoPhaseModel(unittest.TestCase):
    """PR #122 two-phase targeting regressions: Phase A (pre-mutation composer
    preflight, no send control required) + Phase B (pre-send re-preflight that
    proves same-target identity and locates a trusted send control, excluding
    voice/dictation). Any Phase B failure rolls back the checkpoint and fails
    closed with NO click."""

    def test_phase_a_empty_composer_no_send_then_send_appears(self):
        # empty composer, no send initially -> Phase A allowed; after inject the
        # send control appears -> Phase B proves same target -> click succeeds.
        fake = FakeBrowser([chat_surface(node_css="#chat-0", has_send=False,
                                         send_enabled=False, send_css=None)])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)  # Phase A does NOT require a send control
        self.assertIsNone(t["send_css"])
        ok2, _pre = asyncio.run(target.inject(t, "CHECKPOINT TEXT"))
        self.assertTrue(ok2)
        self.assertTrue(await_wrap(target.verify(t, "CHECKPOINT TEXT")))
        # ChatGPT now renders the send button (text is present).
        fake._surfaces = json.dumps([chat_surface(node_css="#chat-0", has_send=True,
                                                  send_enabled=True, send_css="#send-0")])
        self.assertTrue(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 1)
        self.assertEqual(fake.clicked_css, ["#send-0"])

    def test_phase_b_send_never_appears_rollback_fail_closed(self):
        # empty composer, no send initially -> inject OK, but the send control
        # never appears -> Phase B rolls back the checkpoint and fails closed
        # (no orphan draft, no click).
        fake = FakeBrowser([chat_surface(node_css="#chat-0", has_send=False,
                                         send_enabled=False, send_css=None)])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        ok2, _pre = asyncio.run(target.inject(t, "CHECKPOINT TEXT"))
        self.assertTrue(ok2)
        self.assertTrue(await_wrap(target.verify(t, "CHECKPOINT TEXT")))
        # surfaces stay send-less -> Phase B fails -> rollback -> no orphan.
        self.assertFalse(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 0)
        self.assertEqual(fake.nodes.get("#chat-0", ""), "")  # restored empty
        self.assertNotIn("CHECKPOINT TEXT", fake.nodes.get("#chat-0", ""))

    def test_voice_button_not_treated_as_send(self):
        # A voice/dictation control must never be used as the send control.
        fake = FakeBrowser([chat_surface(node_css="#chat-0", has_send=True,
                                         send_enabled=True, send_css="#voice-btn",
                                         send_control_type="voice")])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        ok2, _pre = asyncio.run(target.inject(t, "CHECKPOINT TEXT"))
        self.assertTrue(ok2)
        self.assertFalse(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 0)  # voice control is NOT clicked
        self.assertEqual(fake.nodes.get("#chat-0", ""), "")  # rollback, no orphan

    def test_phase_b_resolves_different_composer_rollback_fail_closed(self):
        # composer A injected; Phase B resolves a DIFFERENT composer -> rollback
        # the checkpoint + fail closed, no click.
        fake = FakeBrowser([chat_surface(node_css="#a", send_css="#send-a")])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        ok2, _pre = asyncio.run(target.inject(t, "CHECKPOINT TEXT"))
        self.assertTrue(ok2)
        fake._surfaces = json.dumps([chat_surface(node_css="#b", send_css="#send-b")])
        self.assertFalse(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 0)
        self.assertEqual(fake.nodes.get("#a", ""), "")  # A restored, no orphan

    def test_writing_block_plus_real_empty_composer_only_real_selected(self):
        # writing-block + a real empty composer (no send yet) -> Phase A selects
        # only the real composer.
        surfaces = [chat_surface(node_css="#wb", is_wb=True, has_send=False),
                    chat_surface(node_css="#chat-0", has_send=False,
                                 send_enabled=False, send_css=None)]
        ok, t, _ = nr.select_trustworthy_chat_composer(surfaces)
        self.assertTrue(ok)
        self.assertEqual(t["node_css"], "#chat-0")
        self.assertFalse(t["send_css"])

    def test_textarea_phase_a_then_send_appears(self):
        # textarea read/write semantics stay correct through both phases.
        fake = FakeBrowser([chat_surface(node_css="#ta", editable_kind="textarea",
                                         has_send=False, send_enabled=False, send_css=None)])
        target = nr.ChatComposerTarget(fake.js)
        ok, t, _ = asyncio.run(target.resolve())
        self.assertTrue(ok)
        ok2, _pre = asyncio.run(target.inject(t, "CHECKPOINT TEXT"))
        self.assertTrue(ok2)
        self.assertTrue(await_wrap(target.verify(t, "CHECKPOINT TEXT")))
        fake._surfaces = json.dumps([chat_surface(node_css="#ta", editable_kind="textarea",
                                                  has_send=True, send_enabled=True, send_css="#send-ta")])
        self.assertTrue(await_wrap(target.click_send(expected_target=t)))
        self.assertEqual(fake.clicks, 1)
        self.assertEqual(fake.clicked_css, ["#send-ta"])
        self.assertEqual(fake.nodes["#ta"], "CHECKPOINT TEXT")

    def test_phase_b_preflight_pass_but_click_unavailable_rollback_fail_closed(self):
        # Phase B preflight PASS (the re-enumerated surfaces advertise a trusted
        # send control), but at the instant of the click the control is gone or
        # disabled -> CLICK_SEND_JS returns false -> NO click, the checkpoint is
        # rolled back, and the composer is restored to its pre-mutation content.
        async def scenario(send_present, send_enabled):
            fake = FakeBrowser([chat_surface(node_css="#chat-0", has_send=False,
                                             send_enabled=False, send_css=None)])
            target = nr.ChatComposerTarget(fake.js)
            ok, t, _ = await target.resolve()
            self.assertTrue(ok)  # Phase A: empty composer without send is valid
            ok2, _pre = await target.inject(t, "CHECKPOINT TEXT")
            self.assertTrue(ok2)
            self.assertTrue(await target.verify(t, "CHECKPOINT TEXT"))
            # Phase B re-enumerates: a trusted send control is visible + enabled.
            fake._surfaces = json.dumps([chat_surface(node_css="#chat-0", has_send=True,
                                                      send_enabled=True, send_css="#send-0")])
            # ...but between preflight and the click it vanishes / goes disabled.
            fake.send_present = send_present
            fake.send_enabled = send_enabled
            self.assertFalse(await target.click_send(expected_target=t))
            self.assertEqual(fake.clicks, 0)  # NO click dispatched
            self.assertEqual(fake.nodes.get("#chat-0", ""), "")  # rollback, no orphan draft

        asyncio.run(scenario(send_present=False, send_enabled=True))  # disappeared
        asyncio.run(scenario(send_present=True, send_enabled=False))  # disabled


def await_wrap(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
