# Commons governance: gratitude, Ostrom, and group rules

Research for design conversations, written 2026-10-15 to follow the
Principal's decisions in [community-model.md](../community-model.md).
Three parts: how thanks stays genuine in public; Ostrom's design principles
mapped to Casper; and governance patterns for groups. Recommendations and
questions for the Principal are at the end. Related:
[mutual-arrangements.md](mutual-arrangements.md), [gates.md](../gates.md),
[trust-framework.md](../trust-framework.md).

## 1. Genuine gratitude in public

### What the psychology says

- **Gratitude builds relationships.** Sara Algoe's *find, remind and bind*
  theory: gratitude helps people find good partners, reminds them of the
  ones they have, and binds them closer
  ([Algoe 2012](https://asset-pdf.scinapse.io/prod/2103781831/2103781831.pdf),
  *Social and Personality Psychology Compass*). The trigger is a partner's
  *responsiveness*: thanks that names what the giver did for *me* says "you
  noticed what I needed". Generic thanks says nothing of the kind.
- **Thanks makes helpers help again, and help others.** Grant & Gino
  (2010): helpers who were thanked were about twice as likely to help again
  (55% vs 25%), because they felt **socially valued**, not merely
  competent. The effect spilled over to helping a different person
  ([summary](https://www.gsb.stanford.edu/insights/frank-flynn-gratitude-gift-keeps-giving);
  *Journal of Personality and Social Psychology* 98(6)).
- **Rewards can crowd out the motive they reward.** A meta-analysis of 128
  experiments found that expected, tangible rewards reduce intrinsic
  motivation; unexpected, informational praise does not
  ([Deci, Koestner & Ryan 1999](https://depts.washington.edu/techdocs/papers/deciExtrinsicRewardsAndIntrinsicMotivation99.pdf)).
  Economists call it *motivation crowding*: outside incentives can crowd
  motivation out when they feel controlling, or in when they feel
  supportive ([Frey & Jegen 2001](https://ideas.repec.org/p/ces/ceswps/_245.html)).
  The classic field case: a fine for late daycare pickup *increased*
  lateness, because the fine turned a social obligation into a price, and
  lateness stayed high after the fine was removed
  ([Gneezy & Rustichini 2000](https://rady.ucsd.edu/_files/faculty-research/uri-gneezy/fine.pdf)).
- **Public giving runs on image, which incentives spoil.** People give more
  when others can see it, and paying them for it works *worse* in public
  than in private, because the payment muddies what the gift signals
  ([Ariely, Bracha & Meier 2009](https://www.bostonfed.org/publications/research-department-working-paper/2007/doing-good-or-doing-well-image-motivation-and-monetary-incentives-in-behaving-prosocially.aspx),
  *AER*). Visibility of giving is a real motive (our "giving is visible"
  rule leans on it), but anything that looks like payment for it destroys
  the signal.

### What platforms learned

| Platform | Mechanism | What happened |
|---|---|---|
| **Wikipedia Thanks** | One click thanks a person for a specific edit; the recipient is notified privately; it's logged but not counted on profiles | Thanks flows mostly *upward*, from newer to more experienced editors, and receiving it is followed by more activity ([Goel, Anderson & Zia 2019](https://arxiv.org/abs/1903.03308)). In a field experiment across four Wikipedias (15,558 editors), volunteers thanking people raised two-week retention by 2 points and recipients sent ~1.6× more thanks themselves, but total time contributed didn't change ([CAT Lab, Wikimedia newsletter](https://meta.wikimedia.org/wiki/Research:Newsletter/2020/July); [Nieman Lab](https://www.niemanlab.org/2020/07/giving-thanks-isnt-just-for-turkey-day-its-also-a-way-to-retain-your-users/)). A companion study found people assigned to *give* thanks felt no better, and heavy mentors reported feeling drained ([preprint](https://osf.io/m9cy6/)). |
| **Wikipedia barnstars** | A peer-written award posted on the recipient's talk page, always with a personal note | A randomized barnstar raised top editors' productivity 60% for 90 days and made further barnstars from others six times likelier ([Restivo & van de Rijt 2012](https://pmc.ncbi.nlm.nih.gov/articles/PMC3315525)). The follow-up was titled ["No praise without effort"](https://research-portal.uu.nl/en/publications/no-praise-without-effort-experimental-evidence-on-how-rewards-aff/): recognition works on people already giving; it doesn't conjure giving. |
| **Stack Overflow reputation** | Points and badges for answers; reputation unlocks moderation powers | Drove early growth, but the score became a hierarchy: newcomers met closed questions and curt comments, and those who hit such adversity were far less active afterwards ([neophytes study, 2022](https://www.scitepress.org/Papers/2022/110811/); [Univ. of Limerick](https://pure.ul.ie/en/publications/the-ugly-side-ofstack-overflow-an-in-depth-exploration-ofthesocia/)). Gamified promotions moved experienced users, not novices. |
| **Couchsurfing references** | Public, mutual references after a stay | 87–90% positive and about 2,500 positives per negative: references became reciprocal courtesies, not information ([Lauterbach et al. 2009](https://www.gnuband.org/tag/adamic/)). Members avoid writing honest negatives because direct reciprocity (thanking a host) collides with the community's need for warnings ([Mikołajewska-Zając 2018](https://research.cbs.dk/en/publications/terms-of-reference-the-moral-economy-of-reputation-in-a-sharing-e/)). |
| **Buy Nothing groups** | Three post types: gift, ask, **gratitude**; gifts given "without any expectation of reward" ([rules](https://buynothingproject.org/10-rules), [guidelines](https://docs.buynothingproject.org/guidelines)) | Gratitude posts are often about the *community* or the *story* of a gift, not a score for the giver. No counts, no profiles of giving. The closest living example of what we want. |
| **Workplace kudos (Bonusly, Kudos, Slack bots)** | Each person gets a points allowance to give colleagues | Evidence is mostly vendor-published. Bonusly's own data shows people avoid returning a bonus to whoever just gave them one ([Bonusly](https://bonusly.com/post/trust-employees-with-peer-bonuses)), a sign that people sense thanks shouldn't look like a trade. Allowances that "expire" invite perfunctory end-of-period giving: the crowding-out pattern in miniature. |

### Distilled: what keeps thanks real

Thanks feels real when it is:

1. **Specific**: names the act and what it made possible ("Your copy was
   there when my disk died"), which is Algoe's responsiveness.
2. **Unprompted, or prompted only by a real moment**: a restore, a first
   full copy, a year of keeping. Never on a schedule, never a "rate your
   peer" form.
3. **Private first**: the giver hears it before anyone else does.
4. **Controlled by the recipient**: they choose whether it's shown to the
   group, like a barnstar they may keep on their page or not.
5. **Written by a person**: an agent may suggest the moment, never the
   words, and never sends thanks on someone's behalf.
6. **Free of counts**: no tallies, leaderboards, streaks or "most thanked".

What poisons it: counts and rankings (status becomes a score); allowances
or quotas (thanks becomes an obligation); mutual or public-by-default
ratings (courtesy replaces truth); tangible rewards attached to giving
(crowding out); and power attached to standing (the Stack Overflow
hierarchy).

## 2. Ostrom applied

Ostrom's eight principles for long-enduring commons (*Governing the
Commons*, 1990; [list](https://wiki.p2pfoundation.net/Elinor_Ostrom%E2%80%99s_Eight_Commons_Governance_Design_Principles)),
each with what it means for Casper:

| # | Principle | For Casper |
|---|---|---|
| 1 | **Clear boundaries**: who belongs and what the resource is | Group membership is explicit (`member` tuples), and so is each offering ("20 GB on this Mac, for Mutual Aid"). |
| 2 | **Rules fit local conditions**, and what you take relates to what you give | Each group sets its own norms (who can invite, how much to offer, expected uptime) within Casper's safe defaults. No global quota. |
| 3 | **Those affected make the rules** | Rule and membership changes follow the group's own decision rule (part 3). |
| 4 | **Monitoring by members or people accountable to them** | Protection status ("your copies are on 2 networks; Sam's Mac has been off 9 days") is visible to those it concerns. Casper measures; the group sees. |
| 5 | **Graduated sanctions** | Soft and impersonal first: a quiet nudge, then Casper re-places copies away from a sleeping holder, and only then a group decision about membership. Never public shaming. |
| 6 | **Cheap, fast conflict resolution** | A low-key way to raise an issue with the organizer or stewards, and an easy, blameless exit ("leave, and your copies move elsewhere"). |
| 7 | **Outside authorities recognize the right to organize** | Casper (the higher authority here) doesn't override a group's rules, except for platform-level abuse handling, which is stated up front ([risk-model.md](../risk-model.md)). |
| 8 | **Nested enterprises** | Groups within groups, and groups linked to groups: a household inside a neighbourhood circle; a member's spare space offered to a second group; recovery holders drawn across groups. |

Ostrom and colleagues later argued these principles hold for groups of
all kinds, not only fisheries and forests, because they follow from how
cooperation evolves ([Wilson, Ostrom & Cox 2013](https://davidsloanwilson.world/papers-and-studies/generalizing-the-core-design-principles-for-the-efficacy-of-groups/)).

**Digital and knowledge commons.**

- **Hess & Ostrom**, *Understanding Knowledge as a Commons* (MIT Press,
  2007; [overview](https://wiki.p2pfoundation.net/Understanding_Knowledge_as_a_Commons)):
  knowledge commons face *enclosure* (a provider taking control) more than
  overuse. For Casper, the standing rule that Casper never becomes the only
  place data or control lives is the anti-enclosure rule.
- **Governing Knowledge Commons** (Frischmann, Madison & Strandburg,
  2014; [SSRN](https://papers.ssrn.com/abstract=2490622)) adapts Ostrom's
  framework to shared information and finds communities, not resources,
  are the unit of analysis: rules grow out of who the members are.
- **Wikipedia** governance decentralized over time: policy-making moved to
  local groups (WikiProjects) that write and enforce their own norms, an
  Ostrom pattern ([Forte, Larco & Bruckman 2009](https://jmis-web.org/contributors/618),
  *JMIS* 26(1)).
- **Open source.** Schweik & English's study of ~170,000 projects found
  success tied to a clear vision, real usefulness and leaders who lead by
  doing; heavy formal governance mattered less than expected
  ([*Internet Success*, 2012](https://www.bollier.org/blog/why-do-some-software-commons-succeed-and-others-fail)).
  Python's founder stepped down as "benevolent dictator" in 2018 after
  finding the sole-arbiter role too stressful, and the community replaced
  it with an elected five-person council
  ([LWN](https://lwn.net/Articles/768791)): single-organizer groups need a
  succession path.
- **Platform cooperatives** (Scholz, 2016;
  [summary](https://sharingcitiesalliance.knowledgeowl.com/help/platform-cooperativism)):
  shared ownership, democratic governance and data portability as the
  answer to extractive platforms; aligned with goal 2.
- **A warning:** groups that claim to have no structure still have one, an
  invisible and unaccountable one ([Freeman, "The Tyranny of
  Structurelessness"](https://libcom.org/article/tyranny-structurelessness-jo-freeman)).
  Every Casper group should have an explicit, visible decision rule, even
  if it's "Riley decides".

## 3. Group governance patterns

Each pattern below could be a Casper group policy: *who may change
membership (and the group's rules), and how they agree.*

| Pattern | How it works | As a Casper group policy |
|---|---|---|
| **Single organizer** (benevolent dictator) | One person decides; others leave if they disagree. Fast, clear, fragile (Python above). | "Riley decides." The organizer's intent alone adds or removes members. Needs a named successor or a fallback so the group survives the organizer leaving. Today's `groups.owner_user_id` is this. |
| **Consent** (sociocracy) | A proposal passes unless someone has a reasoned objection; the question is "can you live with it?", not "do you prefer it?" ([Sociocracy For All](https://sociocracyforall.org/consent-decision-making/)) | "Anyone may propose; it passes if no member objects within N days." Good for small circles of equals. An objection opens a conversation, never a silent veto. |
| **Quorum / majority vote** | A set number or share of members must approve. Loomio offers proposal templates for advice, consent and consensus, plus polls ([Loomio](https://help.loomio.com/en/user_manual/polls/intro_to_decisions)). | "Any 2 of the 3 stewards approve." Each steward's yes is an intent item on Casper's own surface, so it fits the generalized `pending_approvals` naturally. |
| **Lazy consensus** (Apache) | "Silence gives assent": announce, wait (typically 72 hours), proceed unless someone objects ([Apache](https://apache.org/foundation/voting)). | "Member invites go through after 3 days unless someone objects." Low friction for routine changes. Caution: silence fills nothing in our gate model, so silence may only approve what members agreed up front to approve this way. |
| **Invitation with a sponsor** | Members invite; the invite tree is public, and an inviter can lose invite rights if invitees abuse the site ([Lobsters](https://lobste.rs/about)). Hacker News lets established users *vouch* to restore a wrongly killed post ([HN FAQ](https://news.ycombinator.com/newsfaq.html)). Early Gmail spread by scarce invites from existing users ([history](https://en.wikipedia.org/wiki/History_of_Gmail)). | "Any member may invite; the inviter is the newcomer's sponsor." Casper already works this way (invites name an inviter). The sponsor is who the newcomer turns to first, a gentle accountability, not liability. |

These combine. A plausible default: **single organizer for groups of two
to five, with any member able to invite as sponsor; consent ("no
objection in 3 days") as an opt-in once a group has several active
stewards.**

**The key separation.** Joining a group never commits a member's
resources. Membership says who may *ask* and who may *receive*; each
member's offering ("20 GB on this Mac for Mutual Aid") stays their own
intent item, filled only by them through a trusted surface
([gates.md](../gates.md)). So a quorum can admit someone, but it can't
spend anyone's disk. Likewise, removing a member removes their access to
the group's pool, while Casper re-places any copies they held and their
own folders keep their other holders.

## 4. Design recommendations

1. **Thanks is a person-to-person note tied to a moment.** Casper offers
   the moment (a restore succeeded, a first copy completed, a year of
   keeping), the person writes the words, and it goes privately to the
   giver first.
2. **The recipient decides whether thanks is shown to the group.** Shown
   thanks appears as quoted notes on the giver's page, never as a number.
3. **No counts anywhere**: no thank tallies, leaderboards, streaks,
   "top keepers" or ratings, and no mutual references. Giving is shown
   as plain facts ("keeps copies for Sam and Mira; holds 3 recovery
   shares"), not scores.
4. **Standing never buys power.** How much someone gives never grants
   rights to decide for others; decision rights come only from the group's
   stated rule.
5. **Every group states its decision rule in one line**, visible to all
   members ("Riley decides"; "any 2 of 3 stewards"; "no objection within
   3 days"). Default: single organizer, with a named successor.
6. **Membership approvals are gates.** Each approver's yes is an intent
   item on Casper's surface (a `pending_approvals` row); lazy consensus is
   allowed only for changes the group opted into beforehand.
7. **Joining never spends resources.** Offerings stay per-member intent;
   group membership only makes someone eligible to ask and receive.
8. **Graduated, impersonal responses**: a private nudge, then quiet
   re-placement of copies, then a group decision. Leaving is always easy
   and blameless.
9. **Need is shown softly at group level** ("Mutual Aid could use one more
   holder on another network"), inviting help without naming who owes,
   and turning a time of need into an invitation to give.
10. **Agents never perform gratitude.** An agent may remind a person that
    a thank-you moment happened; it never writes, sends or shows thanks for
    them.

## 5. Questions for the Principal

1. **Where is giving visible?** Only within the groups a person shares
   with the viewer, or across all of Casper (a public profile)?
2. **Default governance for a new group:** single organizer with a named
   successor, or consent among all members from the start?
3. **Sponsor accountability:** should an inviter bear any consequence when
   someone they invited misbehaves (as on Lobsters), or is sponsorship
   purely welcoming?
4. **Shown thanks:** should Casper allow thanks to be shown beyond the
   group it arose in (for example, carried along when the giver joins a new
   group), or does it stay where it was given?
5. **Leaving and the organizer:** if a single organizer leaves or goes
   silent for long, does the group pass to the successor automatically
   after a waiting period, or does it need the members' consent?
