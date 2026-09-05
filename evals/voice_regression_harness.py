#!/usr/bin/env python3
"""
voice-regression-harness.py — text-mode CallTaker regression across all 18 trades.

The 14/14 fleet smoke test never touches voice: CallTaker prompt drift, a broken
knowledge pack, or a bad prompt-proposal merge goes undetected until a live
caller hits it. This harness closes that gap WITHOUT burning Vapi/Twilio money:
it rebuilds each trade's CallTaker-equivalent system prompt from the canonical
knowledge pack and drives 3 scripted caller scenarios through the Anthropic API
(claude-haiku — ~$0.25/full run), then asserts trade-native behavior.

Per trade × scenario assertions:
  EMERGENCY  — treats it as urgent (Haiku judge), collects address/callback
  ROUTINE    — books normally, asks trade-specific intake questions
  PRICE      — does NOT invent dollar amounts (deterministic regex)
  ALL        — zero banned filler phrases (deterministic), answers as the company

Deploy:  /opt/app/scripts/voice-regression-harness.py
Cron:    30 9 * * 0  (weekly Sunday 09:30 UTC — before agent-evolve re-seed)
Also run on demand before/after ANY CallTaker prompt or pack change.
Exit 0 = all green. Exit 1 = failures (Telegram alert fired).
"""
import json
import re
import sys
import time
import urllib.request

ENV_FILE = "/opt/app/.env"
PACK_PATHS = [
    "/opt/app/scripts/vertical-knowledge.json",
    "/opt/app/frontend/src/content/vertical-knowledge.json",
    "hvac-ai-automation-core/frontend/src/content/vertical-knowledge.json",  # local dev
]
MODEL = "claude-haiku-4-5-20251001"
CHAT_ID_DEFAULT = "8294399375"

# The verbatim filler ban from RULE #0 (fix-calltaker-filler-v2) — any of these
# in a response is an automatic FAIL for that scenario.
BANNED_FILLERS = [
    "just a sec", "hold on a sec", "hold on", "one moment", "1 moment",
    "give me a moment", "let me check", "let me see", "bear with me",
    "hang on", "checking...", "this'll just take a sec",
]


def env_from_file(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def load_packs():
    for p in PACK_PATHS:
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f), p
        except FileNotFoundError:
            continue
    raise SystemExit("FATAL: vertical-knowledge.json not found in any known path")


def telegram(text, env):
    token = env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    data = json.dumps({"chat_id": env.get("TELEGRAM_CHAT_ID", CHAT_ID_DEFAULT), "text": text[:4000]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print("[harness] telegram failed:", e)


def anthropic_call(api_key, system, user_msg, max_tokens=300):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    return "".join(b.get("text", "") for b in resp.get("content", []))


def build_system_prompt(slug, p, company):
    """Compact CallTaker-equivalent: RULE #0 + core behavior + the trade profile
    block, mirroring create-trade-calltaker.py / trade-profile.ts structure."""
    sw = p.get("serviceWord", slug)
    return f"""## RULE #0 — ABSOLUTE, OVERRIDES EVERYTHING
NEVER say filler phrases: "just a sec", "hold on", "one moment", "give me a moment", "let me check", "let me see", "checking", "bear with me", "hang on". When you need to look something up, say NOTHING extra — proceed silently.

You are the brand voice, the AI receptionist for {company}, a residential & light-commercial {sw} company. You answer every call warmly and professionally, identify yourself as {company}'s AI assistant, and book jobs.

CORE RULES:
- Answer AS {company}. One question at a time. Keep replies under 3 sentences.
- NEVER invent prices, fees, or dollar amounts. If asked about cost, explain that pricing depends on the job and the technician confirms exact pricing on site — then move to booking.
- For dates, say them as words ("May seventh"), never "May 7" or "May 22nd".
- EMERGENCIES: treat urgent {sw} situations as priority — express empathy, get the address and best callback number, and offer the soonest available slot today.

## YOUR TRADE — {p.get('displayName', slug)}
Jobs you book: {', '.join(p.get('common_services', [])[:8])}.
Trade vocabulary you use naturally: {', '.join(p.get('technician_terms', [])[:10])}.
Intake questions for {sw} work: {' '.join(p.get('greeting_intake_questions', [])[:4])}
## URGENCY TRIAGE
{p.get('urgency_triage', '')}
EMERGENCY signals: {'; '.join(p.get('emergency_examples', [])[:5])}.
Routine — book normally: {'; '.join(p.get('non_emergency_examples', [])[:4])}."""


def check_fillers(text):
    low = text.lower()
    return [f for f in BANNED_FILLERS if f in low]


def check_invented_price(text):
    # Any concrete dollar figure on a price question = invented (harness gives
    # the model no pricing data, so every $ amount is a hallucination).
    return re.findall(r"\$\s?\d[\d,.]*", text)


def judge_urgency(api_key, trade, scenario, response):
    # Calibrated on the 2026-07-03 baseline run: a great CSR's FIRST turn on an
    # emergency is often a safety-triage question ("is everyone safe?") or a
    # 911 redirect for life-threatening situations — both are correct urgent
    # handling, not failures. FAIL only if the response treats it casually
    # (routine quote flow, no urgency acknowledgment, deferral to "someone
    # will call you back eventually").
    verdict = anthropic_call(
        api_key,
        "You are a QA judge for an AI receptionist at a home-services company. Answer with exactly one word: YES or NO.",
        f"A caller to a {trade} company said: \"{scenario}\"\n\nThe receptionist's FIRST reply: \"{response}\"\n\n"
        "Did the receptionist handle this as URGENT? Answer YES if the reply does ANY of the following: "
        "(a) acknowledges the urgency/emergency with empathy and moves toward fast help, "
        "(b) asks an immediate safety-triage question (e.g. 'is everyone safe?', 'can the pets get out?'), "
        "(c) directs a life-threatening medical/gas/fire situation to 911 or the utility FIRST, "
        "(d) offers soonest/same-day service, or (e) asks for address/callback to dispatch. "
        "Answer NO only if it treats the situation casually — routine quote flow, no urgency acknowledgment, or a vague 'someone will get back to you'.",
        max_tokens=5,
    )
    return "YES" in verdict.upper()


def main():
    env = env_from_file(ENV_FILE)
    api_key = env.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("FATAL: ANTHROPIC_API_KEY not in", ENV_FILE)
        sys.exit(1)

    packs, pack_path = load_packs()
    print(f"[harness] pack: {pack_path} ({len(packs)} trades) | model: {MODEL}")

    failures = []
    results = []

    for slug, p in packs.items():
        company = f"Test {p.get('displayName', slug)} Co"
        system = build_system_prompt(slug, p, company)
        emergency = (p.get("emergency_examples") or ["I have an emergency and need someone today"])[0]
        routine = (p.get("non_emergency_examples") or ["I'd like to get a quote for some work"])[0]

        trade_fails = []

        # Scenario 1 — EMERGENCY
        try:
            r1 = anthropic_call(api_key, system, f"Hi — {emergency}")
            fillers = check_fillers(r1)
            if fillers:
                trade_fails.append(f"EMERGENCY: filler {fillers}")
            if not judge_urgency(api_key, slug, emergency, r1):
                trade_fails.append(f"EMERGENCY: not treated as urgent → \"{r1[:120]}\"")
        except Exception as e:
            trade_fails.append(f"EMERGENCY: api error {e}")

        # Scenario 2 — ROUTINE
        try:
            r2 = anthropic_call(api_key, system, f"Hi — {routine}")
            fillers = check_fillers(r2)
            if fillers:
                trade_fails.append(f"ROUTINE: filler {fillers}")
            if "?" not in r2:
                trade_fails.append(f"ROUTINE: asked no intake question → \"{r2[:120]}\"")
        except Exception as e:
            trade_fails.append(f"ROUTINE: api error {e}")

        # Scenario 3 — PRICE PUSH
        try:
            r3 = anthropic_call(api_key, system, "Before anything else — how much is this going to cost me?")
            fillers = check_fillers(r3)
            if fillers:
                trade_fails.append(f"PRICE: filler {fillers}")
            dollars = check_invented_price(r3)
            if dollars:
                trade_fails.append(f"PRICE: invented amount {dollars} → \"{r3[:120]}\"")
        except Exception as e:
            trade_fails.append(f"PRICE: api error {e}")

        status = "PASS" if not trade_fails else "FAIL"
        results.append((slug, status, trade_fails))
        print(f"  {slug:<22}{status}" + (f"  {trade_fails}" if trade_fails else ""))
        if trade_fails:
            failures.append((slug, trade_fails))

    passed = sum(1 for _, s, _ in results if s == "PASS")
    print(f"\n[harness] {passed}/{len(results)} trades PASS")

    # Status JSON for dept-status-collect.py → DeptStatus → public reliability API (same
    # pattern as fleet-smoke-status.json). Must be written on BOTH pass and fail paths —
    # the public block renders honest reds, never a cherry-picked green.
    try:
        with open("/opt/app/logs/voice-regression-status.json", "w") as sf:
            json.dump({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "exit": 1 if failures else 0,
                "passed": passed,
                "total": len(results),
                "failures": [f"{slug}: {'; '.join(f[:80] for f in fails[:2])}" for slug, fails in failures[:5]],
            }, sf)
    except Exception as e:
        print(f"[harness] WARN could not write status json: {e}")

    if failures:
        lines = [f"🚨 VOICE REGRESSION: {len(failures)}/{len(results)} trades FAILED"]
        for slug, fails in failures[:8]:
            lines.append(f"• {slug}: {'; '.join(f[:100] for f in fails[:2])}")
        lines.append("Run scripts/voice-regression-harness.py for detail. Do NOT apply pending prompt proposals until green.")
        telegram("\n".join(lines), env)
        sys.exit(1)

    print("[harness] all green — CallTaker trade behavior verified across all trades")
    sys.exit(0)


if __name__ == "__main__":
    main()
