"""
EXCERPT of the safety layer's test suite: 29 of 119 tests, the classes that prove the
mechanism (baseline, recipient hygiene, rate limits, circuit breaker + canary, fail-closed,
HMAC clearance, append-only ledger). The classes not included test product-specific policy
lists and identity rules and stay private. Names of the product, the operator and the brand
voice are generalized. This excerpt documents behaviour; it is not runnable without the
private package it imports.
"""
#!/usr/bin/env python3
"""
Guardian contract tests.

Run:  python -m unittest discover -s tests -v

stdlib unittest, not pytest — the VPS cron environment has no pytest and adding a
dependency for a safety system that must run everywhere is the wrong trade.

The tests that matter most are the NEGATIVE ones: not "a good message passes" but
"a bad message cannot get through, and a broken checker blocks rather than
shrugs". Under full autonomy nobody is downstream to catch a false pass.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
REAL_CONTENT = REPO / "hvac-ai-automation-core" / "frontend" / "src" / "content" / "content-guardrails.json"
REAL_GUARDIAN = REPO / "guardian" / "guardian-policy.json"

FOOTER = "<street>, <town>, CT <zip>"

CLEAN_EMAIL = f"""Hi Mike,

Saw your shop has been busy this summer. Quick question — when a call comes in
after hours, what happens to it right now?

We built something that answers those and books the job. Happy to show you if
useful, no pressure either way.

Thanks,
The the platform Team

Reply with unsubscribe and we will not write again.
{FOOTER}
"""


def _mkpolicy(tmp: Path, mutate=None) -> Path:
    """Build an isolated policy dir so tests never bind to the real ruleset."""
    shutil.copy(REAL_CONTENT, tmp / "content-guardrails.json")
    g = json.loads(REAL_GUARDIAN.read_text(encoding="utf-8"))
    import hashlib
    g["pinned_content_sha256"] = hashlib.sha256(
        (tmp / "content-guardrails.json").read_bytes()).hexdigest()
    if mutate:
        mutate(g)
    (tmp / "guardian-policy.json").write_text(json.dumps(g), encoding="utf-8")
    return tmp


class GuardianTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="guardian-test-"))
        _mkpolicy(self.tmp)
        os.environ["GUARDIAN_POLICY_DIR"] = str(self.tmp)
        os.environ["GUARDIAN_DB"] = str(self.tmp / "ledger.sqlite")
        os.environ["GUARDIAN_SECRET"] = "x" * 48
        from guardian import ledger, policy
        self.policy = policy.load()
        self.con = ledger.connect()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, **kw):
        from guardian.gates import Request
        base = dict(channel="email", body=CLEAN_EMAIL, subject="quick question",
                    recipient="mike@example.com", from_addr="info@example.com",
                    template_id="t1", trade="hvac", state="MA", actor="test")
        base.update(kw)
        return Request(**base)

    def gates_for(self, **kw):
        from guardian import gates
        return gates.evaluate(self.req(**kw), self.policy, self.con)

    def seed_canary(self, **kw):
        """Record a PASS canary for this exact content, then evaluate.

        Used ONLY by tests asserting a message should pass. The canary gate is
        content-bound on purpose — any edit revokes its PASS — so without this
        every positive fixture would trip canary and mask the gate under test.
        The canary behaviour itself is covered by TestBreakerAndCanary."""
        r = self.req(**kw)
        self.con.execute("INSERT INTO canary (ts,template_id,content_sha256,status)"
                         " VALUES (?,?,?,'PASS')",
                         (datetime.now(timezone.utc).isoformat(), r.template_id,
                          r.template_sha256()))
        self.con.commit()
        return self.gates_for(**kw)

    def assertBlockedBy(self, verdict, gate_name, msg=""):
        self.assertFalse(verdict.allowed, f"expected BLOCK, got allow. {msg}")
        hit = [v.gate for v in verdict.violations]
        self.assertIn(gate_name, hit, f"expected gate {gate_name!r}, got {hit}: "
                                      f"{verdict.reasons()}")


# ---------------------------------------------------------------------------
class TestBaseline(GuardianTestCase):
    def test_clean_email_passes_all_deterministic_gates(self):
        """If this fails, no compliant message could ever be sent — the gates
        would be a denial-of-service on ourselves rather than a guardrail."""
        v = self.seed_canary()
        self.assertTrue(v.allowed, f"clean email was blocked: {v.reasons()}")

    def test_canary_seeded_content_allows_send(self):
        from guardian import ledger
        r = self.req()
        ledger.record  # noqa
        self.con.execute("INSERT INTO canary (ts,template_id,content_sha256,status)"
                         " VALUES (?,?,?,'PASS')",
                         (datetime.now(timezone.utc).isoformat(), "t1", r.content_sha256()))
        self.con.commit()
        self.assertTrue(self.gates_for().allowed)


class TestRecipient(GuardianTestCase):
    def test_blocks_malformed(self):
        for bad in ("notanemail", "a@b", "@example.com", "mike@", "mike @example.com"):
            self.assertBlockedBy(self.gates_for(recipient=bad), "recipient", f"addr={bad}")

    def test_blocks_multiple_recipients(self):
        self.assertBlockedBy(self.gates_for(recipient="a@x.com,b@y.com"), "recipient")

    def test_blocks_suppressed_address(self):
        from guardian import ledger
        ledger.suppress(self.con, "mike@example.com", "unsubscribe")
        self.assertBlockedBy(self.gates_for(), "recipient")

    def test_bounce_auto_suppresses(self):
        from guardian import ledger
        ledger.record_feedback(self.con, "bounce", "mike@example.com")
        self.assertTrue(ledger.is_suppressed(self.con, "mike@example.com"))


class TestRateLimits(GuardianTestCase):
    def _sent(self, recipient, ts=None, channel="email"):
        ts = ts or datetime.now(timezone.utc)
        self.con.execute(
            "INSERT INTO outbound (ts,channel,recipient,recipient_domain,content_sha256,"
            "decision,policy_id) VALUES (?,?,?,?,?,'SENT','p')",
            (ts.isoformat(timespec="seconds"), channel, recipient,
             recipient.rsplit("@", 1)[-1], "sha"))
        self.con.commit()

    def test_blocks_at_daily_cap(self):
        cap = self.policy.limits("email")["per_day"]
        for i in range(cap):
            self._sent(f"u{i}@d{i}.com", datetime.now(timezone.utc) - timedelta(minutes=90 + i))
        self.assertBlockedBy(self.gates_for(), "rate")

    def test_blocks_second_contact_at_same_domain_same_day(self):
        self._sent("other@example.com", datetime.now(timezone.utc) - timedelta(hours=2))
        self.assertBlockedBy(self.gates_for(recipient="mike@example.com"), "rate")

    def test_blocks_burst(self):
        self._sent("someone@elsewhere.com", datetime.now(timezone.utc) - timedelta(seconds=5))
        self.assertBlockedBy(self.gates_for(recipient="mike@example.com"), "rate")

    def test_blocks_recontact_inside_cooldown(self):
        self._sent("mike@example.com", datetime.now(timezone.utc) - timedelta(days=10))
        self.assertBlockedBy(self.gates_for(sequence_step=1), "rate")

    def test_blocks_sequence_step_too_soon(self):
        self._sent("mike@example.com", datetime.now(timezone.utc) - timedelta(hours=6))
        self.assertBlockedBy(self.gates_for(sequence_step=2), "rate")

    def test_blocks_beyond_max_sequence_steps(self):
        for d in (30, 20, 10):
            self._sent("mike@example.com", datetime.now(timezone.utc) - timedelta(days=d))
        self.assertBlockedBy(self.gates_for(sequence_step=4), "rate")


class TestBreakerAndCanary(GuardianTestCase):
    def test_tripped_breaker_blocks_everything(self):
        from guardian import ledger
        ledger.trip(self.con, "test trip")
        self.assertBlockedBy(self.gates_for(), "breaker")

    def test_breaker_stays_tripped_until_explicitly_reset(self):
        from guardian import ledger
        ledger.trip(self.con, "boom")
        self.assertTrue(ledger.breaker_state(self.con)[0])
        # The secret must remain readable at VERIFY time too — breaker_state
        # recomputes the HMAC. Popping it early made a legitimate reset
        # unrecognisable and the breaker permanently un-clearable.
        os.environ["GUARDIAN_RESET_SECRET"] = "reset-me-" + "z" * 24
        try:
            ledger.reset(self.con, "investigated", "raj",
                         token=os.environ["GUARDIAN_RESET_SECRET"])
            self.assertFalse(ledger.breaker_state(self.con)[0])
        finally:
            os.environ.pop("GUARDIAN_RESET_SECRET", None)

    def test_the_agent_that_tripped_the_breaker_cannot_clear_it(self):
        """The review tripped the breaker and then cleared it in the same process.
        The reset secret is deliberately separate from GUARDIAN_SECRET and is not
        needed to send, so it need not exist on the sending host at all."""
        from guardian import ledger
        ledger.trip(self.con, "boom")
        with self.assertRaises(ledger.LedgerError):
            ledger.reset(self.con, "nothing to see here", "agent")
        os.environ["GUARDIAN_RESET_SECRET"] = "correct-horse-" + "q" * 20
        try:
            with self.assertRaises(ledger.LedgerError):
                ledger.reset(self.con, "guessing", "agent", token="wrong")
        finally:
            os.environ.pop("GUARDIAN_RESET_SECRET", None)
        self.assertTrue(ledger.breaker_state(self.con)[0], "breaker was cleared!")

    def test_canary_required_for_new_template(self):
        self.assertBlockedBy(self.gates_for(template_id="never-seen"), "canary")

    def test_canary_pass_does_not_launder_edited_content(self):
        """A PASS is bound to the exact template, so editing it revokes the PASS."""
        r = self.req()
        self.con.execute("INSERT INTO canary (ts,template_id,content_sha256,status)"
                         " VALUES (?,?,?,'PASS')",
                         (datetime.now(timezone.utc).isoformat(), "t1", r.template_sha256()))
        self.con.commit()
        self.assertTrue(self.gates_for().allowed)
        self.assertBlockedBy(self.gates_for(body=CLEAN_EMAIL + "\nOne more line."), "canary")

    def test_one_canary_covers_every_personalisation_of_its_template(self):
        """The flaw this guards against: binding the canary to the RENDERED body
        means "Hi Mike" and "Hi Dana" hash differently, so no canary would ever
        match a real personalised send and email autonomy would deadlock."""
        template = CLEAN_EMAIL.replace("Hi Mike,", "Hi {{first_name}},")
        self.con.execute(
            "INSERT INTO canary (ts,template_id,content_sha256,status) VALUES (?,?,?,'PASS')",
            (datetime.now(timezone.utc).isoformat(), "t-seq-1",
             self.req(template_body=template, template_id="t-seq-1").template_sha256()))
        self.con.commit()

        for name, addr in (("Mike", "mike@shopa.com"), ("Dana", "dana@shopb.com"),
                           ("Chris", "chris@shopc.com")):
            v = self.gates_for(template_id="t-seq-1", template_body=template,
                               body=template.replace("{{first_name}}", name),
                               recipient=addr)
            self.assertTrue(v.allowed, f"{name}: {v.reasons()}")

    def test_editing_the_template_revokes_the_canary_for_all_recipients(self):
        template = CLEAN_EMAIL.replace("Hi Mike,", "Hi {{first_name}},")
        self.con.execute(
            "INSERT INTO canary (ts,template_id,content_sha256,status) VALUES (?,?,?,'PASS')",
            (datetime.now(timezone.utc).isoformat(), "t-seq-1",
             self.req(template_body=template, template_id="t-seq-1").template_sha256()))
        self.con.commit()
        edited = template + "\nP.S. one more thing."
        self.assertBlockedBy(
            self.gates_for(template_id="t-seq-1", template_body=edited,
                           body=edited.replace("{{first_name}}", "Mike"),
                           recipient="mike@shopa.com"), "canary")


class TestFailClosed(GuardianTestCase):
    def test_tampered_content_rules_block_everything(self):
        """An agent quietly editing the rules it is judged by must halt the system."""
        from guardian.policy import PolicyError, load
        p = self.tmp / "content-guardrails.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["silaBrands"] = []                      # disable the W2 blocklist
        p.write_text(json.dumps(d), encoding="utf-8")
        with self.assertRaises(PolicyError):
            load(strict=True)

    def test_missing_required_content_key_blocks(self):
        from guardian.policy import PolicyError, load
        p = self.tmp / "content-guardrails.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        del d["silaBrands"]
        p.write_text(json.dumps(d), encoding="utf-8")
        with self.assertRaises(PolicyError):
            load(strict=False)

    def test_a_gate_that_raises_blocks_rather_than_passes(self):
        from guardian import gates
        def explode(*a, **k):
            raise RuntimeError("boom")

        # _CONTENT_GATES captured the original function objects at import time, so
        # rebinding gates.g_pricing alone would never reach evaluate(). Patch the tuple.
        orig = gates._CONTENT_GATES
        gates._CONTENT_GATES = (explode,) + orig
        try:
            v = gates.evaluate(self.req(), self.policy, self.con)
            self.assertFalse(v.allowed)
            self.assertIn("boom", v.reasons())
        finally:
            gates._CONTENT_GATES = orig

    def test_ledger_is_append_only(self):
        import sqlite3
        from guardian import ledger
        rid = ledger.record(self.con, channel="email", decision="SENT",
                            content_sha256="s", policy_id="p", recipient="a@b.com")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE outbound SET decision='BLOCKED' WHERE id=?", (rid,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("DELETE FROM outbound WHERE id=?", (rid,))


class TestClearance(GuardianTestCase):
    def test_roundtrip(self):
        from guardian import clearance
        t = clearance.mint(channel="email", recipient="Mike@Example.com",
                           content_sha256="abc", policy_id="p")
        body = clearance.verify(t, channel="email", recipient="mike@example.com",
                                content_sha256="abc")
        self.assertEqual(body["sha"], "abc")

    def test_rejects_content_swap(self):
        from guardian import clearance
        t = clearance.mint(channel="email", recipient="m@x.com",
                           content_sha256="abc", policy_id="p")
        with self.assertRaises(clearance.ClearanceError):
            clearance.verify(t, channel="email", recipient="m@x.com",
                             content_sha256="DIFFERENT")

    def test_rejects_recipient_swap(self):
        from guardian import clearance
        t = clearance.mint(channel="email", recipient="m@x.com",
                           content_sha256="abc", policy_id="p")
        with self.assertRaises(clearance.ClearanceError):
            clearance.verify(t, channel="email", recipient="victim@y.com",
                             content_sha256="abc")

    def test_rejects_forged_signature(self):
        from guardian import clearance
        t = clearance.mint(channel="email", recipient="m@x.com",
                           content_sha256="abc", policy_id="p")
        forged = t.rsplit(".", 1)[0] + ".AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        with self.assertRaises(clearance.ClearanceError):
            clearance.verify(forged, channel="email", recipient="m@x.com",
                             content_sha256="abc")

    def test_rejects_expired(self):
        from guardian import clearance
        t = clearance.mint(channel="email", recipient="m@x.com",
                           content_sha256="abc", policy_id="p", ttl_seconds=-1)
        with self.assertRaises(clearance.ClearanceError):
            clearance.verify(t, channel="email", recipient="m@x.com",
                             content_sha256="abc")

    def test_weak_secret_refuses_to_mint(self):
        from guardian import clearance
        os.environ["GUARDIAN_SECRET"] = "short"
        try:
            with self.assertRaises(clearance.ClearanceError):
                clearance.mint(channel="email", recipient="m@x.com",
                               content_sha256="abc", policy_id="p")
        finally:
            os.environ["GUARDIAN_SECRET"] = "x" * 48


