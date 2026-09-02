# Deferred, on purpose

Decisions that were made and then postponed, with the reason and the trigger that should
bring each one back. Nothing here is forgotten work; it is work whose cost is currently
higher than its benefit.

---

## 1. Make `main` writable only through a pull request

**Status:** deferred 2026-09-02.
**Trigger:** when the integration reaches a stable point and changes stop being frequent.

**Why it is deferred.** Right now nearly every change is infrastructure — hooks, skills,
permission rules, the deployment layer. Forcing each of those through a pull request would
cost more than it protects while the shape of the thing is still moving. That is a
judgement about *timing*, not about whether the guardrail is right.

**Why it comes back.** A `deny` rule in `.claude/settings.json` only constrains the agent,
and only the exact spelling of the command it names. It held on 2026-09-02 because the
agent chose to respect it rather than reach for the bare `git push` that would have
slipped past the pattern. That is not a guarantee.

### The three layers, and what each actually covers

| Layer | Stops | Bypassed by |
|---|---|---|
| `deny` rule in `settings.json` | the agent, and only that spelling of the command | writing the command another way |
| `pre-push` git hook | anyone on this machine | `--no-verify`, or a clone without `core.hooksPath` |
| **GitHub branch protection** | **everyone, always** | nothing local |

A permission rule cannot express "refuse if the branch is `main`": rules match command
text, and `git push` does not say which branch it pushes — that depends on the current
branch and the configured upstream. Git itself does know, which is why the check belongs
there rather than in a Claude hook. A git hook is also strictly better than a Claude hook
here, because it constrains every caller rather than only the agent.

### The local half: a `pre-push` hook

`.githooks/pre-push`:

```bash
#!/usr/bin/env bash
# Refuse to push main directly. Git feeds one line per ref on stdin:
#   <local ref> <local sha> <remote ref> <remote sha>
while read -r _ _ remote_ref _; do
    if [ "$remote_ref" = "refs/heads/main" ]; then
        echo "pre-push: refusing to push main directly. Open a PR." >&2
        exit 1
    fi
done
```

Then:

```bash
chmod +x .githooks/pre-push
git config core.hooksPath .githooks
```

It sees the refs however the push was invoked — `git push`, `git push origin main`,
`git push --all` — because git hands them to the hook rather than reconstructing them from
the command line.

**Two things to get right when this is enabled:**

- `.gitignore` starts with a blanket `.*`, so `.githooks/` would be ignored exactly as
  `.claude/` was. It needs a `!.githooks/` exception, or the hook exists only on the
  machine that wrote it.
- `core.hooksPath` is per-clone. It is not inherited by a fresh clone, so it has to be set
  again on the server and anywhere else the repo lands.

### The half that actually holds: branch protection

GitHub → Settings → Branches → Add rule on `main` → *Require a pull request before
merging*. Server-side, so no local configuration evades it — not `--no-verify`, not a
clone without the hook, not a misconfigured agent.

The local hook catches mistakes before they leave the machine. Branch protection is the
one that is true regardless.

---

## 2. Cost per run on the Trello card

**Status:** deferred.
**Trigger:** the runner, which is where `claude -p --output-format json` returns the usage.

There is no per-run boundary while runs are launched by hand, so there is nothing to
attribute a number to. Note for when it lands: a subagent's usage does not appear inline in
the parent's transcript and has to be summed separately.

---

## 3. "No new modules" as a hook

**Status:** deferred.
**Trigger:** the runner, which supplies the ticket's card id in the environment.

It cannot be a `deny` rule: denying writes under `custom_addons/` would also block the
module the pipeline is supposed to edit, and `deny` has no exceptions. Until the hook
exists the rule lives in `CLAUDE.md` as advice only, and should not be assumed enforced.
The same applies to the other half — refusing to delete a module unless the ticket carries
a given label.

---

## 4. The `redirect-to-https` middleware in `docker-compose.yml`

**Status:** carried over from production untouched.

The label defines a Traefik middleware that no router references, so it does nothing. It
was kept verbatim when production's compose file was rescued, to avoid changing behaviour
during the rescue. It should either be attached to the router or removed.

---

## 5. The `no-se-que-tiene` branch

**Status:** leftover from untangling the repositories on 2026-09-01.

It holds the working-tree snapshot taken before the nested repository was removed. Once
production has been rebuilt and nothing is missing, it can go.
