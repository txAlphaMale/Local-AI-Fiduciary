# Estate Planning Mechanics — Wills, Trusts, POA & Healthcare Directives (Reference)

_Corpus doc for the Fiduciary knowledge RAG. This is the FOUNDATIONAL/MECHANICS layer — what these
documents actually do, day-to-day, in plain language. It is deliberately separate from
`Estate-Generational-Reference.md`, which covers the advanced ESTATE-TAX strategy tier (basis
management, SLAT/IDGT/GRAT, GST allocation) for households approaching the federal exemption.
Most people need THIS layer first; the tax-strategy layer only becomes relevant once an estate's
projected value crosses the (growing) exemption. Researched 2026-07 from public
attorney/bar-association/consumer-finance sources (see Sources). Educational reference only — not
legal advice; every document below requires proper execution (witnesses, notarization, state-
specific formalities) that a licensed estate-planning attorney should handle for anything beyond
the simplest estate.

## The four foundational documents

Every estate plan rests on the same four documents, regardless of net worth or whether a trust is
involved. Missing or stale ones are the most common gap this app's own document-currency tracker
(PLAN → CONTINUITY) flags:

1. **Will** — names who inherits what, names an executor to administer the estate, and (critically,
   if there are minor children) names a guardian. Without one, state INTESTACY law decides who
   inherits, in a fixed order that may not match your wishes, and a court picks the guardian.
2. **Durable power of attorney (financial/legal)** — appoints an AGENT to make financial and legal
   decisions (pay bills, manage accounts, handle real estate, file taxes) if you become
   incapacitated. "Durable" specifically means it stays in effect through incapacity — an ordinary
   (non-durable) POA does not. Without one, a family member has no legal authority to act on your
   behalf financially and must petition a court for guardianship/conservatorship — slower, public,
   and costlier than naming an agent in advance.
3. **Durable power of attorney for healthcare (medical POA / healthcare proxy)** — appoints an agent
   to make MEDICAL decisions on your behalf if you can't communicate them yourself (surgery
   consent, treatment choices, end-of-life care), guided by your known wishes. This is a DIFFERENT
   document from #2 above — a financial POA agent generally has no authority over medical
   decisions, and vice versa, unless the document is drafted to cover both.
4. **Advance directive / living will** — a WRITTEN statement of your own medical wishes in specific
   situations (e.g., whether you want life-sustaining treatment, artificial nutrition, or CPR if
   there's no reasonable chance of recovery), rather than delegating the decision to a person. The
   common, practical setup is BOTH #3 and #4 together: the directive states your wishes, and the
   healthcare agent is the person empowered to interpret and apply them when a decision the
   document didn't anticipate comes up. A HIPAA authorization (allowing named people to even SEE
   your medical records) is a related, easily-forgotten fifth piece — without it, an agent may be
   unable to get the information needed to make an informed decision.

**The core confusion to watch for**: "power of attorney" alone is ambiguous — always clarify
FINANCIAL vs. HEALTHCARE, since they're separate documents naming (often) different agents with
non-overlapping authority.

## Will vs. revocable living trust vs. both

These solve overlapping but distinct problems, and the choice is not either/or in practice:

- **Will alone**: simplest, cheapest to create, but everything it covers goes through PROBATE (see
  below) — a public, court-supervised process. A will also does nothing for INCAPACITY — if you
  become unable to manage your affairs while alive, a will (which only takes effect at death) is
  irrelevant; that's what the durable POA above is for.
- **Revocable living trust**: you (as grantor) transfer assets into a trust you control during your
  lifetime (often as your own trustee), naming a successor trustee to take over on your death or
  incapacity. Assets titled IN the trust bypass probate entirely and pass privately per the trust's
  terms, AND the successor trustee can step in immediately if you're incapacitated — without a
  court guardianship proceeding. You retain full control and can amend or revoke it any time while
  competent (hence "revocable") — it offers NO asset protection or estate-tax benefit while
  revocable, since you're still treated as the owner for both creditor and tax purposes.
- **Why you still need a will if you have a trust — the pour-over will**: a trust only controls
  assets actually TITLED into it. It's easy to acquire a new asset, forget to retitle something, or
  simply never get around to funding everything. A "pour-over will" is a will whose only real job
  is to catch anything left outside the trust at death and direct it INTO the trust — so the trust's
  instructions still ultimately govern everything, but whatever wasn't already in the trust must
  first pass through probate to get there (a pour-over will doesn't avoid probate for un-funded
  assets; it just makes sure they end up in the right place once probate is done). A trust-based
  plan is really a PAIR of documents working together, not a will replacement.

## Funding a trust — the step people skip

Creating the trust document is only half the work; an UNFUNDED trust (one that exists on paper but
holds no retitled assets) provides none of its probate-avoidance benefit — everything still sitting
in your individual name at death still goes through probate, pour-over will or not. "Funding"
means:

- **Real estate**: a new deed must be signed and RECORDED transferring title from you individually
  to you (or the current trustee) "as trustee of [Trust Name]." This is a real recording-office
  transaction, not a paperwork formality inside the trust document itself.
- **Bank and brokerage accounts**: retitled into the trust's name at the institution. Brokers/banks
  typically want to see the trust document (or a shorter "certificate of trust" that proves the
  trustee's authority without disclosing the trust's private terms) before retitling.
- **Beneficiary-designation assets — usually NOT retitled into the trust**: retirement accounts
  (401(k), IRA) and life insurance are normally left in your own name and instead have their
  BENEFICIARY DESIGNATION updated (sometimes naming the trust as beneficiary, sometimes naming
  people directly — a fact-specific call with tax implications, especially for retirement accounts
  given the SECURE Act's post-death distribution rules). This is the same "beneficiary designations
  override the will/trust document" hygiene point already tracked elsewhere in this corpus and in
  the app's own PLAN → CONTINUITY document-currency check.
- **Practical tell**: if you set up a trust years ago and never went through this retitling process
  for a NEW account or property since, that new asset is almost certainly NOT in the trust yet.

## Probate — what it actually is, and why avoiding it is often the point

Probate is the court-supervised process of validating a will (or applying intestacy law if there
isn't one), paying the decedent's debts, and formally transferring the remaining assets to heirs.
It is public record, and — this is the part that motivates most trust planning — it takes real time
and can cost real money:

- **Timeline varies widely by state and complexity**: a simple, uncontested estate can resolve in
  roughly 6–9 months; more typical estates run 1–2 years; a national study found an average around
  20 months; contested estates can run well beyond that. There is no single national number —
  timelines are set by STATE law (creditor-claim windows, court calendars, whether the state allows
  a streamlined/"independent" administration or requires full court supervision of every step).
- **How assets avoid probate regardless of a trust**: assets with a beneficiary designation (401k,
  IRA, life insurance), assets held in joint tenancy with right of survivorship, and — where state
  law allows it — a Transfer-on-Death (TOD) deed for real estate or TOD/POD registration on bank and
  brokerage accounts all pass OUTSIDE probate automatically, with no trust required. A funded
  revocable trust is the broadest tool (it can cover everything at once), but for a simpler estate,
  beneficiary designations plus a TOD deed can accomplish much of the same probate-avoidance
  without the cost/complexity of a trust.
- **Texas-specific fast paths** (this household's state of residence per PLAN → ASSUMPTIONS — verify
  your own state's equivalents if that changes): Texas favors "independent administration," which
  gives the named executor broad authority to act WITHOUT seeking court approval for each step —
  markedly faster and cheaper than a fully court-supervised ("dependent") administration. Two
  narrower shortcuts: **muniment of title** admits a will to probate solely to clear title to real
  estate, with no executor appointed at all — available only when there are no unpaid debts
  requiring administration, and often completed in 4–6 weeks; a **small estate affidavit** (Texas
  Estates Code §205.001) lets heirs collect a decedent's assets with NO court proceeding at all when
  non-homestead assets are under $75,000 and debts don't exceed assets — but it does not work for
  real property (beyond a narrow homestead-to-spouse exception) and is unavailable if the decedent
  left a will (muniment of title or full probate applies instead in that case).

## DIY vs. attorney — an honest tradeoff, not a verdict

- **Cost gap is real**: online/DIY will services commonly run in the ~$150–250 range vs. roughly
  $800–1,500+ for attorney-drafted documents (more for a funded trust or a complex estate) —
  multiply for both spouses and it's a material difference.
- **What DIY tends to get wrong**: one-size-fits-all templates handle a simple estate (no minor
  children, no blended family, no business interests, modest/simple asset mix, one state involved)
  reasonably well. They're more likely to fail on EXECUTION formalities that vary by state (witness
  count, notarization, self-proving affidavits) — an improperly executed document can be thrown out
  entirely, which nobody discovers until it's needed and it's too late to fix. They also don't catch
  fact-specific issues: blended families, a special-needs beneficiary, business succession, a
  beneficiary designation that conflicts with the will/trust, or state-specific quirks (like Texas's
  community-property rules already covered in `Estate-Generational-Reference.md`).
  A trust in particular is only as good as its FUNDING — a DIY trust that never gets assets properly
  retitled provides none of its benefit, and that step is easy to get wrong without guidance.
- **Reasonable read**: DIY is a defensible starting point for the simplest single-person or young-
  couple estates with modest, simple assets and no trust — but a trust-based plan, any blended-
  family or minor-children situation, or an estate with meaningfully complex or high-value assets
  is exactly where an attorney's fact-specific judgment (and correct execution) earns its cost. This
  app's own framing already reflects this: it can EXPLAIN what these documents do and roughly when
  each becomes relevant, but it does not draft or replace legal documents.

## Scope boundary (repeating deliberately)

Nothing in this document is a substitute for a licensed estate-planning attorney, and nothing here
should be read as advice about YOUR specific situation. Execution requirements (witnesses,
notarization, self-proving affidavits), whether a trust makes sense for your assets and goals, and
how to coordinate beneficiary designations with a will or trust are all fact-specific and state-
specific calls. Use this doc to walk INTO that attorney conversation informed — knowing what to ask
for and what each document is supposed to do — not to skip the conversation.

## Sources (researched July 2026)
Illinois State Bar Association, "Your Guide to a Living Trust" (isba.org) · American Bar Association
consumer estate-planning guidance (definitions of revocable/irrevocable trust) · WealthCounsel,
"Essentials for Funding a Revocable Living Trust" · Trust & Will / FreeWill / Edelman Financial
Engines, pour-over will explainers · FindLaw / Super Lawyers, durable POA vs. advance directive
explainers · LegalZoom / Frame & Frame / Prosperity Law Firm, DIY-vs-attorney cost and risk
comparisons (2026) · FindLaw / LegalMatch / Trust & Will 2024 probate-timeline study, probate
process and timeline · Texas Law Help ("Probate Court Basics"), Texas Probate Attorney and The
Private Client Law Firm, muniment of title and small estate affidavit mechanics · Texas Estates
Code §205.001 (small estate affidavit) and independent-administration provisions.
