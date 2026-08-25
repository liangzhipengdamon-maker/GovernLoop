#!/usr/bin/env python3
"""GovernLoop session manager + checkpoint reporter.

WorkBuddy slash-command entrypoint for GovernLoop (`/governloop`). Creates and
manages a task/session-level GovernLoop session, binds a ChatGPT conversation
URL in TEMPORARY state only (never the canonical config), and reports review
checkpoints (text + evidence attachments) to the bound conversation through the
GovernLoop Neutral Relay.

Subcommands:
  new                       create/resume a session (auto repo/task/session-id)
  status                    show current session state
  bind <conversation-url>   store a ChatGPT conversation URL in temp session state
  checkpoint <TYPE>         report a review checkpoint (text + evidence attachments)
  end [--final]             optionally send FINAL_VERIFICATION, then remove temp state

Exit codes:
  0  success
  1  error (including CHECKPOINT_DELIVERY_INCOMPLETE)
  3  USER_CONVERSATION_SELECTION_REQUIRED
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request

# --------------------------------------------------------------------------
# Constants / environment (all injectable via env for tests)
# --------------------------------------------------------------------------
STATE_DIR_DEFAULT = "/tmp"
DEFAULT_CDP_PORT = int(os.environ.get("GOVERLOOP_CDP_PORT", "9233"))


def _default_relay_path():
    """Resolve the Neutral Relay for the current runtime.

    Installed Phase 2B bundle: neutral_relay.py is a sibling of this session
    manager inside the installed version's runtime/ directory, so the installed
    runtime is checkout-independent (the relay must be found after the original
    checkout is removed). Checkout-era fallback: the canonical repository
    layout. GOVERLOOP_RELAY_PATH always overrides both.
    """
    sibling = os.path.join(os.path.dirname(os.path.abspath(__file__)), "neutral_relay.py")
    if os.path.exists(sibling):
        return sibling
    return os.path.expanduser(
        "~/Documents/02_other_projects/GovernLoop-workspace/repos/GovernLoop/"
        "tools/neutral-relay/neutral_relay.py"
    )


RELAY_DEFAULT = os.path.expanduser(
    os.environ.get("GOVERLOOP_RELAY_PATH", _default_relay_path())
)
CANONICAL_CONFIG = os.path.expanduser("~/.governloop/relay/config.json")

USER_CONVERSATION_SELECTION_REQUIRED = "USER_CONVERSATION_SELECTION_REQUIRED"
CHECKPOINT_TYPES = (
    "NEW_BLOCKER",
    "UNEXPECTED_STATE",
    "BEFORE_DESTRUCTIVE_ACTION",
    "REVIEW_REQUIRED",
    "FINAL_VERIFICATION",
)
# Cap for inline attachment degradation (relay without --attachment support).
INLINE_ATTACHMENT_MAX_CHARS = 200_000

SECRET_RE = re.compile(
    r"github_pat_[A-Za-z0-9_]{10,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|"
    r"ghu_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|Bearer [A-Za-z0-9._-]{20,}"
)
ISSUE_TOKEN_RE = re.compile(r"\b(?:[A-Z]{2,5})-(\d+)\b")   # LEA-91, AGE-53, GL-123
ISSUE_WORD_RE = re.compile(r"\bissue[_-]?(\d+)\b", re.I)    # issue-128 / issue_128
PR_NUM_RE = re.compile(r"#(\d+)")                           # #128

CHATGPT_URL_RE = re.compile(r"^https?://(chatgpt\.com|c\.chatgpt\.com|chat\.openai\.com)/(?:g/[\w-]+/)?c/[\w-]+/?(?:[?#].*)?$")


# --------------------------------------------------------------------------
# git / repo detection
# --------------------------------------------------------------------------
def run_git(cwd, *args):
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=15
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def remote_url_to_slug(url):
    """Convert a git origin URL to owner/repo (https, ssh, or scp-like)."""
    url = (url or "").strip()
    m = re.search(r"(?:github\.com[/:])([^/\s]+)/([^/\s]+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return ""


def detect_repo(cwd=None):
    """Detect the current repository as owner/repo (origin) or dir name fallback."""
    cwd = cwd or os.getcwd()
    url = run_git(cwd, "config", "--get", "remote.origin.url")
    slug = remote_url_to_slug(url) if url else ""
    if slug:
        return slug
    name = os.path.basename(os.path.abspath(cwd))
    return name or None


# --------------------------------------------------------------------------
# task identity detection (priority: env issue id -> branch -> title -> slug)
# --------------------------------------------------------------------------
def normalize_task(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(s).strip()).strip("-")
    return s.upper()[:48] or "task"


def task_from_branch(branch):
    b = (branch or "").replace("origin/", "").split("/")[-1]
    if not b or b in ("HEAD", "main", "master", "develop"):
        return ""
    m = ISSUE_TOKEN_RE.search(b)
    if m:
        return normalize_task(m.group(0))          # LEA-91
    m = ISSUE_WORD_RE.search(b)
    if m:
        return f"ISSUE-{m.group(1)}"
    return normalize_task(b)


def detect_task(cwd=None, title=None, env=None):
    """Resolve the task identity; returns (task, source)."""
    env = env or os.environ
    for key in ("LINEAR_ISSUE_ID", "GITHUB_ISSUE_ID", "ISSUE_ID", "TASK_ID", "GOVERLOOP_TASK"):
        v = (env.get(key) or "").strip()
        if v:
            return normalize_task(v), f"env:{key}"
    cwd = cwd or os.getcwd()
    # symbolic-ref works even on an unborn HEAD (fresh repo with no commits)
    branch = run_git(cwd, "symbolic-ref", "--short", "HEAD") or run_git(
        cwd, "rev-parse", "--abbrev-ref", "HEAD"
    )
    t = task_from_branch(branch)
    if t:
        return t, "branch"
    if title and str(title).strip():
        return normalize_task(title), "title"
    # deterministic generated slug (stable per repo so same-session reuse works)
    import hashlib
    repo = detect_repo(cwd) or "unknown"
    slug = hashlib.sha1(repo.encode()).hexdigest()[:6].upper()
    return f"TASK-{slug}", "slug"


# --------------------------------------------------------------------------
# session identity + state
# --------------------------------------------------------------------------
def session_id_for(project, task, date=None):
    date = date or datetime.date.today().isoformat()
    return f"{normalize_task(project)}-{normalize_task(task)}-{date}"


def state_path(state_dir, session_id):
    return os.path.join(state_dir, f"governloop-session-{session_id}.json")


def load_state(state_dir, session_id):
    p = state_path(state_dir, session_id)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(state_dir, state):
    with open(state_path(state_dir, state["session_id"]), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _scan_state_files(state_dir):
    if not os.path.isdir(state_dir):
        return []
    out = []
    for fn in os.listdir(state_dir):
        if fn.startswith("governloop-session-") and fn.endswith(".json"):
            s = load_state(state_dir, fn[len("governloop-session-"): -len(".json")])
            if s:
                out.append(s)
    return out


def find_session_for_repo(state_dir, repo, task=None):
    """Reuse only when same repo + same task/session + valid temp state exists."""
    states = [s for s in _scan_state_files(state_dir) if s.get("repo") == repo]
    if not states:
        return None
    if len(states) == 1:
        return states[0]
    if task:
        for s in states:
            if normalize_task(s.get("task", "")) == normalize_task(task):
                return s
    # Ambiguous (multiple sessions for the same repo): prefer the most recently
    # updated ACTIVE session. Deterministic and practical for bind/status/end.
    active = [s for s in states if s.get("status") == "ACTIVE"]
    pool = active or states
    return max(pool, key=lambda s: s.get("updated_at", ""))


def new_session(state_dir, cwd=None, env=None, title=None, date=None):
    """Create or resume a session. Returns (state, created, message)."""
    env = env or os.environ
    cwd = cwd or os.getcwd()
    repo = detect_repo(cwd)
    if not repo:
        return None, False, "ERROR: cannot detect repository (no git origin, no dir name)"
    task, source = detect_task(cwd=cwd, title=title, env=env)
    project = repo.split("/")[-1]
    sid = session_id_for(project, task, date=date)
    existing = load_state(state_dir, sid)
    if existing:
        existing["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_state(state_dir, existing)
        return existing, True, f"REUSE session {sid} (repo={repo} task={task} src={source})"
    state = {
        "session_id": sid,
        "repo": repo,
        "project": project,
        "task": task,
        "task_source": source,
        "conversation_url": None,
        "cdp_port": _default_cdp_port(env),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "checkpoints": [],
        "status": "ACTIVE",
    }
    save_state(state_dir, state)
    return state, False, f"NEW session {sid} (repo={repo} task={task} src={source})"


def _default_cdp_port(env=None):
    env = env or os.environ
    if env.get("GOVERLOOP_CDP_PORT"):
        try:
            return int(env["GOVERLOOP_CDP_PORT"])
        except ValueError:
            pass
    try:
        with open(CANONICAL_CONFIG, encoding="utf-8") as f:
            d = json.load(f)
        p = d.get("runtime", {}).get("cdp_port")
        if p:
            return int(p)
    except Exception:
        pass
    return DEFAULT_CDP_PORT


# --------------------------------------------------------------------------
# bind (conversation URL is session/task-level; never canonical config)
# --------------------------------------------------------------------------
def bind_url(state_dir, session_id, url, cdp_port=None):
    state = load_state(state_dir, session_id)
    if not state:
        return None, f"ERROR: no session {session_id} (run `new` first)"
    url = url.strip()
    if not CHATGPT_URL_RE.fullmatch(url):
        return None, (
            "ERROR: not a valid ChatGPT conversation URL "
            "(expected https://chatgpt.com/c/<id> or "
            "https://chatgpt.com/g/<project-or-gpt-id>/c/<id>)"
        )
    state["conversation_url"] = url
    if cdp_port:
        state["cdp_port"] = int(cdp_port)
    state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(state_dir, state)
    return state, f"BOUND session {session_id} (temp state only; canonical config untouched)"


def cdp_target_open(url, cdp_port, timeout=5):
    """Best-effort CDP check: is the conversation page open? Non-fatal."""
    conv = re.search(r"/c/([\w-]+)", url)
    if not conv:
        return None
    conv_id = conv.group(1)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{cdp_port}/json/list", timeout=timeout
        ) as r:
            pages = json.loads(r.read().decode())
        return any(conv_id in (p.get("url") or "") for p in pages)
    except Exception:
        return None


# --------------------------------------------------------------------------
# checkpoint reporting
# --------------------------------------------------------------------------
def scan_secret(path, limit=2_000_000):
    """Return number of secret-pattern hits in a text file (0 for binary)."""
    try:
        if os.path.getsize(path) > limit:
            return 0
        with open(path, encoding="utf-8", errors="ignore") as f:
            return len(SECRET_RE.findall(f.read()))
    except Exception:
        return 0


def build_request(state, ctype, message, seq):
    head = (
        f"REVIEW_REQUEST_ID: {state['session_id']}-{ctype}-{seq}\n"
        f"REPO: {state['repo']}\n"
        f"CHECKPOINT: {ctype}\n"
        f"SESSION: {state['session_id']}\n\n"
    )
    body = message if (message or "").strip() else f"Checkpoint {ctype} for session {state['session_id']}."
    return head + body.strip()


def relay_supports_attachment(relay):
    """Probe whether the neutral relay accepts --attachment args.

    Relay versions differ: older builds reject --attachment (argparse exit 2,
    CHECKPOINT_DELIVERY_INCOMPLETE). Fail open on probe errors so callers
    degrade to inline delivery.
    """
    try:
        r = subprocess.run(
            [sys.executable, relay, "--help"],
            capture_output=True, text=True, timeout=30,
        )
        return "--attachment" in (r.stdout or "") + (r.stderr or "")
    except Exception:
        return False


def run_checkpoint(
    state_dir,
    cwd=None,
    env=None,
    ctype=None,
    message=None,
    message_file=None,
    attach=None,
    relay_path=None,
    cdp_port=None,
    state=None,
):
    """Report a review checkpoint. Returns (ok, text, exit_code)."""
    env = env or os.environ
    cwd = cwd or os.getcwd()
    if ctype not in CHECKPOINT_TYPES:
        return False, f"ERROR: unknown checkpoint type {ctype!r} (allowed: {', '.join(CHECKPOINT_TYPES)})", 1
    if state is None:
        repo = detect_repo(cwd)
        state = find_session_for_repo(state_dir, repo)
        if state is None:
            return False, (
                f"{USER_CONVERSATION_SELECTION_REQUIRED}: no active session for {repo}; "
                "run `/governloop` first, then `/governloop bind <url>`"
            ), 3
    if not state.get("conversation_url"):
        return False, (
            f"{USER_CONVERSATION_SELECTION_REQUIRED}: session {state['session_id']} has no "
            "ChatGPT conversation URL; ask the user once and run `/governloop bind <url>`"
        ), 3

    relay = relay_path or env.get("GOVERLOOP_RELAY_PATH") or RELAY_DEFAULT
    if not os.path.exists(relay):
        return False, f"ERROR: relay not found at {relay} (set GOVERLOOP_RELAY_PATH)", 1

    # evidence attachments: safety scan first (contract: exists/relevance/secret/sha256)
    attach = [a for a in (attach or []) if a]
    refused = []
    for ap in attach:
        if not os.path.exists(ap):
            refused.append(f"{ap} (missing)")
        elif scan_secret(ap) > 0:
            refused.append(f"{ap} (secret pattern found; attach a .redacted copy only)")
    if refused:
        return False, f"CHECKPOINT_DELIVERY_INCOMPLETE: refused attachments: {', '.join(refused)}", 1

    if message_file:
        try:
            with open(message_file, encoding="utf-8") as f:
                message = f.read()
        except Exception as e:
            return False, f"ERROR: cannot read message file {message_file}: {e}", 1

    # relay capability probe: relay versions differ on --attachment support.
    # Without support, degrade to inlining the attachment text into the message
    # body so the conversation still receives the full content (honest
    # degradation, never a false "attachments delivered").
    inline_degraded = False
    inline_count = 0
    if attach and not relay_supports_attachment(relay):
        inline_parts = []
        for ap in attach:
            try:
                with open(ap, encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception as e:
                refused.append(f"{ap} (unreadable for inline: {e})")
                continue
            if len(content) > INLINE_ATTACHMENT_MAX_CHARS:
                refused.append(
                    f"{ap} ({len(content)} chars exceeds inline limit "
                    f"{INLINE_ATTACHMENT_MAX_CHARS}; attach a trimmed copy)"
                )
                continue
            inline_parts.append(
                f"\n\n===== [ATTACHMENT INLINE: {ap} ({len(content)} chars)] =====\n"
                f"{content}\n===== END ATTACHMENT ====="
            )
        if refused:
            return False, f"CHECKPOINT_DELIVERY_INCOMPLETE: refused attachments: {', '.join(refused)}", 1
        message = (message or "").rstrip() + "".join(inline_parts)
        inline_count = len(inline_parts)
        attach = []
        inline_degraded = bool(inline_count)

    seq = len(state.get("checkpoints", [])) + 1
    request_text = build_request(state, ctype, message, seq)
    req_path = os.path.join(state_dir, f"governloop-request-{state['session_id']}-{ctype}-{seq}.txt")
    out_path = os.path.join(state_dir, f"governloop-response-{state['session_id']}-{ctype}-{seq}.md")
    cfg_path = os.path.join(state_dir, f"governloop-config-{state['session_id']}-{ctype}-{seq}.json")
    with open(req_path, "w", encoding="utf-8") as f:
        f.write(request_text)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "routes": {
                    state["repo"]: {
                        "conversation_url": state["conversation_url"],
                        "cdp_port": cdp_port or state.get("cdp_port") or _default_cdp_port(env),
                    }
                }
            },
            f,
            indent=2,
        )

    cmd = [
        sys.executable, relay,
        "--request-file", req_path,
        "--output-file", out_path,
        "--config-file", cfg_path,
    ]
    for ap in attach:
        cmd += ["--attachment", ap]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "CHECKPOINT_DELIVERY_INCOMPLETE: relay timed out", 1

    if r.returncode != 0:
        return False, f"CHECKPOINT_DELIVERY_INCOMPLETE: relay exit {r.returncode}: {r.stdout.strip()[:400]}", 1

    response = ""
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            response = f.read()

    state.setdefault("checkpoints", []).append(
        {"type": ctype, "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "request": req_path, "response": out_path}
    )
    state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(state_dir, state)

    attrs_note = f"ATTACHMENTS: {len(attach)} delivered"
    if inline_degraded:
        attrs_note = (
            f"ATTACHMENTS: 0 delivered ({inline_count} inlined into message text "
            f"-- relay has no --attachment support)"
        )
    # Full readback is part of the stable CLI boundary. The relay keeps the
    # canonical response file as internal evidence, but agents must not need to
    # open that temp file (which may cross their sandbox boundary) just to read
    # the assistant reply. Emit the complete response on stdout instead.
    summary = (
        f"CHECKPOINT: {ctype}\nSESSION: {state['session_id']}\n"
        f"TEXT_RELAY: PASS\n{attrs_note}\n"
        "RESPONSE_BEGIN\n"
        f"{response}"
        "\nRESPONSE_END"
    )
    return True, summary, 0


def end_session(state_dir, session_id, send_final=False, attach=None, env=None, relay_path=None):
    state = load_state(state_dir, session_id)
    if not state:
        return False, f"ERROR: no session {session_id}"
    if send_final and state.get("conversation_url"):
        ok, text, code = run_checkpoint(
            state_dir, ctype="FINAL_VERIFICATION", attach=attach,
            relay_path=relay_path, env=env, state=state,
        )
        if not ok:
            return False, text, code
    os.remove(state_path(state_dir, session_id))
    return True, f"ENDED session {session_id}; temp routing state removed (canonical config untouched)", 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------
def status_text(state, state_dir):
    bound = bool(state.get("conversation_url"))
    cps = state.get("checkpoints", [])
    last = cps[-1]["type"] if cps else "none"
    return (
        f"repo:            {state.get('repo')}\n"
        f"task:            {state.get('task')} (src: {state.get('task_source')})\n"
        f"session id:      {state.get('session_id')}\n"
        f"conversation:    {'bound: yes' if bound else 'bound: no'}\n"
        f"last checkpoint: {last} ({len(cps)} reported)\n"
        f"temp state path: {state_path(state_dir, state['session_id'])}"
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    state_dir = os.environ.get("GOVERLOOP_STATE_DIR", STATE_DIR_DEFAULT)

    p = argparse.ArgumentParser(prog="governloop", description="GovernLoop session manager + checkpoint reporter")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new", help="create/resume a session (auto repo/task/session-id)")
    p_new.add_argument("--title", default=None, help="explicit task title (lowest priority after issue-id and branch)")
    p_new.add_argument("--session", dest="sid", default=None, help="explicit session id (normally not required)")

    p_status = sub.add_parser("status", help="show current session state")

    p_bind = sub.add_parser("bind", help="bind a ChatGPT conversation URL (temp state only)")
    p_bind.add_argument("url")
    p_bind.add_argument("--cdp-port", type=int, default=None)
    p_bind.add_argument("--session", dest="sid", default=None)

    p_chk = sub.add_parser("checkpoint", help="report a review checkpoint")
    p_chk.add_argument("type", choices=CHECKPOINT_TYPES)
    p_chk.add_argument("--message", default=None)
    p_chk.add_argument("--message-file", default=None)
    p_chk.add_argument("--attach", action="append", default=[])
    p_chk.add_argument("--session", dest="sid", default=None)

    p_end = sub.add_parser("end", help="(optionally send FINAL_VERIFICATION and) remove temp state")
    p_end.add_argument("--final", action="store_true", help="send FINAL_VERIFICATION before ending (only if bound)")
    p_end.add_argument("--attach", action="append", default=[])
    p_end.add_argument("--session", dest="sid", default=None)

    sub.add_parser("doctor", help="read-only diagnostics (Phase 2C: stable CLI + doctor)")

    args = p.parse_args(argv)
    cwd = os.getcwd()

    # doctor is strictly read-only: it must run BEFORE any state_dir creation
    # or session-state mutation (e.g. GOVERLOOP_STATE_DIR must not be created
    # by a doctor invocation).
    if args.cmd == "doctor":
        return _doctor_main()

    os.makedirs(state_dir, exist_ok=True)

    if args.cmd == "new":
        state, created, msg = new_session(state_dir, cwd=cwd, title=getattr(args, "title", None))
        if state is None:
            print(msg); return 1
        print(msg)
        if not state.get("conversation_url"):
            print(USER_CONVERSATION_SELECTION_REQUIRED)
            print("Ask the user once for the ChatGPT conversation URL, then run: /governloop bind <url>")
            return 3
        print(status_text(state, state_dir))
        return 0

    if args.cmd == "status":
        repo = detect_repo(cwd)
        state = find_session_for_repo(state_dir, repo)
        if not state:
            print(f"no active session for {repo}")
            return 0
        print(status_text(state, state_dir))
        return 0

    if args.cmd == "bind":
        sid = getattr(args, "sid", None) or _require_session(state_dir, cwd)
        if not sid:
            print(USER_CONVERSATION_SELECTION_REQUIRED); return 3
        state, msg = bind_url(state_dir, sid, args.url, args.cdp_port)
        print(msg)
        if state is None:
            return 1
        open_state = cdp_target_open(state["conversation_url"], state.get("cdp_port"))
        if open_state is None:
            print("CDP check skipped/unreachable (verify the conversation is open before the first checkpoint)")
        else:
            print("CDP target open:", "YES" if open_state else "NO")
        return 0

    if args.cmd == "checkpoint":
        sid = getattr(args, "sid", None)
        state = None
        if sid:
            state = load_state(state_dir, sid)
            if not state:
                print(f"ERROR: no session {sid}"); return 1
        ok, text, code = run_checkpoint(
            state_dir, cwd=cwd, ctype=args.type, message=args.message,
            message_file=args.message_file, attach=args.attach, state=state,
        )
        print(text)
        return code

    if args.cmd == "end":
        sid = getattr(args, "sid", None) or _require_session(state_dir, cwd)
        if not sid:
            print(USER_CONVERSATION_SELECTION_REQUIRED); return 3
        ok, text, code = end_session(state_dir, sid, send_final=args.final, attach=args.attach)
        print(text)
        return code if not ok else 0

    return 0


# --------------------------------------------------------------------------
# doctor (read-only diagnostics — Phase 2C)
# --------------------------------------------------------------------------
# All paths resolved relative to the runtime that is currently active (i.e.
# the directory containing THIS file after installation).  The caller\'s cwd
# is NEVER changed and no filesystem mutation occurs.

def is_valid_current_target(target):
    """Strict installer contract for the `current` pointer.

    Accepts exactly ``versions/<single-install-id>``: one non-empty path
    component with no ``.``, ``..``, nested path, or traversal. Any other
    shape — ``versions/``, ``versions/.``, ``versions/..``,
    ``versions/../x``, ``versions/a/b``, or a non-``versions/*`` target — is
    rejected, mirroring ``scripts/install.sh``. Dangling targets are a
    separate check (shape first, then existence).
    """
    if not isinstance(target, str) or not target.startswith("versions/"):
        return False
    rest = target[len("versions/"):]
    return bool(rest) and rest not in (".", "..") and "/" not in rest


def _doctor_main():
    results = doctor_report()
    has_fail = False
    has_warn = False
    for level, label, detail in results:
        if level == "FAIL":
            has_fail = True
        elif level == "WARN":
            has_warn = True
        print(f"[{level}] {label}: {detail}")
    print()
    if has_fail:
        print("RESULT: FAIL — one or more checks failed; GovernLoop may not function correctly")
        return 1
    elif has_warn:
        print("RESULT: WARN — one or more checks have warnings; GovernLoop should work but check above")
        return 0
    else:
        print("RESULT: PASS — all checks passed")
        return 0


def _rel_path(path, base):
    """Return path relative to base, or the original if outside base."""
    try:
        return os.path.relpath(os.path.realpath(path), os.path.realpath(base))
    except ValueError:
        return path


def _check_blocking(state_dir=None):
    """Return (level, message) for the most severe blocking condition."""
    home = os.environ.get("GOVERLOOP_HOME", os.path.join(os.path.expanduser("~"), ".governloop"))
    current = os.path.join(home, "current")
    if not os.path.exists(current):
        return "FAIL", f"No active version: {current} does not exist (run the installer first)"
    if os.path.islink(current):
        target = os.readlink(current)
        if not is_valid_current_target(target):
            return "FAIL", f"current symlink target is malformed or uses traversal: {target}"
        if not os.path.exists(current):
            return "FAIL", f"current symlink is dangling: {target}"
    else:
        return "FAIL", f"current exists but is not a symlink (type: {os.path.lstat(current).st_mode & 0o170000:#o:o})",
    return None, None


def doctor_report(goverloop_home=None, state_dir=None):
    """Run read-only diagnostics. Returns list of (result, label, detail) tuples.

    Result is one of: PASS, WARN, FAIL
    This function is idempotent and performs NO filesystem mutation.
    """
    home = goverloop_home or os.environ.get("GOVERLOOP_HOME",
                                            os.path.join(os.path.expanduser("~"), ".governloop"))
    state_dir = state_dir or os.environ.get("GOVERLOOP_STATE_DIR", STATE_DIR_DEFAULT)
    # Resolve this runtime\'s own directory for comparison (checkout-independent).
    runtime_dir = os.path.dirname(os.path.abspath(__file__))
    results = []

    # 1. GovernLoop home path
    if os.path.isdir(home):
        results.append(("PASS", "GovernLoop home", f"{home} (exists)"))
    else:
        results.append(("FAIL", "GovernLoop home", f"{home} (not found — run install.sh)"))
        return results  # nothing else can be checked

    # 2. Stable CLI present (the dispatcher at GOVERLOOP_HOME/bin/governloop)
    dispatcher = os.path.join(home, "bin", "governloop")
    if os.path.isfile(dispatcher) and os.access(dispatcher, os.X_OK):
        results.append(("PASS", "Stable CLI", f"{dispatcher} (executable)"))
    elif os.path.isfile(dispatcher):
        results.append(("WARN", "Stable CLI", f"{dispatcher} (exists but not executable)"))
    else:
        results.append(("FAIL", "Stable CLI", f"{dispatcher} (not found)"))

    # 3. current pointer present and valid
    current = os.path.join(home, "current")
    if os.path.islink(current):
        target = os.readlink(current)
        if not is_valid_current_target(target):
            results.append(("FAIL", "current pointer",
                            f"malformed target (expected versions/<single-install-id>): {target}"))
        elif not os.path.exists(current):
            results.append(("FAIL", "current pointer", f"dangling symlink: {target}"))
        else:
            results.append(("PASS", "current pointer", f"{target} (valid)"))
    elif os.path.exists(current):
        results.append(("FAIL", "current pointer", f"exists but is not a symlink"))
    else:
        results.append(("FAIL", "current pointer", f"{current} (absent)"))

    # 4. Active INSTALL_ID / version directory
    if os.path.islink(current) and os.path.exists(current):
        version_dir = os.path.join(home, os.readlink(current))
        install_id = os.path.basename(version_dir)
        if os.path.isdir(version_dir):
            results.append(("PASS", "Active version", f"{install_id} at {version_dir}"))
            # Verify it\'s the same directory this runtime resolves to.
            runtime_marker = os.path.join(runtime_dir, "governloop_session.py")
            if os.path.realpath(runtime_marker) == os.path.realpath(os.path.join(version_dir, "runtime", "governloop_session.py")):
                results.append(("PASS", "Runtime matches current", "active version is the running runtime"))
            else:
                results.append(("WARN", "Runtime matches current",
                                f"running runtime is {runtime_dir} but current -> {version_dir}"))
        else:
            results.append(("FAIL", "Active version", f"current target is not a directory: {version_dir}"))
    else:
        results.append(("FAIL", "Active version", "cannot check: current pointer unavailable"))

    # 5. Installed runtime files present
    for fname in ("governloop_session.py", "neutral_relay.py"):
        fp = os.path.join(runtime_dir, fname)
        if os.path.isfile(fp) and os.path.getsize(fp) > 0:
            results.append(("PASS", f"Runtime file: {fname}", f"{_rel_path(fp, runtime_dir)} ({os.path.getsize(fp)} bytes)"))
        elif os.path.isfile(fp):
            results.append(("FAIL", f"Runtime file: {fname}", f"{_rel_path(fp, runtime_dir)} (empty)"))
        else:
            results.append(("FAIL", f"Runtime file: {fname}", f"{_rel_path(fp, runtime_dir)} (missing)"))

    # 6. Installed universal skill present
    skill_path = os.path.join(runtime_dir, "..", "skills", "governloop", "SKILL.md")
    if os.path.isfile(skill_path) and os.path.getsize(skill_path) > 0:
        results.append(("PASS", "Installed skill", f"{_rel_path(skill_path, runtime_dir)}"))
    else:
        results.append(("WARN", "Installed skill", f"{_rel_path(skill_path, runtime_dir)} (missing or empty)"))

    # 7. Normative contracts present
    contracts_dir = os.path.join(runtime_dir, "..", "skills", "governloop", "contracts")
    expected_contracts = [
        "neutral-relay-checkpoint-delivery.md",
        "AGENT_SAFETY_CONTRACT.md",
        "policy.md",
    ]
    for cname in expected_contracts:
        cp = os.path.join(contracts_dir, cname)
        if os.path.isfile(cp):
            results.append(("PASS", f"Contract: {cname}", "present"))
        else:
            results.append(("WARN", f"Contract: {cname}", "missing"))

    # 8. Canonical relay config path
    relay_config = os.path.join(home, "relay", "config.json")
    if os.path.isfile(relay_config):
        results.append(("PASS", "Relay config", f"{relay_config}"))
    else:
        results.append(("WARN", "Relay config", f"{relay_config} (optional; relay works without it)"))

    # 9. Configured CDP port
    cdp_port = int(os.environ.get("GOVERLOOP_CDP_PORT", "0"))
    if cdp_port > 0:
        results.append(("PASS", "CDP port (env)", f"GOVERLOOP_CDP_PORT={cdp_port}"))
    elif os.path.isfile(relay_config):
        try:
            with open(relay_config, encoding="utf-8") as f:
                cfg = json.load(f)
            p = cfg.get("runtime", {}).get("cdp_port")
            if p:
                results.append(("PASS", "CDP port (config)", f"{relay_config} → cdp_port={p}"))
            else:
                results.append(("WARN", "CDP port (config)", "runtime.cdp_port not set in relay config"))
        except Exception as e:
            results.append(("WARN", "CDP port (config)", f"could not read {relay_config}: {e}"))
    else:
        results.append(("WARN", "CDP port", f"not configured (env or {relay_config}); using default {DEFAULT_CDP_PORT}"))

    # 10. Neutral Relay path resolution
    relay_resolved = RELAY_DEFAULT
    if os.path.isfile(relay_resolved):
        results.append(("PASS", "Neutral Relay", f"{_rel_path(relay_resolved, runtime_dir)} ({os.path.getsize(relay_resolved)} bytes)"))
    else:
        results.append(("FAIL", "Neutral Relay", f"not found at {relay_resolved}"))

    # 11. Python 3 availability
    py = sys.executable or "python3"
    try:
        r = subprocess.run([py, "--version"], capture_output=True, text=True, timeout=10)
        py_ver = r.stdout.strip() or r.stderr.strip() or "unknown"
        if py_ver.startswith("Python 3"):
            results.append(("PASS", "Python 3", f"{py}: {py_ver}"))
        else:
            results.append(("WARN", "Python 3", f"{py}: {py_ver} (may not be Python 3)"))
    except Exception as e:
        results.append(("FAIL", "Python 3", f"{py} not available: {e}"))

    # 12. Installed runtime checkout independence sanity
    # This file must resolve inside a version/ directory, not a source checkout.
    # If the running binary is inside a source checkout, this is a checkout-dependent
    # invocation (fine for development, but the installed runtime is preferred).
    in_checkout = "GovernLoop" in runtime_dir and "versions" not in runtime_dir
    in_version_dir = "versions" in runtime_dir
    if in_version_dir:
        results.append(("PASS", "Runtime isolation", "running from installed version directory (checkout-independent)"))
    elif in_checkout:
        results.append(("WARN", "Runtime isolation",
                        f"running from source checkout: {runtime_dir} (use installed runtime for production)"))
    else:
        results.append(("WARN", "Runtime isolation", f"runtime location unclear: {runtime_dir}"))

    # 13. Whether current working directory looks like a target repository
    cwd = os.getcwd()
    git_toplevel = ""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            git_toplevel = r.stdout.strip()
    except Exception:
        pass
    if git_toplevel:
        results.append(("PASS", "Target repo detection", f"cwd={cwd} (git toplevel={git_toplevel})",
                        ))
        if cwd != git_toplevel:
            results.append(("WARN", "Target repo cwd",
                            f"cwd ({cwd}) != git toplevel ({git_toplevel}); session created for git toplevel"))
    else:
        results.append(("WARN", "Target repo detection",
                        f"cwd={cwd} is not inside a git repository; sessions will use directory name as repo identifier"))

    # 14. Any blocking issue
    block_level, block_msg = _check_blocking(state_dir)
    if block_level == "FAIL":
        results.append(("FAIL", "Blocking issue", block_msg))
    elif block_level == "WARN":
        results.append(("WARN", "Blocking issue", block_msg))
    else:
        results.append(("PASS", "Blocking issue", "no blocking conditions detected"))

    return results


def _require_session(state_dir, cwd):
    repo = detect_repo(cwd)
    state = find_session_for_repo(state_dir, repo)
    return state["session_id"] if state else None


if __name__ == "__main__":
    sys.exit(main())
