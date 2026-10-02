# Values and goals

Values set by the Principal on 2026-09-26. Goals were drafted in discussion
and confirmed on 2026-09-28.

## Values

- Create a social fabric of collaboration and trust.
- Promote information sovereignty.
- Enable resilient community infrastructure.
- Attract principled and community-minded participants.
- Give people a handle on emerging technology.

## Goals

1. **A trust framework.** Granting scoped access to your own machine should
   feel as normal and low-stakes as sharing a doc, not like handing over
   your house keys. Success means people feel confident enough to share.

2. **Sovereignty through interdependence.** Replace reliance on a single
   provider, including us, with reliance on a trusted community. The
   near-term form is letting people distribute their data across trusted
   peers' hosts. The long-term vision is groups gaining collective
   bargaining power with service providers. This is not achievable with the
   product today, but we align with it where we can.
   - *Standing rule:* Casper should never become the only place a
     person's data or control lives.
   - The owner's policy is always final, and revocation is instant and
     total.

3. **Resilience through redundancy.** There are two kinds:
   - *Resources:* the same data or service lives on several members' hosts.
   - *Responsibility:* stewardship is shared, so if one maintainer lapses,
     another person, or an agent acting for them, steps in. This is the
     reason for **approver delegation** (see
     [trust-framework.md](trust-framework.md)).

4. **Curation shapes the community.** Who participates, and how well they
   match these values, depends on the structures we provide and the
   workflows we support best. We offer a few opinionated workflows rather
   than a general-purpose tool. Curation lives in the app layer.

   **The community model is a gift economy: default to generosity**
   (Principal, 2026-10-01). People want to be known as providers of
   resources to their community. Casper's defaults favour giving:
   - offerings don't ask for anything back;
   - asking for help comes with an offer to help in return, which can be
     declined;
   - what's visible about a person is what they give, not what they owe.

   Reciprocity and barter remain possible, but they aren't the featured
   collaboration model.

5. **Legibility through modelling.** Granting, reviewing and revoking
   access must make sense to someone who isn't comfortable with a policy
   DSL or a CLI. Today's surface (YAML, `harness`) is the opposite. We get
   there by modelling each workflow first ([scenarios/](scenarios/)) and
   designing the UX from that model, not from the policy engine upward.
