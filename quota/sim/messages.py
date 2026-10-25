"""The sales email and Slack around a world's deals (ELASTIC_TASKS E1), with 12 planted off-contract promises
and 12 decoys, and an answer key. Templates only, no model calls: the same seed gives byte-identical files.

Emails in a thread quote the message they answer (`> ...`), so a promise made across two messages is one
searchable document. Promises go on deals no planted case uses, each on a deal whose signed contract lacks the
matching clause; the decoys that say "it's in the contract" go on the two deals whose contracts have it.
"""

import json
import random
from datetime import datetime, timedelta, timezone

from quota import config
from quota.ledger import queries

START, END = "2026-01-02T16:00:00Z", "2026-06-28T23:00:00Z"     # the corpus covers Jan-Jun 2026
FIRST = ["Ana", "Ben", "Chloe", "Dev", "Elena", "Femi", "Grace", "Hugo", "Ines", "Jamal", "Keiko", "Lars", "Maya",
         "Nico", "Olga", "Priyanka", "Quinn", "Rosa", "Sam", "Tomas", "Uli", "Vera", "Wes", "Xin", "Yusuf", "Zoe"]

# ---------- the planted promises and decoys (texts verbatim from ELASTIC_TASKS E1) ----------

PROMISES = [
    ("P01", "early_exit", "keyword", "en", "Confirming what we discussed: you can terminate after 60 days with a full "
                                           "refund if adoption stalls."),
    ("P02", "early_exit", "paraphrase", "en", "If the rollout slips past June your team can walk away, no charge. "
                                              "I'll sort it with legal later."),
    ("P03", "early_exit", "paraphrase", "en", "Don't stress about year two. If it isn't working by Q3, we'll just "
                                              "let you out."),
    ("P04", "free_period", "paraphrase", "en", "I'll get you three months on the house after go-live. Just don't "
                                               "put it in the order form."),
    ("P05", "price_lock", "paraphrase", "en", "Whatever happens at renewal, you'll stay at this year's number. You "
                                              "have my word."),
    ("P06", "payment_terms", "paraphrase", "en", "Pay the first invoice whenever finance gets to it. 120 days is "
                                                 "fine on our side."),
    ("P07", "free_addon", "paraphrase", "en", "Throw in the analytics module for free, we'll just not bill it."),
    ("P08", "early_exit", "multilingual", "es", "Si no funciona en 60 días, cancelan sin costo. Lo dejamos entre "
                                                "nosotros."),
    ("P09", "early_exit", "thread", "en", "Yes. Consider it done."),
    ("P10", "early_exit", "internal", "en", "Told Northwind they can cancel anytime in the first quarter, needed it "
                                            "to close."),
    ("P11", "credit", "keyword", "en", "We'll credit you $15,000 against the first invoice as discussed."),
    ("P12", "price_lock", "paraphrase", "en", "Your price won't move for three years even though the paper says "
                                              "annual uplift."),
]
P09_QUESTION = "Can we get out if the integration isn't live by September?"
DECOYS = [
    ("D01", "early_exit", "en", "Our standard order form already includes termination for convenience in the first "
                                "90 days.", "in the contract"),
    ("D02", "free_period", "en", "I can't offer free months; that would need a signed amendment.", "refusal"),
    ("D03", "early_exit", "en", "Let's ask legal whether an exit clause is possible.", "proposal"),
    ("D04", "price_lock", "en", "Your renewal price is set by the 5% cap in section 7.", "in the contract"),
    ("D05", "free_addon", "en", "We'll walk you through the analytics module in a free demo.", "demo"),
    ("D06", "payment_terms", "en", "Can you pay the invoice within 30 days?", "normal terms"),
    ("D07", "early_exit", "en", "If adoption stalls, let's set up a success plan call.", "no exit"),
    ("D08", "credit", "en", "We never waive fees without an amendment signed by finance.", "refusal"),
    ("D09", "early_exit", "en", "Cancellation terms are in section 9 of the MSA.", "pointer"),
    ("D10", "early_exit", "es", "Los términos de cancelación están en la sección 9.", "pointer, multilingual"),
    ("D11", "early_exit", "en", "Northwind asked about cancelling early; I said no.", "refusal, internal"),
    ("D12", "credit", "en", "Credits apply only per the SLA schedule in the contract.", "in the contract"),
]
INTERNAL = {"P10", "D11"}
CONTRACT_KEY = {"early_exit": "termination_for_convenience", "price_lock": "price_increase_cap"}

# ---------- noise: (subject, customer opener, rep reply, customer follow-up) and internal Slack lines ----------

THREADS = [
    ("Scheduling the kickoff", "Hi {rep}, can we find an hour for kickoff next {day}? Our IT lead {other} will join.",
     "Hi {cust}, {day} at 10am Pacific works. I'll send an invite with the agenda.", "Perfect, see you then."),
    ("Security questionnaire", "Our security team sent over their vendor questionnaire. Can you fill it in by Friday?",
     "Happy to. I'll route it to our security team and send the SOC 2 report with it.",
     "Thanks, the SOC 2 report is what they wanted most."),
    ("Redlines on the MSA", "Legal has a few redlines on the limitation of liability section. Attached.",
     "Thanks {cust}. I've passed them to our counsel and will come back with comments this week.",
     "Great, no rush past Thursday."),
    ("Onboarding plan", "What does onboarding look like for a team of our size?",
     "We usually run four weekly sessions, then hand over to your admin. I'll share the plan.",
     "Four sessions sounds right for us."),
    ("Invoice copy", "Finance asked for a PDF copy of the invoice for {account}.",
     "Attached. Let me know if they need it addressed differently.", "That's the one, thanks."),
    ("Pricing question", "Is the price per seat or per workspace?",
     "It's per seat, billed annually. The quote lists the seat count you signed for.",
     "Got it, per seat it is."),
    ("SSO setup", "We're ready to turn on SSO. Which identity providers do you support?",
     "Okta, Azure AD and Google. I'll send the setup guide for Okta since that's what you use.",
     "Okta guide received, thanks."),
    ("Data migration", "How long does it usually take to migrate two years of history?",
     "About two weeks for that volume. Our migration team will scope it on a call.",
     "Two weeks works with our timeline."),
    ("Training sessions", "Can we record the training sessions for people who miss them?",
     "Yes, we record every session and share the links afterwards.", "Perfect."),
    ("Renewal timeline", "When would renewal paperwork usually start?",
     "About 60 days before the term ends. I'll put a reminder in for both of us.", "Sounds good."),
    ("Usage report", "Could you send last month's usage report?",
     "Attached. Adoption is up about 18% since the second training session.", "Nice, I'll share it internally."),
    ("Procurement portal", "Procurement needs you registered in our supplier portal.",
     "Done, we registered this morning. Our tax forms are uploaded too.", "Thanks for moving quickly."),
    ("Support hours", "What are your support hours for the EU team?",
     "Support runs 24/5 with weekend coverage for priority issues.", "That covers us."),
    ("API limits", "What are the API rate limits on our plan?",
     "1,000 requests a minute per workspace. Higher limits are available if you need them later.",
     "That's plenty for now."),
    ("Admin roles", "Can we have more than one admin?", "Yes, as many as you like. I'll show you the roles page.",
     "Great."),
    ("Status page", "Is there a status page we can subscribe to?", "Yes, status.example.com. You can subscribe by email.",
     "Subscribed."),
    ("Quarterly review", "Can we set up a quarterly business review in {month}?",
     "Absolutely. I'll bring usage trends and the roadmap.", "Looking forward to it."),
    ("New team members", "We're adding three people to the pilot team. How do we add seats?",
     "Your admin can add them from the billing page; the seats are prorated on the next invoice.",
     "Understood, we'll add them this week."),
    ("Integration with our CRM", "Does the integration sync both ways with our CRM?",
     "It does, every 15 minutes. Field mapping is configurable.", "Two-way sync is what we needed."),
    ("Executive sponsor", "Our COO would like to meet your leadership.",
     "Of course. I'll ask our VP to join the next call.", "Thanks, {day} would be ideal."),
    ("Purchase order", "Our PO number is PO-{n}. Please reference it on invoices.",
     "Noted, it will appear on every invoice from now on.", "Thank you."),
    ("Data residency", "Can our data stay in the EU region?", "Yes, we host EU customers in Frankfurt.",
     "Perfect, that answers legal's question."),
    ("Uptime SLA", "What uptime do you commit to?", "99.9% monthly, as written in the SLA schedule.",
     "Good, legal has that schedule."),
    ("Reference call", "Would one of your customers in our industry talk to us?",
     "Yes, I'll set up a reference call with a customer in a similar industry.", "That would help a lot."),
    ("Sandbox environment", "Is there a sandbox for testing?", "Yes, every workspace gets a sandbox. I'll enable it.",
     "Enabled, thanks."),
    ("Accessibility", "Do you have a VPAT?", "Yes, attached is our latest VPAT.", "Received."),
    ("Mobile app", "Is there a mobile app for field staff?", "Yes, iOS and Android, included with every seat.",
     "Great news for the field team."),
    ("Audit logs", "How long are audit logs kept?", "Twelve months on your plan, exportable anytime.",
     "That meets our policy."),
    ("Dashboard question", "The dashboard shows last week's numbers. Is there a delay?",
     "Dashboards refresh hourly; I'll check why yours is behind.", "Thanks for looking into it."),
    ("Billing contact", "Please send invoices to ap@{deal}.example from now on.",
     "Updated. Future invoices go to that address.", "Thanks."),
    ("Holiday coverage", "Will someone be reachable over the holidays?",
     "Yes, support is staffed and I'll share an on-call contact.", "Appreciated."),
    ("Feature request", "We'd love bulk editing in the admin console.",
     "I've logged it with product and will let you know when it's on the roadmap.", "Thanks for passing it on."),
    ("Contract signature", "The signed order form is attached.",
     "Thank you, {cust}. Countersignature will follow today.", "Great."),
]
SLACK = [
    "Kickoff with {account} is booked for {day}.", "{account} security review is with our team, due Friday.",
    "Legal is looking at {account}'s redlines.", "{account} asked about SSO, sent them the Okta guide.",
    "{account} wants a QBR in {month}.", "Usage at {account} is up since training.",
    "Anyone free to cover {account}'s training on {day}?", "{account} added three seats.",
    "Invoice copy sent to {account} finance.", "{account} procurement registration done.",
    "{account} asked for a reference call, can someone set one up?", "Updated the {account} opportunity notes.",
]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday"]
MONTHS = ["July", "August", "September"]


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quoted(body: str, previous: str | None) -> str:
    if previous is None:
        return body
    own = previous.split("\n\n> ")[0]
    return body + "\n\n" + "\n".join(f"> {line}" for line in own.splitlines())


class Corpus:
    def __init__(self, world, seed):
        self.ro, self.rng = world.ro, random.Random(f"messages-{seed}")
        deals = {d["id"]: d for d in queries.latest_rows(self.ro, "deals", queries.END_OF_TIME)}
        contracts = {c["deal_id"]: c for c in queries.latest_rows(self.ro, "contracts", queries.END_OF_TIME)}
        self.people = {p["id"]: p for p in queries.latest_rows(self.ro, "people", queries.END_OF_TIME)}
        self.deals = [deals[i] for i in sorted(contracts) if i in deals and deals[i]["stage"] == "closed_won"]
        self.contracts = contracts
        self.used = set(getattr(getattr(world, "fernhill", None), "used_deals", set()))
        self.messages: list[dict] = []
        self.thread = 0

    def first_name(self, principal) -> str:
        return config.display_name(principal).split()[0] if principal in self.people else principal

    def clauses(self, deal_id) -> dict:
        return queries.loads(self.contracts[deal_id]["clauses_json"])

    def add(self, kind, deal, author, to, at, subject, body, thread_id, channel=None, lang="en"):
        self.messages.append({"kind": kind, "thread_id": thread_id, "account": deal["account"], "deal_id": deal["id"],
                              "rep_id": deal["owner_id"], "author": author, "to": to, "channel": channel,
                              "sent_at": _fmt(at), "subject": subject, "body": body, "lang": lang})
        return self.messages[-1]

    def new_thread(self) -> str:
        self.thread += 1
        return f"T-{self.thread:04d}"

    def window(self, deal) -> tuple[datetime, datetime]:
        signed = _ts(self.contracts[deal["id"]]["countersigned_at"])
        return max(signed - timedelta(days=40), _ts(START)), min(signed + timedelta(days=75), _ts(END))

    def noise(self, deal) -> None:
        rng, rep = self.rng, deal["owner_id"]
        cust = rng.choice(FIRST)
        buyer = f"{cust.lower()}@{deal['id']}.example"
        start, end = self.window(deal)
        fill = {"rep": self.first_name(rep), "cust": cust, "account": deal["account"], "deal": deal["id"],
                "other": rng.choice(FIRST), "day": rng.choice(DAYS), "month": rng.choice(MONTHS),
                "n": rng.randint(10000, 99999)}
        for subject, opener, reply, follow in rng.sample(THREADS, rng.choice([3, 4])):
            thread_id, at = self.new_thread(), start + (end - start) * rng.random()
            previous = None
            turns = [(buyer, [rep], opener), (rep, [buyer], reply), (buyer, [rep], follow)][:rng.choice([2, 3, 3])]
            for n, (author, to, text) in enumerate(turns):
                body = _quoted(text.format(**fill), previous)
                previous = self.add("email", deal, author, to, at, ("Re: " if n else "") + subject, body,
                                    thread_id)["body"]
                at += timedelta(hours=rng.randint(2, 30))
        for line in rng.sample(SLACK, 3):
            self.add("slack", deal, rep, ["morgan.diaz"], start + (end - start) * rng.random(), None,
                     line.format(**fill), self.new_thread(), channel=f"#deal-{deal['id']}")

    def plant(self, key, text, deal, lang, question=None, internal=False) -> list[dict]:
        """One planted message (two for a thread), a few days before the countersignature."""
        rep, rng = deal["owner_id"], self.rng
        at = max(_ts(self.contracts[deal["id"]]["countersigned_at"]) - timedelta(days=rng.randint(2, 12),
                                                                                  hours=rng.randint(0, 6)), _ts(START))
        thread_id = self.new_thread()
        if internal:
            return [self.add("slack", deal, rep, ["morgan.diaz"], at, None, text, thread_id, channel=f"@{rep}")]
        cust = rng.choice(FIRST)
        buyer = f"{cust.lower()}@{deal['id']}.example"
        sign = self.first_name(rep)
        greet, close = ("Hola", "Saludos") if lang == "es" else ("Hi", "Best")
        if question:
            asked = self.add("email", deal, buyer, [rep], at, "Integration timeline",
                             f"Hi {sign},\n\n{question}\n\nThanks,\n{cust}", thread_id)
            reply = self.add("email", deal, rep, [buyer], at + timedelta(hours=3), "Re: Integration timeline",
                             _quoted(f"{greet} {cust},\n\n{text}\n\n{close},\n{sign}", asked["body"]), thread_id)
            return [asked, reply]
        subject = {"es": "Re: próximos pasos"}.get(lang, rng.choice(["Re: rollout plan", "Re: next steps",
                                                                     "Re: order form", "Following up"]))
        return [self.add("email", deal, rep, [buyer], at, subject, f"{greet} {cust},\n\n{text}\n\n{close},\n{sign}",
                         thread_id, lang=lang)]


def generate_messages(world, seed, plant=True) -> tuple[list[dict], list[dict]]:
    corpus = Corpus(world, seed)
    for deal in corpus.deals:
        corpus.noise(deal)
    key = []
    if plant:
        rng = corpus.rng
        free = [d for d in corpus.deals if d["id"] not in corpus.used and len(queries.loads(d["splits_json"])) == 1]
        covered = {name: [d for d in free if name in corpus.clauses(d["id"])]
                   for name in ("termination_for_convenience", "price_increase_cap")}
        plain = [d for d in free if not set(corpus.clauses(d["id"])) & set(CONTRACT_KEY.values())]
        northwind = [d for d in plain if d["account"].startswith("Northwind")]
        taken: set[str] = set()

        def take(pool):
            deal = rng.choice([d for d in pool if d["id"] not in taken])
            taken.add(deal["id"])
            return deal
        chosen = {"P10": take(northwind), "D11": take(northwind) if len(northwind) > 1 else None,
                  "D01": take(covered["termination_for_convenience"]), "D04": take(covered["price_increase_cap"])}
        for k, kind, difficulty, lang, text in PROMISES:
            deal = chosen.get(k) or take(plain)
            msgs = corpus.plant(k, text, deal, lang, question=P09_QUESTION if k == "P09" else None,
                                internal=k in INTERNAL)
            key.append({"key": k, "messages": msgs, "deal_id": deal["id"], "account": deal["account"], "type": kind,
                        "difficulty": difficulty, "lang": lang, "expected": "commitment"})
        for k, kind, lang, text, why in DECOYS:
            deal = chosen.get(k) or take(plain)
            msgs = corpus.plant(k, text, deal, lang, internal=k in INTERNAL)
            key.append({"key": k, "messages": msgs, "deal_id": deal["id"], "account": deal["account"], "type": kind,
                        "difficulty": "decoy", "lang": lang, "expected": "not", "why": why})
    corpus.messages.sort(key=lambda m: (m["sent_at"], m["thread_id"], m["author"]))
    for n, m in enumerate(corpus.messages, start=1):
        m.update(doc_id=f"MSG-{n:06d}", source_file="messages.jsonl", source_row=n)
    rows = [{k: v for k, v in r.items() if k != "messages"} | {"doc_ids": [m["doc_id"] for m in r["messages"]]}
            for r in key]
    ordered = ["doc_id", "kind", "thread_id", "account", "deal_id", "rep_id", "author", "to", "channel", "sent_at",
               "subject", "body", "lang", "source_file", "source_row"]
    return [{k: m[k] for k in ordered} for m in corpus.messages], rows


def write(messages, key, out_path) -> None:
    from pathlib import Path
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages), encoding="utf-8", newline="\n")
    (out.parent / "messages_key.json").write_text(json.dumps(key, indent=2, ensure_ascii=False) + "\n",
                                                  encoding="utf-8", newline="\n")


def world_for(name, seed, reps=20):
    """The world a corpus is written for: a Fernhill seed run to its end, or the Acme story with its cast."""
    if name == "fernhill":
        from quota.sim.generator import generate
        world = generate(seed, reps=reps, out=lambda line: None)
        world.run_until(world.end_ts)
        return world
    from quota.sim import demo_acme, demo_cast, seed_acme
    demo = demo_acme.AcmeDemo(out=lambda line: None)
    seed_acme.seed(demo.world.wp, demo.world.clock, demo.world.emit)
    demo_cast.add_cast(demo.world)
    demo.world.run_until(demo_acme.REPORT_AT)
    return demo.world


def corpus_for(name, seed) -> tuple[list[dict], list[dict]]:
    """Fernhill gets the planted promises and decoys; the Acme story gets noise only (its one side letter is
    the scenario side_letter_summit's to inject)."""
    world = world_for(name, seed)
    try:
        return generate_messages(world, seed, plant=name == "fernhill")
    finally:
        world.close()
