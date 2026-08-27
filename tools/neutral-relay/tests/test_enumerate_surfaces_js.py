"""Real-JS regression for ENUMERATE_SURFACES_JS composer collapsing.

ChatGPT renders ONE composer as TWO editables inside the same
<form class="group/composer">: a hidden fallback <textarea> and the visible
contenteditable ProseMirror. The enumerate snippet must collapse them to a
single surface (canonical editable = the visible contenteditable) so Phase A
does not see two candidates and fail closed with "ambiguous". The REAL JS is
executed in Node against a minimal DOM stub; each scenario prints its surfaces
JSON, and Python feeds that output into select_trustworthy_chat_composer() to
assert Phase A selection semantics. Skipped when Node is unavailable.
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import neutral_relay as nr

_NODE_TEMPLATE = r"""
const assert = require('assert');

const ALL = [];
function el(tag, attrs = {}, children = [], parent = null) {
  const n = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    children: [],
    parentElement: null,
    classList: (attrs.class || '').split(/\s+/).filter(Boolean),
    offsetWidth: 100,
    offsetHeight: 30,
    isContentEditable: !!attrs._ce,
  };
  if (attrs.id) n.id = attrs.id;
  if (attrs._disabled) n.disabled = true;
  if (attrs._visible === false) { n.offsetWidth = 0; n.offsetHeight = 0; }
  if (attrs._ce) { n.offsetWidth = 20; n.offsetHeight = 40; }
  n.getClientRects = () => (n.offsetWidth ? [{}] : []);
  n.querySelectorAll = (sel) => {
    const out = [];
    (function walk(node) { for (const c of node.children) { out.push(c); walk(c); } })(n);
    if (sel === 'button') return out.filter((c) => c.tagName === 'BUTTON');
    return [];
  };
  n.querySelector = (sel) => {
    const out = [];
    (function walk(node) { for (const c of node.children) { out.push(c); walk(c); } })(n);
    if (sel === 'button[data-testid="composer-plus-btn"]')
      return out.find((c) => c.getAttribute('data-testid') === 'composer-plus-btn') || null;
    return null;
  };
  ALL.push(n);
  if (parent) { n.parentElement = parent; parent.children.push(n); }
  for (const c of children) { c.parentElement = n; n.children.push(c); }
  return n;
}
const document = {
  querySelectorAll: (sel) => {
    if (sel === '[contenteditable="true"], textarea')
      return ALL.filter((n) => n.isContentEditable || n.tagName === 'TEXTAREA');
    return [];
  },
};
const window = { CSS: null };

__ENUM__

const scenarios = {};

// Scenario 1: real ChatGPT structure, EMPTY composer (Phase A, no send yet).
(function () {
  ALL.length = 0;
  const form = el('form', { class: 'group/composer w-full' });
  el('button', { 'data-testid': 'composer-plus-btn', 'aria-label': '添加文件等' }, [], form);
  el('button', { 'aria-label': '启动语音功能', class: 'composer-submit-button-color' }, [], form);
  const wrap = el('div', { class: 'wcDTda_prosemirror-parent' }, [], form);
  el('textarea', { class: 'wcDTda_fallbackTextarea', _visible: false }, [], wrap);
  el('div', { class: 'ProseMirror', _ce: true, id: 'prompt-textarea' }, [], wrap);
  const r = JSON.parse(__ENUM__);
  assert.strictEqual(r.length, 1, 'fallback textarea + ProseMirror collapse to ONE composer');
  assert.strictEqual(r[0].node_css, '#prompt-textarea', 'canonical editable is the visible ProseMirror');
  assert.strictEqual(r[0].editable_kind, 'contenteditable');
  assert.strictEqual(r[0].is_writing_block, false);
  assert.strictEqual(r[0].has_send, false, 'no send control before text (Phase A)');
  assert.strictEqual(r[0].send_control_type, null, 'voice/dictation button is never a send control');
  scenarios.scenario1 = r;
})();

// Scenario 2: after injection (Phase B) the send button appears.
(function () {
  ALL.length = 0;
  const form = el('form', { class: 'group/composer w-full' });
  el('button', { 'data-testid': 'composer-plus-btn', 'aria-label': '添加文件等' }, [], form);
  el('button', { 'aria-label': '启动语音功能', class: 'composer-submit-button-color' }, [], form);
  el('button', { 'data-testid': 'send-button', 'aria-label': 'Send message' }, [], form);
  const wrap = el('div', { class: 'wcDTda_prosemirror-parent' }, [], form);
  el('textarea', { class: 'wcDTda_fallbackTextarea', _visible: false }, [], wrap);
  el('div', { class: 'ProseMirror', _ce: true, id: 'prompt-textarea' }, [], wrap);
  const r = JSON.parse(__ENUM__);
  assert.strictEqual(r.length, 1);
  assert.strictEqual(r[0].node_css, '#prompt-textarea');
  assert.strictEqual(r[0].has_send, true, 'send control appears after text (Phase B)');
  assert.strictEqual(r[0].send_control_type, 'send');
  assert.strictEqual(r[0].send_enabled, true);
  assert.ok(r[0].send_css && /button/.test(r[0].send_css),
    'send_css resolves to a real send control (voice skipped; send_control_type===send)');
  scenarios.scenario2 = r;
})();

// Scenario 3: two SEPARATE composer forms must stay two candidates (fail closed).
(function () {
  ALL.length = 0;
  const f1 = el('form', { class: 'group/composer w-full' });
  el('button', { 'data-testid': 'composer-plus-btn' }, [], f1);
  const w1 = el('div', {}, [], f1);
  el('textarea', { class: 'wcDTda_fallbackTextarea', _visible: false }, [], w1);
  el('div', { class: 'ProseMirror', _ce: true, id: 'prompt-textarea' }, [], w1);
  const f2 = el('form', { class: 'group/composer w-full' });
  el('button', { 'data-testid': 'composer-plus-btn' }, [], f2);
  const w2 = el('div', {}, [], f2);
  el('div', { class: 'ProseMirror', _ce: true, id: 'prompt-textarea-2' }, [], w2);
  const r = JSON.parse(__ENUM__);
  assert.strictEqual(r.length, 2, 'two separate composer forms stay separate');
  scenarios.scenario3 = r;
})();

// Scenario 4: writing-block + real empty composer -> only real composer eligible.
(function () {
  ALL.length = 0;
  const wb = el('div', { class: 'writing-block' });
  el('div', { 'data-testid': 'writing-block-header' }, [], wb);
  el('button', { class: 'magic-edit' }, [], wb);
  el('div', { class: 'ProseMirror', _ce: true }, [], wb);
  const form = el('form', { class: 'group/composer w-full' });
  el('button', { 'data-testid': 'composer-plus-btn' }, [], form);
  const wrap = el('div', {}, [], form);
  el('textarea', { class: 'wcDTda_fallbackTextarea', _visible: false }, [], wrap);
  el('div', { class: 'ProseMirror', _ce: true, id: 'prompt-textarea' }, [], wrap);
  const r = JSON.parse(__ENUM__);
  assert.strictEqual(r.length, 2);
  const real = r.find((s) => s.node_css === '#prompt-textarea');
  const wbS = r.find((s) => s.node_css !== '#prompt-textarea');
  assert.ok(real && real.is_writing_block === false, 'real composer stays eligible');
  assert.ok(wbS && wbS.is_writing_block === true, 'writing-block editor is excluded');
  scenarios.scenario4 = r;
})();

// Scenario 5: NON-composer form with two unrelated editables -> NOT merged.
(function () {
  ALL.length = 0;
  const form = el('form', { class: 'some-settings-form' });
  el('textarea', { class: 'a', id: 'x' }, [], form);
  el('textarea', { class: 'b', id: 'y' }, [], form);
  const r = JSON.parse(__ENUM__);
  assert.strictEqual(r.length, 2, 'non-composer form: editables stay separate (fail-closed ambiguity)');
  scenarios.scenario5 = r;
})();

for (const [k, v] of Object.entries(scenarios)) {
  console.log('SCENARIO_' + k.replace('scenario', '') + ':' + JSON.stringify(v));
}
console.log('ALL_PASS');
"""


def _node_script():
    return _NODE_TEMPLATE.replace("__ENUM__", nr.ENUMERATE_SURFACES_JS)


class TestEnumerateSurfacesJs(unittest.TestCase):
    def test_real_js_composer_collapsing_and_selection(self):
        try:
            proc = subprocess.run(
                ["node", "-e", _node_script()],
                capture_output=True, text=True, timeout=60,
            )
        except (FileNotFoundError, OSError) as e:
            raise unittest.SkipTest(f"node unavailable, skipping enumerate JS test: {e}")
        self.assertEqual(
            proc.returncode, 0,
            f"node enumerate failed:\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )
        self.assertIn("ALL_PASS", proc.stdout)

        # Feed each scenario's real surfaces into the pure-Python Phase A
        # selection and assert the fail-closed semantics hold end to end.
        scenarios = {}
        for line in proc.stdout.splitlines():
            if line.startswith("SCENARIO_"):
                _, name = line.split(":", 1)
                key = line.split(":", 1)[0].split("_", 1)[1]
                scenarios[key] = __import__("json").loads(name)

        ok, target, reason = nr.select_trustworthy_chat_composer(scenarios["1"])
        self.assertTrue(ok, f"scenario1 (single empty composer) should select: {reason}")
        self.assertEqual(target["node_css"], "#prompt-textarea")

        ok, target, reason = nr.select_trustworthy_chat_composer(scenarios["2"])
        self.assertTrue(ok, f"scenario2 (send appears) should select: {reason}")
        self.assertEqual(target["send_control_type"], "send")

        ok, _, reason = nr.select_trustworthy_chat_composer(scenarios["3"])
        self.assertFalse(ok, "scenario3 (two composers) must fail closed")
        self.assertEqual(reason, "ambiguous-multiple-chat-composers")

        ok, target, reason = nr.select_trustworthy_chat_composer(scenarios["4"])
        self.assertTrue(ok, f"scenario4 (writing-block + real) should select real: {reason}")
        self.assertEqual(target["node_css"], "#prompt-textarea")

        ok, _, reason = nr.select_trustworthy_chat_composer(scenarios["5"])
        self.assertFalse(ok, "scenario5 (non-composer form, 2 editables) must fail closed")
        self.assertEqual(reason, "ambiguous-multiple-chat-composers")


if __name__ == "__main__":
    unittest.main()
