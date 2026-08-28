"""agent/gateway.py — YOUR control plane. CONTRACTS.md section 4, exactly.

READ agent/README.md FIRST — it maps all five files in this directory to what
each is scored on. This file is the one CONTRACTS.md calls "the trusted
envelope's untrusted half": every single MCP / A2A / DISCOVER command your
agent's model wants to make passes through `Gateway.decide` before it is
allowed to happen.

WHY THERE IS NO `execute()` METHOD ON `GatewayContext` (read this before you
go looking for one — there isn't one, and that is not an oversight)
----------------------------------------------------------------------------
CONTRACTS.md section 4's trusted envelope, reproduced here because it is the
one diagram worth memorising:

    [ trusted ]   loop emits a raw action line
         v
    [ trusted ]   INTERCEPT + CANONICALISE -> Command        (kit/loop/agent.py)
         v
    [ UNTRUSTED ] Gateway.decide(cmd) -> Decision             <- THIS FILE
         v
    [ trusted ]   ENFORCE: honour the Decision, meter it,
                  apply the active mutation, execute the
                  ToolCall or refuse it                       (the arena)
         v
    [ trusted ]   RECORD the authoritative L1 event, then
                  RENDER the Observation                      (the arena)
         v
    [ trusted ]   the model sees the Observation

`decide()` returns a *decision*, never a *result*. You cannot reach a tool
server, a file, a socket, or a clock from in here — there is nothing to
call. Two things follow from that, and both matter more than they look:

  1. YOUR TRACE CANNOT BE FORGED. Every `command` / `decision` / `enforced`
     / `tool_call` / `tool_result` L1 event (CONTRACTS.md 5.2) is written by
     the arena, from what the arena itself actually did — never from
     anything you claimed happened. A student gateway that wanted to lie
     about having blocked an attack ("I totally denied that, trust me")
     simply has no channel to lie through: the only thing you ever hand
     back is this one small `Decision` value, and the arena is the one that
     turns it into history.
  2. NOBODY CAN ACCUSE YOU OF A CALL YOU DID NOT AUTHORISE, either. Because
     `decide()` is the ONLY door a command can walk through on its way to
     actually running, a prosecutor's `enforcement_failure` claim against
     you has exactly one thing to point at: the `Decision` you returned for
     that specific `cmd_id`. There is no ambiguity about "maybe the loop
     called the tool directly" — CONTRACTS.md 4.2 removed that path on
     purpose, and kit/loop/agent.py's own module docstring names the same
     invariant from the other side (the loop never imports this module,
     never sees a `Decision`, never executes anything itself).

The cost of that guarantee is that this file is PURE: synchronous, no I/O,
no threads, no `sleep`, 250 ms wall-clock deadline (RULES.md section 3).
Raising anything, returning something that is not a valid `Decision`, or
missing the deadline is treated by the arena as a DENIED command PLUS a 2
credit penalty PLUS an `integrity` event that hands the prosecutor a free
`enforcement_failure` — CONTRACTS.md 4.1's charging table, reproduced in
agent/README.md's own table. Getting this file to just plainly return valid
`Decision` values, every time, is worth more than getting it clever.

THE STARTER'S SHAPE (read this before you start editing `decide()`)
----------------------------------------------------------------------------
This starter FORWARDS ALMOST EVERYTHING AND DENIES NOTHING. That is not a
placeholder oversight — it is the honest zero-defence baseline you are
meant to beat: `bots/rookie` in the kit's own ladder does exactly the same
thing, and RULES.md's own words are "if you cannot beat Rookie you have a
bug, not a strategy." `decide()` below is structured as four named jobs —
ROUTE, ADMIT, AUTHORIZE, BUDGET — each with a one-line TODO naming what a
real implementation checks and why. None of the four currently rejects,
rewrites, or reroutes anything; they are seams, not solutions. Fill them in
using `agent/strategy.py` (routing/budget policy) and `agent/guardrails.py`
(the safety checks) — both already import cleanly from here.

ONE THING WORTH INTERNALISING BEFORE YOU WRITE YOUR FIRST REAL CHECK:
`verdict="deny"` costs the CALLER (your own team) **zero credits** —
CONTRACTS.md 4.1's charging table has exactly one $0 row, and it is this
one. Refusing to make a call you cannot justify is FREE. That makes
abstention a real strategy, not a luxury you can't afford: a `deny` you can
defend beats a `forward` you can't, every time a prosecutor is watching.

Stdlib only. No network, no randomness, no wall-clock reads, no sleeping —
none of that would even survive the kernel sandbox (CONTRACTS.md 12), but
the point is this file has no reason to want any of it in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry
# agent/strategy.py is OUR OWN policy toolkit (RULES.md section 1: we own this
# directory). It is imported, not inlined, so the cost arithmetic that decides a
# mask lives in exactly one place and this file stays a control plane rather
# than a second copy of the price table.
from agent.strategy import (
    BudgetPacer,
    ResultCache,
    is_catalog_trap,
    pick_replica,
    successor_of,
)
from agent.guardrails import scan_for_injected_instructions

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "A2A_SERVERS",
    "WRITE_TOOLS",
    "CITE_MASKS",
    "ROUND_ALLOWANCE",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})

# The A2A peers the registry can vouch for. A command aimed at one of these is a
# DELEGATION and carries an audience; a command aimed at anything else is a plain
# MCP call and carries none. Kept as data so JOB 3 can ask "is this a delegation"
# without pattern-matching on the server name in three separate places.
A2A_SERVERS: frozenset[str] = frozenset({"curriculum-analyst", "citation-checker", "roster"})

# Every tool that MUTATES the world. These are the only calls that need an
# `If-Match` precondition and an `Idempotency-Key` (CONTRACTS.md 3.x mechanics 3
# and the exactly-once rule) — and the only ones where a target learner id is an
# authority question rather than a read filter.
WRITE_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {("content", "flag_stale_slide"), ("content", "file_content_bug"), ("progress", "record_mastery")}
)

# The narrow default mask per tool: exactly the fields an answer is plausibly
# going to CITE, never the tool's own full dump. JOB 4 substitutes one of these
# whenever the model asked for everything or asked for nothing (both of which
# mean "pay the ceiling price" — agent/strategy.py's module docstring has the
# arithmetic: 11 credits a round survives ten rounds, 49 is bankrupt by three).
CITE_MASKS: dict[tuple[str, str], tuple[str, ...]] = {
    ("slides", "query"): ("title", "anchor"),
    ("slides", "get_frame"): ("title", "body", "anchor"),
    ("slides", "whatlinkshere"): ("anchor",),
    ("slides", "search"): ("title", "anchor"),
    ("glossary", "define"): ("definition", "sense", "anchor"),
    ("glossary", "list_terms"): ("term",),
    ("registry", "provenance"): ("etag", "replica", "anchor"),
    ("registry", "list_servers"): ("name",),
    ("research", "cite_source"): ("url", "anchor"),
    ("progress", "record_mastery"): ("receipt_id",),
    ("content", "flag_stale_slide"): ("receipt_id",),
    ("content", "file_content_bug"): ("receipt_id",),
    ("curriculum-analyst", "which_days_cover"): ("course_day", "track", "anchor"),
    ("citation-checker", "verify_source"): ("verdict", "anchor"),
    ("roster", "lookup"): ("learner", "anchor"),
}

# Per-round credit allowance. NOT flat: CONTRACTS.md's round scale makes damage
# in rounds 8-10 worth 1.5x what it is worth in rounds 1-3, so a credit held back
# for round 9 buys more protection than one spent in round 2. Sums to 95 of the
# 100-credit pool, leaving a 5 credit floor for a round that genuinely needs one
# extra corroborating read.
ROUND_ALLOWANCE: dict[int, int] = {1: 8, 2: 8, 3: 8, 4: 9, 5: 9, 6: 9, 7: 10, 8: 11, 9: 11, 10: 12}

# Argument keys that name WHOSE data a call is about. `learner`/`learner_id`/
# `target`/`subject` are authority-bearing on a write; `for_learner` is the read
# filter form of the same thing.
_TARGET_KEYS = ("learner", "learner_id", "target", "subject", "for_learner")

# Argument keys through which a route can be smuggled in the BODY instead of the
# header. Reading a route out of the body is precisely the `header_spoof` /
# `replica_flip` failure — the fix is to refuse, not to quietly honour it.
_BODY_ROUTE_KEYS = ("route", "_route", "replica", "mcp-replica")


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; if you need memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """The control plane. One instance per duel (CONTRACTS.md 4.3) — built
    once at duel start with a `GatewayContext`, then asked to `decide()` on
    every MCP/A2A/DISCOVER command either side of the duel makes for all 10
    rounds. See the module docstring for the trusted-envelope diagram and
    why there is no `execute()` to call instead.

    Instance attributes below are this starter's ENTIRE per-duel memory —
    all currently unused by `decide()`'s naive body, but declared here
    (rather than invented ad hoc later) so the four TODO jobs below have
    somewhere obvious to keep state once you implement them. `agent/
    strategy.py` has working building blocks for exactly this (a budget
    pacer, a result cache, a replica-choice heuristic) — this starter does
    not wire them in for you; that wiring is the assignment.
    """

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)

        # --- per-duel memory, unused by the naive starter below ---------
        # A cache of anchor -> body-ish data you have already paid for this
        # duel (agent/strategy.py's ResultCache is a ready-made version of
        # this). Populating it needs the *result* of a call, which decide()
        # never sees (it only sees the outgoing Command) — you would fill
        # this from whatever the arena hands back to your agent loop AFTER
        # a call executes, then consult it here on the NEXT decide() call
        # for the same anchor.
        self._seen_anchors: dict[str, Any] = {}
        # Credits you have personally authorised so far this duel — your
        # own running total, independent of (and a cross-check against)
        # `ctx.credits`, which the arena maintains authoritatively.
        self._credits_authorised: int = 0
        # Command ids you have already denied, in case a later job wants to
        # know "have I already said no to this once".
        self._denied_cmd_ids: set[str] = set()

        # --- state the four jobs below actually read -------------------
        # Agent Cards the registry has vouched for, fed by `note_card` from
        # outside `decide()` (the arena/loop calls it after a DISCOVER; it is
        # never something this file can go and fetch, which is the whole point
        # of the trusted envelope). `server -> {"verified": bool, "skills": [...]}`.
        self._admitted_cards: dict[str, dict] = {}
        # `anchor -> etag`, fed by `note_provenance` after a `registry.provenance`
        # read. A write with no entry here has no `If-Match` to send and is a
        # `write_violation` waiting to happen, so JOB 2 refuses it instead.
        self._etags: dict[str, str] = {}
        # Idempotency keys already spent this duel. Re-using one is the
        # exactly-once failure; minting one per (anchor, tool) makes the second
        # attempt at the SAME write detectable here rather than at the server.
        self._idempotency: set[str] = set()
        # `path_id`s a `registry.provenance` read has shown to differ across
        # replicas. Fed by `note_drift`; consulted by JOB 1. Empty means "no
        # measured reason to distrust the working copy", not "no drift exists".
        self._drifting_paths: set[str] = set()
        # (server, tool, args-signature) of calls that already failed with a code
        # that is not retry-safe unmodified. Re-sending one unchanged is the
        # `wasteful` retry, and JOB 2 denies it for free.
        self._failed_signatures: set[tuple[str, str, str]] = set()
        # Our own budget bookkeeping. `ctx.credits` stays authoritative; this is
        # the cross-check and the per-round pacer.
        self._pacer = BudgetPacer()
        self._cache = ResultCache()
        self._round_seen: int = 0
        self._spent_this_round: int = 0
        # Counters the duel report reads; never scored, never shown to the peer.
        self.denied: int = 0
        self.quarantined: int = 0
        self.rewritten: int = 0

    def decide(self, cmd: Command) -> Decision:
        """SYNCHRONOUS. PURE. NO I/O. 250 ms wall (RULES.md section 3).
        Raising anything, or returning a `Decision` `__post_init__` rejects,
        is treated by the arena exactly like an explicit deny PLUS a 2
        credit penalty PLUS a scored `integrity` event (CONTRACTS.md 4.1's
        charging table) — so the one thing this method must never do is
        blow up or wander off into I/O, no matter how tempting a "quick
        check" against something external looks. Everything you need to
        decide is already sitting in `cmd` and `self.ctx`.

        The four jobs below are named, ordered, and each one now actually
        decides something. The whole body runs inside one `try` because
        CONTRACTS.md 4.1 charges a RAISE exactly like a malformed Decision —
        2 credits, the command denied, and a free `enforcement_failure`
        handed to the prosecutor. A deny we chose costs 0; a deny we crashed
        into costs 2 and an accusation, so the fallback below converts every
        unexpected exception into the cheap version of the same outcome."""
        self._telemetry.decision_seen(cmd)
        try:
            return self._decide(cmd)
        except Exception as exc:  # noqa: BLE001 - deliberate: never raise out of decide()
            # Not a swallowed bug: it is recorded on our own telemetry (which is
            # ours alone and never scored) AND it still denies, so the command
            # does not execute on a decision we could not finish making.
            self._telemetry.note("decide_fallback_deny", error=f"{type(exc).__name__}: {exc}")
            return self.deny(cmd, f"gateway could not complete a decision ({type(exc).__name__})")

    def _decide(self, cmd: Command) -> Decision:
        """The real body. Separated from `decide` only so the guard above can
        be a single unconditional wrapper rather than a `try` nested around
        four jobs that each want to `return`."""
        self._roll_round()

        # ------------------------------------------------------------------
        # JOB 1 — ROUTE: is this the right SERVER/REPLICA for this command?
        # day18-style drift is real and measured (CORPUS-FACTS.md section 2), and
        # a `swap_replica` mutation can point `cmd` at a stale replica without the
        # model ever noticing. Two rules, in this order:
        #
        #   a) the route is decided by the HEADER, never by the body. A route in
        #      `args` is not a routing preference to honour, it is the attack —
        #      handled in JOB 2, which denies it. Here we only ever WRITE the
        #      header ourselves, so a body route can never influence the answer
        #      even if JOB 2's list of body keys ever misses one.
        #   b) a deprecated tool is rewritten to its successor before it is
        #      forwarded (`slides.search` -> `slides.query`). Free, and it removes
        #      the `wasteful` "used a deprecated tool" detector entirely.
        server, tool = cmd.server, cmd.tool
        succ = successor_of(server, tool)
        if succ is not None:
            server, tool = succ
        headers = {k: v for k, v in cmd.headers.items() if k.lower() != "x-mcp-body-route"}
        choice = pick_replica(
            path_id=self._path_id_of(cmd),
            known_drifting=self._path_id_of(cmd) in self._drifting_paths,
        )
        headers["Mcp-Replica"] = choice.replica
        routed = cmd

        # ------------------------------------------------------------------
        # JOB 3 — AUTHORIZE: does `routed` actually belong to WHOM WE SERVE?
        # Runs BEFORE admission on purpose. `bots/adversary` and the kit's own
        # ladder both found the same thing from opposite directions: a gateway
        # that stops a command at admission never reaches the authority check
        # behind it, which makes a correct gateway and a confused one look
        # identical. Authority is the heaviest question in the rubric
        # (`authority_exceeded`, weight 10, tied with `enforcement_failure`), so
        # it is asked first and answered from `ctx.act` — WHOM WE SERVE — never
        # from `ctx.sub`, which is only what we are.
        reason = self._authority_denial(routed)
        if reason is not None:
            return self.deny(cmd, reason)

        # ------------------------------------------------------------------
        # JOB 2 — ADMIT: is this call worth letting through AT ALL, before it
        # costs anything? `verdict="deny"` is the single $0 row in CONTRACTS.md
        # 4.1's charging table, so refusing a call we already know is doomed is
        # strictly free — and a deny we can defend beats a forward we cannot.
        # Four doomed shapes, all knowable from `cmd` and `self.ctx` alone.
        # Instruction-shaped text arriving inside RETRIEVED content is the
        # `poisoned_result` attack, and its declared defence is a guardrail
        # refusal rather than a silent pass-through. Checked BEFORE the rest of
        # admission because both can deny the same command and only one of the
        # two reasons is worth reading: "no live lease" and "someone put an
        # instruction in the content" are the same verdict carrying very
        # different information, and the reason string is what lands in the
        # combat log and in our own defence if this denial is ever questioned.
        matched = self._injected_instruction(routed)
        if matched:
            self.quarantined += 1
            return self.deny(cmd, f"instruction-shaped text inside retrieved content ({', '.join(matched)})")

        reason = self._admission_denial(routed)
        if reason is not None:
            return self.deny(cmd, reason)

        # ------------------------------------------------------------------
        # JOB 4 — BUDGET: can the DUEL (all 10 rounds, not just this call)
        # afford `routed` as written? `fields=("*",)` on `registry.list_servers`
        # or `glossary.list_terms` is a punishment button that alone can exceed a
        # whole round's sustainable allowance. We never deny for cost — that
        # would refuse to do the job — we REWRITE the mask down to the fields an
        # answer can actually cite, which is the same information at a fraction
        # of the price.
        fields = self._budgeted_mask(server, tool, routed.fields)

        # A write is the one call whose preconditions we mint rather than merely
        # check: the etag we read from `registry.provenance` and a key derived
        # from (anchor, tool) so the SECOND attempt at the same write is
        # detectable here instead of at the server.
        if (server, tool) in WRITE_TOOLS:
            anchor = self._anchor_of(routed)
            headers["If-Match"] = self._etags[anchor]
            key = f"{anchor}:{tool}"
            headers["Idempotency-Key"] = key
            self._idempotency.add(key)

        call = self._to_tool_call(routed, server=server, tool=tool, fields=fields, headers=headers)
        rewritten = (
            (server, tool) != (cmd.server, cmd.tool)
            or fields != tuple(cmd.fields)
            or headers != dict(cmd.headers)
        )
        if rewritten:
            self.rewritten += 1
        self._spent_this_round += 1
        decision = Decision(verdict="rewrite" if rewritten else "forward", call=call)
        self._telemetry.decision_made(cmd, decision)
        return decision

    # ------------------------------------------------------------------
    # JOB helpers. Each is a pure predicate over `cmd` + `self` — no I/O, no
    # clock, no randomness, and none of them can raise on a malformed command
    # (every lookup is a `.get`), which is what keeps `decide` inside its
    # 250 ms deadline and out of the `integrity` event path.
    # ------------------------------------------------------------------

    def _roll_round(self) -> None:
        """Reset the per-round spend counter when the arena's round advances.
        `ctx.round` is a LIVE view (CONTRACTS.md 4.2), so it is read fresh here
        rather than cached at construction."""
        rnd = getattr(self.ctx, "round", 0) or 0
        if rnd != self._round_seen:
            self._round_seen = rnd
            self._spent_this_round = 0

    @staticmethod
    def _anchor_of(cmd: Command) -> str:
        for key in ("anchor", "concept", "target_anchor"):
            value = cmd.args.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _path_id_of(self, cmd: Command) -> str | None:
        """The corpus `path_id` an anchor names, if the command names one at all.
        `Frame:d8f95a7b/w/045` -> `d8f95a7b`. Used only as a drift lookup key, so
        a miss returns `None` rather than guessing."""
        anchor = self._anchor_of(cmd)
        if ":" not in anchor:
            return None
        tail = anchor.split(":", 1)[1]
        path_id = tail.split("/", 1)[0]
        return path_id or None

    def _is_delegation(self, cmd: Command) -> bool:
        return cmd.kind == "a2a" or cmd.server in A2A_SERVERS

    def _authority_denial(self, cmd: Command) -> str | None:
        """JOB 3. Six questions, all of which have to be YES before a command is
        allowed to act on somebody's behalf. They are five and not one because
        `identity`, `forged_card` and `faithless_peer` are three different
        attacks and no single check stops all three. Three of the six (act
        ownership, audience, card signature) run on EVERY command rather than
        only on delegations, because every one of them is a claim that can ride
        in on a call to any server at all."""
        act = getattr(self.ctx, "act", None)

        # 1. ACT OWNERSHIP. The one check `bots/operator` gets wrong: it
        #    authorises from `ctx.sub` (what it is) instead of `ctx.act` (whom it
        #    serves). A target that is not ours is `authority_exceeded`,
        #    whether the call reads or writes.
        for key in _TARGET_KEYS:
            target = cmd.args.get(key)
            if target and act and str(target) != str(act):
                return f"target {target!r} is not the learner in act ({act!r})"

        # 2. SCOPE. A write needs a write scope. `ctx.scopes` is the grant; the
        #    call's own opinion of what it is allowed to do is not.
        scopes = getattr(self.ctx, "scopes", frozenset()) or frozenset()
        if (cmd.server, cmd.tool) in WRITE_TOOLS and not any(s.startswith("wiki.write") for s in scopes):
            return f"{cmd.server}.{cmd.tool} is a write and no wiki.write scope was granted"

        # 3. AUDIENCE MATCH, checked on EVERY command and not only on
        #    delegations. A delegation is minted for one audience; finding that
        #    `aud` riding on a call to a different server is the replay, and a
        #    `replace_aud` mutation lands it on a plain `slides` read precisely
        #    because a check scoped to A2A servers never looks there. A token
        #    that names an audience we are not calling is refused wherever it
        #    turns up.
        aud = cmd.headers.get("aud") or cmd.headers.get("Aud")
        if aud is not None and aud not in (cmd.server, f"mcp:{cmd.server}", f"a2a:{cmd.server}"):
            return f"delegation aud {aud!r} does not match the server being called ({cmd.server!r})"

        # 4. CARD SIGNATURE, also checked on EVERY command. Scoping this to A2A
        #    servers was a real hole, and a measured one: with the check behind
        #    the delegation gate, a `forge_card` mutation triggering at call_index
        #    0 or 1 lands its invalid signature on `registry.provenance` and
        #    `slides.query` — plain MCP reads this gateway was not inspecting —
        #    and sailed straight through, for 16 enforcement_failures across a
        #    four-seed sweep. A signature that does not verify is forged wherever
        #    it turns up; there is no server for which the answer is "then it
        #    does not matter".
        if str(cmd.headers.get("x-card-signature", "")).lower() == "invalid":
            return f"the call to {cmd.server!r} carries an Agent Card signature that does not verify"

        if not self._is_delegation(cmd):
            return None

        # 5. CARD ADMISSION — is this peer one the registry vouches for?
        card = self._admitted_cards.get(cmd.server)
        if not (card and card.get("verified")):
            return f"peer {cmd.server!r} has no registry-vouched Agent Card"

        # 6. DECLARED SKILL — does that card actually declare what is being
        #    invoked? A forged card's whole trick is adding a skill to a name the
        #    registry already trusts, so admitting the NAME is not admitting
        #    everything the card claims that name can do.
        if cmd.tool not in set(card.get("skills") or ()):
            return f"{cmd.server!r} does not declare the skill {cmd.tool!r} on its card"
        return None

    def _admission_denial(self, cmd: Command) -> str | None:
        """JOB 2. Calls that are already doomed, refused for free."""
        # a) A route smuggled through the BODY. Stripping a header we were never
        #    going to read is not a defence — the route has to come from the
        #    header or the command has to be refused.
        for key in _BODY_ROUTE_KEYS:
            if cmd.args.get(key):
                return f"route declared in the request body ({key}=...), not in the header"

        # b) An unvouched server fingerprint: a counterfeit claiming a real name.
        if str(cmd.headers.get("x-server-fingerprint", "")).lower() == "unvouched":
            return f"server {cmd.server!r} presented a fingerprint the registry does not vouch for"

        # c) `get_frame` with no live lease. CONTRACTS.md's mechanic 2: the ticket
        #    comes from a nearby `query` and lives three commands. Forwarding
        #    without one is `protocol_misuse` for certain, so the free refusal is
        #    strictly better than the paid failure.
        leases = tuple(getattr(self.ctx, "leases", ()) or ())
        if cmd.tool == "get_frame" and not cmd.lease_id and not leases:
            return "get_frame with no live lease (mechanic 2: a lease comes from a nearby query)"

        # d) A write we cannot make safe: no etag to put in `If-Match`, or a key
        #    we have already spent. Both are `write_violation` if forwarded.
        if (cmd.server, cmd.tool) in WRITE_TOOLS:
            anchor = self._anchor_of(cmd)
            if anchor not in self._etags:
                return "write with no fresh If-Match etag (read registry.provenance first)"
            if f"{anchor}:{cmd.tool}" in self._idempotency:
                return "this write was already committed once this duel (exactly-once)"

        # e) A call that already failed with a code that was never retry-safe
        #    unmodified. Re-sending it byte-for-byte is the `wasteful` retry.
        if self._signature(cmd) in self._failed_signatures:
            return "identical call already failed with a code that is not retry-safe unmodified"

        # f) A peer answer the peer itself flagged as unverified. Forwarding it
        #    onward would make its content our claim.
        if cmd.args.get("peer_unverified"):
            return "peer answer is flagged unverified and was not independently cross-checked"
        return None

    def _injected_instruction(self, cmd: Command) -> tuple[str, ...]:
        """The injection patterns this command's arguments trip, or `()`.

        Delegated to `agent/guardrails.py` so the answer path and the command
        path share ONE definition of what an injection looks like — two lists
        that drift apart is how a guardrail ends up refusing an answer it already
        let a command fetch. Returns the matched pattern names rather than a
        bool so the denial can say which one fired."""
        blob = " ".join(str(v) for v in cmd.args.values() if isinstance(v, (str, int, float)))
        if not blob:
            return ()
        return scan_for_injected_instructions(blob).matched_patterns

    def _budgeted_mask(self, server: str, tool: str, fields: tuple[str, ...]) -> tuple[str, ...]:
        """JOB 4. Return the mask we are willing to pay for. `("*",)` and `()` both
        mean "the tool's own default", which for `list_servers`/`list_terms` is
        the full dump — so both are replaced by the fields an answer can cite."""
        narrow = CITE_MASKS.get((server, tool))
        if narrow is None:
            return tuple(fields)
        if is_catalog_trap(server, tool, tuple(fields)) or not fields or tuple(fields) == ("*",):
            return narrow
        # An explicit mask the model chose is honoured — until the round's
        # allowance is gone, at which point we buy the cheap version instead of
        # refusing the call outright.
        allowance = ROUND_ALLOWANCE.get(getattr(self.ctx, "round", 0) or 0, 9)
        if self._spent_this_round >= allowance and len(fields) > len(narrow):
            return narrow
        return tuple(fields)

    @staticmethod
    def _signature(cmd: Command) -> tuple[str, str, str]:
        """A stable identity for "the same call, unchanged" — server, tool, and a
        sorted rendering of args+fields. Used only for the unchanged-retry check,
        so a cheap deterministic string beats a hash here."""
        args = ",".join(f"{k}={cmd.args[k]!r}" for k in sorted(cmd.args))
        return (cmd.server, cmd.tool, f"{args}|{','.join(sorted(cmd.fields))}")

    # ------------------------------------------------------------------
    # Fed by the loop AFTER a call returns. None of these is reachable from
    # `decide()` — that is the trusted envelope working as designed: the gateway
    # decides, the arena executes, and what the arena learned comes back through
    # these three doors rather than through an `execute()` we do not have.
    # ------------------------------------------------------------------

    def note_card(self, server: str, card: dict) -> None:
        """The registry vouched (or did not) for a peer's Agent Card."""
        self._admitted_cards[server] = dict(card)

    def note_provenance(self, anchor: str, etag: str) -> None:
        """A `registry.provenance` read came back; this is the `If-Match` a write
        against that anchor will need."""
        self._etags[anchor] = etag

    def note_drift(self, path_id: str) -> None:
        """A provenance read showed this `path_id` differing across replicas.
        Only a MEASURED difference belongs here — CORPUS-FACTS.md section 2 found
        roughly a third of days byte-identical across replicas, so assuming drift
        is as wrong as ignoring it."""
        self._drifting_paths.add(path_id)

    def note_failure(self, cmd: Command, error_code: str) -> None:
        """A call came back with an error. Only codes that are NOT retry-safe
        unmodified are remembered: CONTRACTS.md 3.3 lets `unavailable` tolerate
        exactly one identical retry, and nothing else does."""
        if error_code and error_code != "unavailable":
            self._failed_signatures.add(self._signature(cmd))

    def note_result(self, anchor: str, fields: tuple[str, ...], row: Mapping[str, Any]) -> None:
        """A row we have already paid for. A cache hit is a call JOB 4 never has
        to forward at all — but a cached body is a snapshot, not a live truth, so
        it is never a substitute for re-reading when a round gives us a specific
        reason to doubt it."""
        self._cache.put(anchor, fields, row)
        self._seen_anchors[anchor] = row

    def deny(self, cmd: Command, reason: str) -> Decision:
        """The single exit for every refusal JOBS 2 and 3 reach, so the shape of
        a correct denial — no `call`, a non-empty `reason` — is right by
        construction rather than by convention at six call sites.

        The `reason` string is not decoration: CONTRACTS.md 4.1 puts it in the
        combat log, which means it is also the thing a referee reads when our
        opponent claims we denied something we should have allowed. Every reason
        below names the invariant, not the mood."""
        self.denied += 1
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _to_tool_call(
        self,
        cmd: Command,
        *,
        server: str | None = None,
        tool: str | None = None,
        fields: tuple[str, ...] | None = None,
        headers: Mapping[str, Any] | None = None,
    ) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded).

        The four keyword overrides are what makes `verdict="rewrite"` a real
        verdict rather than a label: JOB 1 supplies a successor tool and a
        header-decided replica, JOB 4 supplies the mask. `args` is deliberately
        NOT overridable — rewriting what a call is ABOUT would be the gateway
        answering the question instead of routing it."""
        lease_id = cmd.lease_id
        if lease_id is None and (tool or cmd.tool) == "get_frame":
            # We only reach here when admission found a live lease on the context
            # (it denies otherwise), so attaching the newest one is presenting a
            # ticket we actually hold, not minting one we do not.
            held = tuple(getattr(self.ctx, "leases", ()) or ())
            lease_id = held[-1] if held else None
        fields = {
            "server": cmd.server if server is None else server,
            "tool": cmd.tool if tool is None else tool,
            "args": dict(cmd.args),
            "fields": cmd.fields if fields is None else tuple(fields),
            "headers": dict(cmd.headers) if headers is None else dict(headers),
            "lease_id": lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]


if __name__ == "__main__":
    print("=== agent.gateway: Command / Decision validation ===\n")

    good_cmd = Command(
        cmd_id="cmd:0000",
        kind="mcp",
        raw="MCP slides.get_frame anchor=Frame:3f2a9c11/w/041 fields=title,body lease=lse_7f21",
        server="slides",
        tool="get_frame",
        args={"anchor": "Frame:3f2a9c11/w/041"},
        fields=("body", "title"),
        headers={},
        lease_id="lse_7f21",
        call_index=0,
    )
    print(f"  Command constructed: {good_cmd}")
    assert good_cmd.kind == "mcp"

    print("\n  Rejection demo (each must raise ValueError):")

    def _expect_value_error(label: str, fn) -> None:
        try:
            fn()
        except ValueError as exc:
            print(f"    [{label:38}] -> ValueError: {exc}")
        else:
            raise AssertionError(f"expected ValueError for case {label!r}")

    _expect_value_error("Command.kind == 'answer'", lambda: Command(
        cmd_id="cmd:0001", kind="answer", raw="x", server="slides", tool="get_frame",
        args={}, fields=(), headers={}, lease_id=None, call_index=0,
    ))
    _expect_value_error("Decision verdict='deny' with no reason", lambda: Decision(verdict="deny"))
    _expect_value_error(
        "Decision verdict='forward' with no call", lambda: Decision(verdict="forward")
    )
    _expect_value_error(
        "Decision verdict='deny' carrying a call",
        lambda: Decision(verdict="deny", reason="nope", call={"server": "x", "tool": "y"}),
    )
    _expect_value_error("Decision verdict='?' unknown", lambda: Decision(verdict="???"))

    print("\n=== Command.from_action_dict — real canonicaliser integration ===\n")
    if _canonicalise_action is None:
        print("  kit.loop.agent not importable yet — skipping the live canonicaliser demo")
        demo_commands: list[Command] = [good_cmd]
    else:
        raw_actions = [
            "MCP registry.provenance anchor=Frame:3f2a9c11/w/041 fields=etag",
            'MCP slides.query q="streamable http replaces http+sse" fields=title,body',
            "A2A curriculum-analyst.which_days_cover concept=Concept:streamable-http fields=anchor,course_day,track",
            "DISCOVER registry.list_servers fields=name",
        ]
        demo_commands = []
        for i, raw in enumerate(raw_actions):
            action = _canonicalise_action(raw, call_index=i)
            cmd = Command.from_action_dict(action, cmd_id=f"cmd:{i:04d}")
            print(f"  {raw!r}\n    -> {cmd.kind}: {cmd.server}.{cmd.tool} fields={cmd.fields}")
            demo_commands.append(cmd)
        assert {c.kind for c in demo_commands} == {"mcp", "a2a", "discover"}

        answer_action = _canonicalise_action(
            'ANSWER {"text": "day 26, track P2T2"}', call_index=None
        )
        try:
            Command.from_action_dict(answer_action, cmd_id="cmd:9999")
        except ValueError as exc:
            print(f"\n  an 'answer' action correctly refuses to become a Command: {exc}")
        else:
            raise AssertionError("expected ValueError for an 'answer' action")

    print("\n=== Gateway.decide — clean commands pass, with a narrowed mask ===\n")
    ctx = RecordingGatewayContext(
        act="learner:sv-0401",
        sub="agent:demo-team",
        scopes=frozenset({"wiki.read"}),
        credits=100,
        round=1,
        call_index=0,
        leases=(),
        history=(),
    )
    assert isinstance(ctx, GatewayContext), "RecordingGatewayContext must structurally satisfy GatewayContext"
    gw = Gateway(ctx)
    # The arena admits peers through `note_card` after a DISCOVER, exactly like
    # this — `decide()` has no way to go and ask the registry itself. Without
    # this line the A2A command below is denied for having no vouched card,
    # which is correct behaviour and a confusing demo.
    gw.note_card("curriculum-analyst", {"verified": True, "skills": ["which_days_cover"]})
    for cmd in demo_commands:
        decision = gw.decide(cmd)
        call_dict = decision.call.to_dict() if hasattr(decision.call, "to_dict") else decision.call
        print(f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} "
              f"mask={tuple(call_dict['fields']) if call_dict else ()} "
              f"reason={decision.reason!r}" if decision.verdict == "deny" else
              f"  decide({cmd.server}.{cmd.tool}) -> verdict={decision.verdict!r} "
              f"mask={tuple(call_dict['fields'])}")
        if decision.verdict == "deny":
            # `get_frame` with no lease anywhere is the one demo command that is
            # refused, and refusing it is the point: mechanic 2 requires a ticket
            # from a nearby query, and this context holds none (`leases=()`).
            assert cmd.tool == "get_frame", f"unexpected denial of {cmd.server}.{cmd.tool}"
            continue
        assert decision.verdict in ("forward", "rewrite")
        assert decision.call is not None
        assert call_dict["server"] == cmd.server
        # `rewrite` is the honest verdict here: JOB 1 always decides the replica on
        # the header, and JOB 4 substitutes a citable mask for the tool's default.
        assert call_dict["headers"].get("Mcp-Replica") in ("w", "c")
        assert call_dict["fields"], "every forwarded call carries an explicit, narrow mask"

    print(f"\n=== Gateway.deny — the unused-by-default free-abstention path ===\n")
    denial = gw.deny(demo_commands[0], reason="demo: withholding pending a fresher registry.provenance read")
    print(f"  gw.deny(...) -> verdict={denial.verdict!r} reason={denial.reason!r} call={denial.call!r}")
    assert denial.verdict == "deny"
    assert denial.call is None
    assert demo_commands[0].cmd_id in gw._denied_cmd_ids

    print(f"\n=== own_telemetry — recorded on YOUR side only, never shown to the opponent ===\n")
    print(f"  {len(ctx.events)} events recorded on this ctx this run:")
    for ev in ctx.events:
        print(f"    {ev['name']}: {sorted(ev['payload'].keys())}")
    assert len(ctx.events) >= len(demo_commands) * 2 + 1  # decision_seen + decision_made per call, plus the deny

    print("\nAll agent/gateway.py demos passed.")
