# Branches, and how production gets deployed

Two long-lived branches, one line of history:

| Branch | Deploys to | Moves when |
|---|---|---|
| `main` | staging, automatically | a pull request merges |
| `production` | production, automatically | you run `scripts/promote` |

`production` is a *pointer into `main`*, never a parallel line of development. Everything is
written on a topic branch, reviewed into `main`, deployed to staging by that merge, and then
promoted — the same commit, the same tree, a second build of an image that has already been
running. Promotion is a fast-forward and nothing else.

That is the whole model. The rest of this file is why, and what to do when it is inconvenient.

## Why a branch and not a tag

Railway watches a branch; it does not watch tags. Since the deploy trigger has to be a branch,
making it anything other than a moving pointer into `main` would mean production could contain
a commit staging never ran — which is the one property the two-environment setup exists to
prevent.

## Creating it

The first promotion creates it — `scripts/promote` handles a missing target and says so:

```sh
scripts/promote
# origin/production does not exist yet -- this promotion creates it.
```

All it does in the end is `git push origin main:refs/heads/production`. The checks around that
push are the point, and they are the same ones every later promotion goes through, so the branch
starts its life having been through the process rather than beside it.

There is deliberately no local `production` branch — not in this checkout, not in any worktree.
A local copy is the one way this process grows a divergent history: you check it out, you commit
something small "just to test the deploy", and now the branches have to be reconciled. Everything
below pushes a SHA straight to the remote ref, so nothing has to be checked out.

If it is ever deleted, recreate it by promoting again; the script handles a missing target.

## Promoting

```sh
scripts/promote --dry-run     # what would ship
scripts/promote               # ship it
```

The script refuses unless:

- the commit is on `origin/main` — production never ships anything that skipped review;
- `production` is an ancestor of it, so the push is a fast-forward;
- every CI job (`lint`, `types`, `docker`, `test`, `db-isolation`) is green **on that exact
  commit**, read from the checks API rather than from "CI was green last time I looked".

Then it prints the commit range and asks. `--yes` skips the prompt, `--skip-checks` skips the CI
gate and asks twice instead.

Promote a commit older than main's tip by naming it — useful when main has moved on since the
version you actually validated in staging:

```sh
scripts/promote 95280bc
```

## The rules are enforced, not just written down

GitHub rulesets, applied with `gh api`:

**`main`** — pull request required (0 approvals; it is a solo repo, the point is the checks and
the diff, not a rubber stamp), the five CI jobs required and up to date with the base, no
force-pushes, no deletion.

**`production`** — the same five jobs required, no force-pushes, no deletion, and **no pull
request requirement**. That last one is what lets `scripts/promote` push directly; the safety
does not come from review, since the commit was already reviewed into `main`, it comes from the
push having to be a fast-forward of a commit whose checks are green.

So the interesting failure mode is covered twice: a direct commit to `production` is possible in
principle, and the next promotion then fails loudly with "the branches have diverged" rather than
silently reverting it with a force-push.

Inspect or change them:

```sh
gh api repos/danielshefer1/remi-po/rulesets --jq '.[] | "\(.id) \(.name)"'
gh api repos/danielshefer1/remi-po/rulesets/<id>
gh api -X DELETE repos/danielshefer1/remi-po/rulesets/<id>
```

Neither ruleset has bypass actors, not even for the owner. The escape hatch is deleting the
ruleset, which takes one command and shows up in the audit log — friction and a trace, rather
than a `--force` that leaves no sign it happened.

CI also runs on pushes to `production`. The check runs already exist on that SHA from the `main`
push, so the ruleset would be satisfied without it; the second run exists so Railway's **Wait for
CI** has something unambiguous to wait for on the ref it is watching, and costs a few minutes on
a branch that moves rarely.

## Rolling back

**Reach for Railway first.** Redeploy the previous deployment from the dashboard or
`railway redeploy`. It is immediate, it needs no git operation, and it puts back an image that
demonstrably worked.

Moving the branch backwards is *not* the rollback path: it is a non-fast-forward push, the
ruleset blocks it, and unblocking it would trade a five-minute outage for a permanently
untrustworthy branch. Instead, once the fire is out:

```sh
git revert <bad commit>     # on a branch, through a PR, into main
scripts/promote             # forward, as usual
```

**The schema does not roll back with the image.** Every deploy runs
`po-db bootstrap && alembic upgrade head` as a pre-deploy command, so redeploying yesterday's
image leaves today's schema in place. That is safe for the additive migrations this project
writes (add a column, add a table) and unsafe for anything destructive — which is the real reason
to keep migrations additive and to drop columns a release *after* the code stops reading them.
A migration that cannot be rolled forward out of needs a real `alembic downgrade`, run
deliberately with `railway run`, not a branch move.

## Hotfixes

Through `main`, like everything else. The full loop — PR, CI, merge, staging, promote — is a few
minutes, and the alternative (commit straight to `production`) produces exactly the divergence
this file spends its length preventing.

The case that genuinely differs is when `main` contains work that must not ship yet. Promote the
older commit by SHA, or revert the unshippable work on `main`. Do not cherry-pick onto
`production`: a cherry-pick is a new commit, the branches diverge, and every future promotion
fails until someone force-pushes.

## Why promotion is not a GitHub Action

A `workflow_dispatch` job that fast-forwards the branch would work and would be one click. It
would also mean a token with push rights to `production` living in CI, which is the thing the
Wait-for-CI arrangement was chosen to avoid. Promotion happens from a laptop that already has the
credentials, a handful of times a month, and the human reading the commit range before typing `y`
is a feature.
