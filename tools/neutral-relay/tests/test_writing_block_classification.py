"""Issue 1 regressions for the inWritingBlock() JS classifier.

The classifier lives inside the embedded ENUMERATE_SURFACES_JS snippet, so these
tests execute the REAL JS (extracted from neutral_relay.py) in Node with a small
DOM stub instead of re-implementing the logic in Python. This keeps the test
honest to the shipped snippet. Skipped when Node is unavailable.
"""
import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import neutral_relay as nr


def _extract_in_writing_block():
    m = re.search(r'function inWritingBlock\(el\) \{.*?\n  \}', nr.ENUMERATE_SURFACES_JS, re.S)
    if not m:
        raise AssertionError("could not extract inWritingBlock() from ENUMERATE_SURFACES_JS")
    return m.group(0)


_NODE_TEMPLATE = r"""
const assert = require('assert');

// Minimal DOM stub: only what inWritingBlock() touches (nodeType, tagName,
// getAttribute, children, parentElement).
function el(tag, attrs, children, parent) {
  return {
    nodeType: 1,
    tagName: tag,
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    children: children || [],
    parentElement: parent || null,
  };
}

__FUNC__

// Scenario 1: normal ProseMirror chat composer must remain eligible.
(function () {
  const page = el('MAIN', {}, [], null);
  const chat = el('DIV', { 'class': 'chat-container' }, [], page);
  const editor = el('DIV', { 'class': 'ProseMirror', 'contenteditable': 'true' }, [], chat);
  page.children = [chat];
  chat.children = [editor];
  assert.strictEqual(inWritingBlock(editor), false,
    'normal ProseMirror chat composer stays eligible');
})();

// Scenario 2: writing-block + real chat composer on same page -> only the
// writing-block is excluded.
(function () {
  const page = el('MAIN', {}, [], null);
  const wb = el('DIV', { 'class': 'writing-block' }, [], page);
  const wbHeader = el('DIV', { 'data-testid': 'writing-block-header' }, [], wb);
  const wbEditor = el('DIV', { 'class': 'ProseMirror', 'contenteditable': 'true' }, [], wb);
  const chat = el('DIV', { 'class': 'chat-container' }, [], page);
  const chatEditor = el('DIV', { 'class': 'ProseMirror', 'contenteditable': 'true' }, [], chat);
  page.children = [wb, chat];
  wb.children = [wbHeader, wbEditor];
  chat.children = [chatEditor];
  assert.strictEqual(inWritingBlock(wbEditor), true,
    'writing-block editor is excluded');
  assert.strictEqual(inWritingBlock(chatEditor), false,
    'real chat composer on the same page stays eligible');
})();

// Scenario 3: shared high-level ancestor containing a writing-block toolbar must
// not misclassify an unrelated chat composer (markers are NOT direct children of
// the shared ancestor, so they must not contaminate classification).
(function () {
  const page = el('MAIN', {}, [], null);
  const toolbar = el('DIV', {}, [], page);
  const toolbarHeader = el('DIV', { 'data-testid': 'writing-block-header' }, [], toolbar);
  const magicEdit = el('BUTTON', { 'class': 'magic-edit' }, [], toolbar);
  const chat = el('DIV', { 'class': 'chat-container' }, [], page);
  const chatEditor = el('DIV', { 'class': 'ProseMirror', 'contenteditable': 'true' }, [], chat);
  page.children = [toolbar, chat];
  toolbar.children = [toolbarHeader, magicEdit];
  chat.children = [chatEditor];
  assert.strictEqual(inWritingBlock(chatEditor), false,
    'unrelated chat composer under a shared ancestor is not misclassified');
})();

console.log('ALL_PASS');
"""


def _node_script():
    return _NODE_TEMPLATE.replace("__FUNC__", _extract_in_writing_block())


class TestWritingBlockClassification(unittest.TestCase):
    def test_real_js_classifier_regressions(self):
        try:
            proc = subprocess.run(
                ["node", "-e", _node_script()],
                capture_output=True, text=True, timeout=60,
            )
        except (FileNotFoundError, OSError) as e:
            raise unittest.SkipTest(f"node unavailable, skipping JS classifier test: {e}")
        self.assertEqual(
            proc.returncode, 0,
            f"node classifier failed:\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
        )
        self.assertIn("ALL_PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
