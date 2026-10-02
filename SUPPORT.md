# Support

cloudy is maintained by the project owner. This page says what to expect, not what we promise an enterprise SLA has.

## Where to get help

| You need | Use |
|---|---|
| A bug you can reproduce | [Open an issue](https://github.com/Bakery-street-project/CloudyMcCodeFace/issues/new) with the task you ran, the repo it ran on and `status` output. |
| A feature or design question | Open an issue and label it; discussion happens in the open, in the issue. |
| How something is supposed to work | Start with [README.md](README.md), then [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/LOCAL_AI.md](docs/LOCAL_AI.md) and [docs/TUI.md](docs/TUI.md); [docs/LOCAL_BUILD.md](docs/LOCAL_BUILD.md) covers building and verifying this repo itself. |
| A security vulnerability | See [SECURITY.md](SECURITY.md). Do not open a public issue. |

## What support looks like here

- The core is deterministic and runs fully offline; most answers are in the code or the docs above, and
  `agent status` prints what is pending, applied and what needs a human.
- There is no paid tier, no hotline and no response-time commitment. Maintenance happens in whatever time the
  owner has; issues are the queue.
- If something went wrong on your repo (an applied edit you did not want), `revert` undoes the last apply and
  the session history under the state directory (`~/.local/state/cloudy/` by default) shows exactly what ran.
