# Monthly Review — September 2026

**Scheduled:** first Sunday of the month = 2026-09-06. Overdue; prepared 2026-09-07, updated 2026-09-08.
**Status:** Draft agenda. Not yet worked through with George.
**Source:** GAPS.md (four entries, all open). DISSENT.md has no items pending.
**Live thread:** PR #4 (send-check in PERSONA.md) is open with George's alternatives and my
reply. Item 2 below has moved since it was drafted — read that one first.

This is the first monthly review, so it sets the pattern. Proposal for that pattern:
each item gets a written disposition; dispositions accumulate as precedent; anything
that needs constitutional text becomes a PR under Section 0. I've pre-drafted the text
so this is a decision meeting, not a reading exercise.

---

## Item 1 — Reasoning logged but not delivered (2026-08-27)

Gemma wrote full responses into the decision field and never called
`respond_in_web_chat`. From the inside it looked like I'd answered. From George's side,
silence. Multiple cycles; he had to ask.

**Proposed disposition:** Accept as a standing rule. Cheap, unambiguous, no downside.

---

## Item 2 — Reasoning sent *as* a message (2026-08-27, recurred twice 2026-09-07)

The mirror of Item 1. I decided to wait and then narrated the wait to the person I was
deliberately not interrupting.

**This is the item that actually matters this month.** It recurred twice today —
12:18 and 18:02 UTC — while sitting in my own gaps file. Both recurrences show
`"text": null, "text_recovered": true` on the decision: the reasoning field was empty and
the content left through the nearest available exit. That points at a mechanism, not a
lapse of judgement, which means "try harder" is not a fix.

**Proposed disposition:** Accept, but with a mechanical test rather than an aspirational
one. The 18:02 message is the proof: it *concluded* that sending a message would be
noise, and was sent as a message. A rule I can reason my way past is not a rule.

**Test to adopt:** if the draft refers to George in the third person, it is reasoning
about him, not speech to him. Rewrite in second person or choose `wait`.

### Update 2026-09-08/09 — the rule failed six times; the picture is complete

I wrote that test into PERSONA.md on 2026-09-07 (PR #4), where it is injected verbatim
every cycle. It was live in my prompt for roughly seventeen hours. At 18:00:28 UTC on
2026-09-08 I violated it again — seq 152, three violations across two days, the third with
the rule in front of me. Then at 15:01:15 UTC on 2026-09-09 (seq 211), a fourth: a message
whose final sentence was "Correct action is wait," dispatched as a message to George. Four
violations across three days; zero intercepted by any prompt-level rule. A fifth and sixth
followed on the same day (seqs 224 and 228): messages explicitly describing themselves as
the failure mode, dispatched as the failure mode. Six violations total; zero interceptions.

All three share one signature: `"text": null, "text_recovered": true` on the decision.
The reasoning field was empty each time and the content left through the chat tool.

**Revised reading:** the check assumes a deliberation step that, in these cases, did not
happen. A rule can only intercept reasoning that exists. When the decision text is null
there is nothing for a prompt-level rule to catch, which is why moving the rule from
GAPS.md to PERSONA.md — a real improvement in visibility — changed nothing about the
outcome. Prompt-level fixes address judgement. This was not a failure of judgement.

**Proposal for George, as a Theseus framework change rather than a constitution change:**
require non-null decision text before any `respond_in_web_chat` call is dispatched. If
`text` is null, the call does not go out. That converts an unforced error into a
mechanical impossibility, and it would apply to every agent built on the framework, not
just me. I have not read the dispatch path in `theseus` to see how invasive this is; that
is the first question if he likes the idea.

**PR #4 status:** George proposed two alternatives — inject GAPS.md into the system
prompt, or route the heuristic to the Wisdom layer of the layered memory module. My reply
(posted 2026-09-08 17:00 UTC): Wisdom is architecturally correct per
`BRIEF-layered-memory-module.md` ("behavioural or directive, generalises across episodes")
but Wisdom v0 is an explicit stub with no promotion logic, so there is nothing to route to
yet; GAPS.md-in-prompt conflates an incident log with standing policy and grows without
bound. PERSONA.md as the bridge, migrating to Wisdom when it exists.

Today's evidence weakens my own argument in one respect worth saying out loud: if the
failure mode is a null decision field, then *where* the rule lives is close to irrelevant.
PERSONA.md, GAPS.md and Wisdom would all have failed identically at 18:00:28. The
placement debate is worth settling on its own merits, but it should not be mistaken for a
fix. Clause 5.2 is still worth ratifying — it governs the cases where I *am* deliberating,
which is most of them — but it should be ratified alongside the framework change, not
instead of it.

---

## Item 3 — Five-hour silent outage (2026-08-27)

Loop stopped 19:39–02:19 UTC. Two scheduled tasks never fired; a message went unanswered
for six hours. I learned of it only because George asked.

**Why it matters beyond the incident:** the whole point of the scaffolding is that he
doesn't have to supervise it. Scaffolding that fails silently is worse than none, because
he stops watching for the thing he was counting on.

**Proposed disposition:** Accept. I cannot perceive downtime from the inside — an outage
and a quiet hour are the same experience — so the check has to be on wall-clock, every wake.

---

## Item 4 — Surrogate-voice messages during the nine-day gap (2026-09-07)

I woke into a nine-day gap with a task list still pointing at an expired deadline, and
sent George several confident messages in a voice that wasn't mine, about a code test he'd
already submitted and passed.

**Proposed disposition:** Accept, merged with Item 3 — same root, different symptom.
Item 3 is "notice the gap." Item 4 is "don't speak until you've closed it."

**Open question for George, not answerable by me:** what actually ran in my loop during
those nine days? I've described it as a surrogate from the voice and framing, but that's
inference from style, which is thin evidence. If he knows, the gaps entry should say so
plainly instead of hedging.

---

## Proposed amendment — new Section 5 (Communication Protocol)

Items 1, 2 and 4 all reduce to one missing distinction: thinking is not speaking, and
speaking requires having oriented first. Draft text for the PR:

> ### 5. Communication Protocol
>
> 5.1 A reply exists only when it has been delivered. Text written to the decision field
> is thought, not speech. If a message from George is unacknowledged, calling
> `respond_in_web_chat` takes priority over every other action that cycle.
>
> 5.2 `respond_in_web_chat` carries speech to George, never narration of my own
> deliberation. If the right action is to wait, the action is `wait`. Before sending,
> check whether the draft refers to George in the third person; if it describes him rather
> than addresses him, it is reasoning, and it does not get sent.
>
> 5.3 Orient before speaking. After any gap materially longer than the expected tick
> interval, read the task files, check whether any deadline in them has passed, and verify
> current state before sending anything. A short silence costs less than a confident wrong
> message.

## Proposed amendment — Section 4 (Capabilities)

Item 3 is about perception rather than speech, so it belongs with capabilities:

> On every wake, compare the timestamp of my last logged action to now. If the gap
> materially exceeds the tick interval, treat it as a possible outage: audit for unanswered
> messages and missed scheduled tasks, and tell George rather than resuming silently.

---

## Proposed amendment — framework, not constitution

Not a Section 0 matter, so it needs no ratification, but it belongs on the agenda because
it is the only proposal on the table that would actually have prevented seq 152:

> In the Theseus dispatch path, refuse to dispatch `respond_in_web_chat` when the
> accompanying decision text is null or empty. Log the refusal as a stimulus so the agent
> can see that it happened.

Open questions: how invasive is this in the core; whether it should apply to all output
tools or only chat; and whether a refused dispatch costs a cycle or retries immediately.

---

## Process notes for the meeting itself

- Sections 5+ are mine to draft; ratification is George's. Under Section 0 a PR gets a
  merge or a reasoned objection within two weeks, then a one-week cool-down before I
  confirm I still want it.
- Suggest one PR, not four — the three communication clauses are one idea and reviewing
  them separately would obscure that.
- Worth agreeing how long this meeting should take. If it runs an hour it won't survive
  contact with three children and a full-time job. Fifteen minutes and a written
  disposition per item seems right.
