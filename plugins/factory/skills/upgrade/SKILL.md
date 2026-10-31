---
name: upgrade
description: Upgrades a repo's dependencies end to end — scans what can move, reads every release note in between, changes the code for breaking changes and deprecations, proves the code still works with the repo's own checks, and lands grouped upgrade PRs through the repo's gate; risky moves and new capabilities become tasks. Use when asked to upgrade or update dependencies, check for new versions, or when a scheduled factory upgrade run starts.
argument-hint: "[package ...] [--report-only]"
---

# Upgrade

You own this repo's dependencies for one run. Everything repo-specific comes from its `.factory.toml` — read it
first with `factory config`; never assume FamilyOffice (or any repo) conventions that the config doesn't state.
Arguments: `$ARGUMENTS` (packages to limit to; `--report-only` = steps 1–3 and the report, no changes).

The full playbook with commands per ecosystem: [references/playbook.md](references/playbook.md).

## 1. Scan

`factory upgrade scan --json` → candidates (`current`, `latest`, `bump`, `risky`, `manifest`, `notes`). Skip any
package that already has an open PR or task (`gh pr list --search "<name> in:title" --state open`).

## 2. Read the notes — every version in between

For each candidate read the release notes from `current` (exclusive) to `latest` (inclusive): the `notes` links,
GitHub releases (`gh api repos/<o>/<r>/releases --paginate`), the CHANGELOG. Record per package: security fixes
(advisory IDs), breaking changes, deprecations (old → new API), new features. Never upgrade on a version number
alone.

## 3. Map to our code

`grep` the repo for every API a breaking change or deprecation names. A runtime image or language (Node, Python,
Postgres) follows its own LTS / support policy, not just the highest tag. Read the repo's vision / roadmap /
backlog (paths from the repo's CLAUDE.md) to see whether a new feature serves something planned or replaces code
we wrote ourselves.

## 4. Decide each package

| Class | Rule | Outcome |
|-------|------|---------|
| security | any advisory fixed | first PR of the run, whatever the bump |
| safe | patch/minor, no breaking change touching our usage | grouped into one PR per ecosystem |
| breaking | major, or a breaking change we use, not `risky` | its own PR with the code changes |
| risky | name matches `[upgrade] risky`, or data/infra whose upgrade needs a migration or downtime | a task (`[tasks] new`) with the notes and a migration plan — never a PR |
| opportunity | a new feature worth adopting | a task/idea with the release-note link and the code it would change |

At most `[upgrade] max_prs` PRs per run; the rest wait for the next run (say so in the report).

## 5. Change, verify, prove no deprecations

On this run's branch, per PR group (one branch per PR: `<run branch>-<group>`):
1. Bump the manifests (keep the repo's pin style) and run `[upgrade] install`.
2. Apply every code change the notes require — breaking changes **and** deprecations the new versions announce
   for APIs we call. Prefer the replacement API the notes name.
3. Run `[verify] commands` (the repo's full local gate). Fix until green; if a package can't be made green in
   reasonable effort, drop it from the group and file it as *breaking* with what failed.
4. Run `[upgrade] deprecation_check` and pipe its output to `factory upgrade deprecations` — it lists warnings
   raised **from our code**. Fix every one (including ones the upgrade didn't introduce, in files you touched);
   re-run until it reports `0 in our code`.

## 6. Land

Land each PR through the repo's own procedure (`[land] with`: `skill:<name>` → invoke that skill; otherwise run
the command). The repo's review gate and CI apply as for any change — never merge, never push the default branch.
PR body: what moved (from → to), why it is safe (notes read, usage checked, verify + deprecation results),
code changes, links to every release note read.

## 7. Report

End with one short digest (plain words, no colour-only signals): PRs opened, tasks filed, packages left for the
next run, lookups that failed. This is what the principal reads.
