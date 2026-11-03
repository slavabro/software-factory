# Vision: the factory produces the code

**The principal brings ideas. The factory turns them into shipped, reviewed, maintained code.** It does the
breaking down, designing, building, reviewing, landing and upkeep, and asks only where a decision is genuinely theirs.
It works across every repo the principal owns, on any laptop, the same way.

FamilyOffice is the first repo it serves; today the factory's parts there are hand-run skills (`/design-task` →
`/review-design` → `/compact-design` → `/implement-feature` → `/sync-docs` → `/complete-feature` → `/land`) plus the
tower that watches the agents. The direction is to make that pipeline the factory's own product: generic, driven by
the factory instead of by the principal typing each step, and with its quality checked by machines rather than
remembered.

## The principal's experience

1. **An idea, in their words** — in a terminal, on Telegram, or to the tower ("the Costs page should compare
   last month"). Nothing else is required: no task ID, no format.
2. **One shaping conversation** — the factory reads the repo, then asks the few questions only the principal can
   answer (scope, trade-offs, what "done" looks like) and says how it will split the work.
3. **The factory runs** — small tasks, each designed, reviewed, built, reviewed again and landed by agents, in
   parallel where the dependency graph allows.
4. **Decisions arrive as decisions** — one queue (terminal or phone) with only what needs them: a design choice, a
   merge the rules mark ASK, a failed deploy, a question an agent can't answer from the code. Each with options and
   a recommendation.
5. **Upkeep happens on its own** — dependencies, skills, CI health, docs drift: found, proposed and mostly fixed
   without being asked.

## How work flows

| Stage | Who | Output | The principal |
|-------|-----|--------|---------------|
| Intake | intake agent | idea recorded in the repo's task system, verbatim | — |
| Shape | shaping agent | questions answered, scope fixed, split into small vertical tasks with dependencies | answers questions, approves the split |
| Design | designer + domain reviewers (fresh context each) | design per task, reviewed, compacted | approves designs above a size threshold |
| Build | implementer | code + tests on a branch, `make ci` green | — |
| Review | code / architect / designer lenses (fresh context) | gate block: fixed or rebutted findings | — |
| Land | lander + tower gate | PR, CI green, merged when the rules say MERGE | OKs only ASK merges |
| Upkeep | scheduled scouts | upgrade PRs, drift fixes, new ideas into the inbox | sees results, decides on proposals |
| Learn | every stage | a correction becomes a rule (skill line, banned pattern, hook) so it can't recur | — |

## Principles

- **Ideas in, decisions out.** The principal never has to drive a step. Everything they see is either a result or a
  decision with options.
- **Small tasks, each landable alone.** A task that can't ship by itself is split again.
- **Fresh eyes review.** Reviewers never see the author's reasoning, only the diff and the rules.
- **Rules are code.** A practice that matters is enforced (linter, CI check, hook), not remembered. A correction
  from the principal turns into one of those, or into a skill line that points at its owner.
- **Autonomy is earned per capability.** Each stage starts by asking and moves to acting alone once its record is
  good (the merge gate's MERGE/ASK split is the model).
- **One owner per fact.** The factory points at a repo's own rules; it never copies them.
- **Portable.** Same behaviour on any laptop and in CI; repo specifics live in repo config, not in the factory.
- **Red-green safe, plain words** in everything the principal reads.

## Not the goal

- Replacing the principal's judgement on what to build or on trade-offs that are theirs.
- Scale machinery for a team: this is one principal's factory.
