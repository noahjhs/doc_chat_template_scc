# Product

What Casper is for and why, written down for the first time 2026-09-28
alongside the pivot away from a hosted chat product. Before this, the
project had no PRD. These docs come before code: we check new work against
them and update them when a decision changes.

## The pivot (2026-09-26)

- **Bring your own agent.** The built-in OpenAI tool-calling loop
  (`casper_service/conversations.py`) goes away. Casper exposes an **MCP
  server, hosted on the app server** (the only placement that supports
  tools spanning several hosts, e.g. moving files between two people's
  machines), and any MCP-capable agent can drive it. Out of scope for v1:
  placing the MCP server peer-to-peer on the agent's device or the target
  host, and a tool for invoking local agents on a target host.
- **Social.** Owners share access to their hosts with other people and
  their agents, at different levels of trust. The trust framework is the
  core of the product (see [trust-framework.md](trust-framework.md)).
- **Ask-tier approvals are central.** When a sharee's call needs approval,
  the *host owner*, not the caller, is the one notified. A tool that lets
  an agent notify its *own* principal is out of scope for v1.

## Releasable

v1 is releasable when the simplest end-to-end workflow, agent → MCP → remote
host daemon, passes a small but non-trivial task that represents the real
product. There is no prod promotion before that. v1 is an early alpha for
hand-picked participants.

The chosen v1 task is [peer backup](scenarios/peer-backup.md): back up a
folder to a trusted friend's machine, encrypted so the friend can't read
it. It exercises sharing, roles, an ask-tier approval by a third party, and
more than one host, and it is the simplest concrete form of goals 2 and 3.

## Contents

- [values-and-goals.md](values-and-goals.md): why we're building this.
- [trust-framework.md](trust-framework.md): relations, roles, duties,
  delegation, and the platform/app split.
- [risk-model.md](risk-model.md): abuse and failure categories and how each
  is addressed.
- [v1-implementation-plan.md](v1-implementation-plan.md): the phased
  build plan for the peer-backup scenario.
- [scenarios/](scenarios/): step-by-step workflow models, written before
  building each workflow.
  - [peer-backup.md](scenarios/peer-backup.md): the v1 scenario.
