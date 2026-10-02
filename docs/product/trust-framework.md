# Trust framework

The core of the product (goal 1). This is a working model, not a final one:
check it against each scenario and revise it here.

## Two senses of "role"

"Role" answers two different questions:

- **What are you in relation to me?** friend, family, stranger
- **What is your alignment with this group, project or process?** member,
  maintainer, approver

We keep both under one model by treating each as a **relation**, just aimed
at a different kind of object. This is relationship-based access control
(ReBAC), from Google's Zanzibar paper; OpenFGA and SpiceDB are open-source
implementations. Everything is stored as `(subject, relation, object)`
tuples, and permissions are derived from them:

```
(alex,  friend,     noah)           # person → person
(alex,  maintainer, garden-coop)    # person → group
(alex,  backup-peer, noah@alex-mini)  # person → resource, granted by its owner
```

## The model

- **Relations** are the social facts: edges between people, and between
  people and groups.
- **Grants** attach policy-layer bundles to a relation on a resource, e.g.
  "friends of mine who I've made backup peers get the *Backup Peer* layers
  on `alex-mini`". A **role** is a named grant. The policy layers underneath
  are the existing daemon-enforced allow/ask/deny rules, unchanged.
- **Duties** are *responsibilities* rather than access: approving ask-tier
  calls, maintenance on-call, keeping a replica alive. They attach most
  naturally to group relations, because a duty is owed to something shared.

## Offers, requests and grants

Access with no duties can simply be **granted**. Nobody's obligations
change, so no acceptance is needed.

A duty is different: the recipient is the one who has to perform it, so
they must agree to it. Access that carries a duty is therefore an **offer**.
It becomes a grant only once the recipient accepts the duty prescribed with
it. "You can administer the co-op server, provided you take approver duty on
alternate weeks."

The flow can start from either side:

- **Owner-initiated:** the owner makes an offer (with duties) or a grant
  (without).
- **Requester-initiated:** an owner publishes **offerings**, the kinds of
  access they're willing to share, visible to people with the right
  relation (e.g. friends). A would-be sharee browses them and **requests**
  one. The owner then approves the request, which produces a grant. We
  expect this to be the more common flow.

Either way, the result is consensual on both sides.

**Default to generosity** ([values-and-goals.md](values-and-goals.md),
goal 4). An offering requires nothing in return by default. A request
defaults to offering something back, which the other side may decline.
Reciprocal or bartered arrangements can be expressed (an offer whose duty
is "mirror for me too"), but they're the exception, not the template. Future work: attaching
**conditions** to an offering, e.g. "backups only if you declare what's in
them".

The daemon remains the sole, authoritative enforcer. The trust framework
decides *which* policy layers apply to a caller; it never replaces the
daemon's own matching.

## Agents as subjects

Agents are subjects in the model, like people, so they can hold some
human-like abilities. Most importantly, **approvals can be delegated to an
agent**.

This is the intended happy path. Most people will want to delegate most
approvals to an agent they trust to decide as they would, rather than being
pinged for every ask-tier call. The product should make it easy to set up
such an agent and to build confidence in it, e.g. by showing what it would
have decided before letting it decide for real.

**v1:** an agent connected through MCP acts **on behalf of** one principal.
Its effective permissions are at most its principal's, and possibly narrower
if the principal scopes the agent's connection. Grants go to the
principal–agent pair as a single unit. The audit log records both.

**Later:**

- Grant access to the *principal*, separately from the agent they happen to
  be using.
- **Agents beyond a single principal (research topic).** An agent may serve
  something other than one person: an organisation, or a process or project
  whose constraints several human parties have agreed on. Other ways to
  relate to an agent besides ownership then matter, such as sponsoring,
  auditing, constraining, or co-governing it. There is precedent to study:
  delegation vs. impersonation in OAuth token exchange (RFC 8693),
  verifiable credentials for agent identity, and multi-party governance
  such as multi-signature control. We want the model to stay open to this:
  an agent's trust should not be *definitionally* tied to a single owner.

## Approver delegation

By default, the host owner approves ask-tier calls on their hosts.
Delegation lets them name someone else, or a group relation such as
"maintainers of garden-coop", to approve on their behalf, optionally scoped
to certain rules or hosts.

- Delegation is **always explicit and owner-granted**. We never fall back
  to another approver automatically, even if the owner is unreachable (that
  would break goal 2).
- Delegation is a duty in the sense above, so the delegate accepts it (it
  is an offer). The delegate can be a person or an agent.
- Revoking it takes effect immediately, like every other grant.

## Platform layer vs. app layer

- **Platform:** a comprehensive, fully expressive configuration space.
  Arbitrary relations, arbitrary grants, the full policy-rule schema.
- **App:** the curated subset we actually offer (goal 4). A fixed set of
  relations (friend, family, member, maintainer) and named roles (e.g.
  *Backup Peer*, *Guest*), with only the choices we're confident are safe
  and legible.

A comparable split is the relationship between AWS IAM and the AWS
products built on it.

## Open questions

- Should relations be mutual (friend requires both sides) or one-sided
  (follow)? The peer-backup scenario assumes mutual.
- Do grants attach to an individual ("Alex") or a relation class ("my
  friends")? v1 assumes individual grants, reached through a request, with
  the relation as a precondition.
- Adopt OpenFGA or SpiceDB, or write a small tuple store ourselves? That
  decision comes after the model has survived a few scenarios.
- How can an owner see *everything* they have granted, in one place?
