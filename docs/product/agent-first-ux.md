# Agent-first UX: research and principles

Researched 2026-09-30. Casper's model is that **agents onboard and operate
on behalf of people**: a person points their agent at Casper and says "go",
and the agent does the setup. This is how we pursue goal 5, "a handle on
emerging technology" ([values-and-goals.md](values-and-goals.md)). This doc
records what we learned from related design traditions, and the principles
we take from them. The onboarding design
([scenarios/agent-onboarding.md](scenarios/agent-onboarding.md)) applies
them.

## Traditions and what they teach

### Headless software

Headless content systems (Contentful, Sanity) and headless commerce
(Shopify's storefront API) separate the capability from any particular
screen. The API is the product, and screens come and go.

- **Worked:** an API designed for no particular screen outlives every
  screen built on it.
- **Went wrong:** headless moved the work onto whoever built the front
  end. Being headless didn't remove the need for good interface design; it
  changed who was responsible for it.
- **For Casper:** the MCP server is the product, and the agent is one front
  end. **The context files we ship (AGENTS.md, skills) are our interface**,
  and we own their quality as a design team owns screens.

### Agent-first / agent-native

Examples: the `AGENTS.md` convention ("a README for agents", used by tens
of thousands of open-source projects), `llms.txt`, Claude skills, and
SaaS companies shipping MCP servers.

- **Tools shaped as intents, not raw endpoints.** Fewer, larger,
  meaningful tools mean fewer agent mistakes.
- **Safe to retry and resume.** Agents retry, get interrupted, and lose
  their place.
- **Errors that tell the agent what to do next.**
- **The lethal trifecta** (Simon Willison, June 2025): an agent with
  private data, exposure to untrusted content, and the ability to
  communicate externally can be manipulated into leaking data. Meta's
  "Rule of Two": an unsupervised agent may have at most two of the three.
  Consent for consequential actions has to live outside the agent.

### Invisible UI / Zero UI / calm technology

Examples: voice assistants, ambient smart-home devices, and Weiser and
Brown's "calm technology".

- **Went wrong:** people couldn't find what was possible (there's no menu
  to browse), and *automation surprise*, an aviation human-factors term for
  a system doing something the person didn't predict, eroded trust.
- **Lasted:** calm technology's idea that a system can stay out of the way
  and still be inspectable whenever you choose.
- **For Casper:** invisible must not mean unaccountable. People need a
  ledger, receipts, and an agent that can suggest what's possible.

### Declarative configuration

Examples: Kubernetes, Terraform, NixOS, GitOps, Tailscale's policy file.

- **Worked:** you declare the state you want, and the system reconciles
  towards it; `terraform plan` shows exactly what will change before it
  happens, and drift gets detected.
- **Went wrong:** the declaration languages (YAML, HCL) shut out
  non-experts.
- **For Casper:** agents remove that barrier. The person states an intent,
  the agent drafts the change, Casper shows the plan **in plain language**,
  and the person confirms.

### Generative UI

The agent composes the interface at runtime rather than a designer fixing
every screen. The 2026 landscape is a spectrum:

- **static:** the agent selects prebuilt components;
- **declarative:** the agent composes from a catalogue of approved
  components ("where most production agent UIs currently land"; safest,
  because "the agent can only render what the catalog contains");
- **open-ended:** the agent writes the page itself.

**MCP Apps** (the first official MCP extension, from Anthropic and OpenAI,
January 2026) lets an MCP server return a sandboxed, interactive HTML view
that renders *inside the conversation* and can call the server's tools on
the person's click. Claude, Claude Desktop, VS Code Copilot, Goose and
others support it. The spec names approval workflows as a fit ("action
buttons"). Its documentation doesn't list Claude Code, a terminal client.

### Agentic UX design

The field is converging on a short list:

- one recent framework: **Control, Clarity, Recovery, Collaboration,
  Traceability**;
- Microsoft Design: "easily accessible yet largely invisible", "nudging
  more than notifying", and transparency about what an agent knows and can
  do;
- start with **low-stakes, reversible actions**, "suggestions rather than
  transactions";
- overall, a shift from interface-centred to **intent-centred** design.

## Direct precedents for Casper

- **Tailscale sharing.** The owner shares one machine by invite; the
  recipient must accept and can reach **only that machine**; the shared
  machine is quarantined ("can receive incoming connections … but cannot
  start connections"); revoking takes one action; unused invite links
  expire after 30 days.

  Tailscale also has **declarative** sharing between trusted groups:
  double opt-in, with "no individual invitation or share-acceptance step".
  That maps onto our split: invite-and-accept between people, and standing
  policy between groups later.

  Headless machines join with `tailscale up --auth-key=…`, with no
  browser. The docs warn that keys on the command line end up in shell
  history, so they should come from a file or an environment variable.
- **CrashPlan's friend backup.** A free tier let people back up, encrypted,
  to a friend's drive. Code42 ended consumer plans in August 2017, saying
  consumer needs had "diverged sharply" from its business customers'. In
  October 2018 the app stopped working entirely, *including peer-to-peer
  backups*. One reviewer: it was "a major part of how I provided data
  security to far-flung family members." The lessons:
  - people valued it;
  - a peer feature that still depends on the vendor dies with the vendor.

  That supports goal 2: someday, backups should be restorable without
  Casper's server.
- **Syncthing** (both devices must accept each other) and **Tahoe-LAFS**
  (encrypted storage across friends' machines, "least authority") are
  older relatives of the same ideas.

## Principles for Casper

1. **Intent in, plain-language plan out, consent, then action.** The agent
   proposes; Casper's tools can preview any sharing change as a plain
   sentence; the person confirms; then it happens. The agent never quietly
   sets things up.
2. **Start with suggestions.** In onboarding, the agent recommends ("offer
   20 GB to friends, any backup within that allowed?") and the person
   accepts or adjusts.
3. **A ledger the person can always ask for.** "What have I shared, with
   whom, and what's happened?" works at any time, in plain words.
4. **Consent outside the agent for the person's own consequential
   actions.** This is the Rule of Two. An agent may *relay* its person's
   decision about **other people's** requests (a friend's request, a
   friend's backup). It may never approve its own person's requests (their
   own ask-tier commands). Those stay with Telegram or `harness`.
5. **Everything can be undone, as easily as it was set up.** One sentence
   to revoke, cancel or delete.
6. **The agent teaches while it works.** Onboarding should leave the person
   understanding the model (friends, offerings, approvals, encryption), not
   just a finished setup.
7. **Nudge, don't nag.** Notify only for things that need a decision or
   close a loop.
8. **Mechanics in tools, judgement in instructions.** Anything that must be
   done exactly right (installing, pairing, connecting) is a single command
   that's safe to re-run. The context files only cover the conversation and
   the order of steps.
9. **Instructions are interface.** AGENTS.md and the skills get the same
   care as screens, and are tested the same way: a fresh agent follows them
   for real.

## Interface layers (best first)

1. **An approval card inside the person's own chat** (MCP Apps), where the
   host supports it. This comes after v1: it needs OAuth, so that Claude
   Desktop and claude.ai can connect. Before relying on it for security,
   confirm whether the server can distinguish a click in the card from the
   model calling the same tool.
2. **Plain text from the agent.** Works everywhere, including Claude Code.
   This is v1.
3. **A channel outside the agent** (Telegram) for consent on the person's
   own actions, and for nudges when they aren't chatting.

## Sources

- [Tailscale: Share your machines with other users](https://tailscale.com/docs/features/sharing)
- [Tailscale: Declarative node sharing](https://tailscale.com/docs/features/declarative-node-sharing)
- [Tailscale: tailscale up command](https://tailscale.com/docs/reference/tailscale-cli/up)
- [Tailscale: Securely handle an auth key](https://tailscale.com/docs/features/access-control/auth-keys/how-to/secure-auth-keys)
- [TidBITS: CrashPlan Discontinues Consumer Backups (2017)](https://tidbits.com/2017/08/22/crashplan-discontinues-consumer-backups/)
- [TidBITS: CrashPlan for Home Ends Today (2018)](https://tidbits.com/2018/10/22/crashplan-for-home-ends-today/)
- [Simon Willison: The lethal trifecta for AI agents](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
- [Promptfoo: Testing AI's "Lethal Trifecta" (Rule of Two)](https://www.promptfoo.dev/blog/lethal-trifecta-testing/)
- [OpenUI: The State of Generative UI in 2026](https://www.openui.com/blog/state-of-generative-ui-report)
- [MCP Apps overview](https://modelcontextprotocol.io/extensions/apps/overview)
- [MCP blog: MCP Apps — Bringing UI Capabilities to MCP Clients](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/)
- [Microsoft Design: UX design for agents](https://microsoft.design/articles/ux-design-for-agents/)
- [Designing the Internet of Agents (HAX)](https://arxiv.org/pdf/2512.11979)
- [UX Magazine: Designing for Autonomy](https://uxmag.com/articles/designing-for-autonomy-ux-principles-for-agentic-ai-systems)
- [AGENTS.md](https://agents.md/)
