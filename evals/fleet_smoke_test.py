#!/usr/bin/env python3
"""
the platform fleet regression harness — fires each agent's MAIN write action with
its calibrated payload, asserts a NEW row persisted (row-count delta) AND the
response reports success:true, then cleans up. Run on the VPS.

Usage:  python3 /opt/app/scripts/fleet-smoke-test.py
Exit 0 if all pass, 1 if any fail. Cron-safe (creates + deletes test rows only
for the test tenant). Payloads/task-keys captured during 2026-05-30 hardening.
"""
import json, subprocess, time, urllib.request, sys, re

N8N='http://localhost:5678'
TENANT='user_3AXnIdWPckcDDPwJSwq46Mf7emq'  # Test HVAC Corp

def env(k):
    for line in open('/opt/app/.env'):
        s=line.strip()
        if s.startswith(k+'='): return s.split('=',1)[1].strip().strip('"').strip("'")
    return ''
DBUSER=env('DB_USER') or 'hvac'; DBNAME=env('DB_NAME') or 'app_db'

def psql(sql):
    r=subprocess.run(['docker','exec','hvac-postgres','psql','-U',DBUSER,'-d',DBNAME,'-tAc',sql],
                     capture_output=True,text=True,timeout=30)
    return (r.stdout or '').strip()

def fire(wh,payload):
    req=urllib.request.Request(f'{N8N}/webhook/{wh}',data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=60) as r: return r.read().decode()
    except urllib.error.HTTPError as e:
        try: return e.read().decode()
        except: return f'HTTP_{e.code}'
    except Exception as e: return f'ERR:{e}'

# agent -> webhook, target table
WH={'Scheduling':('scheduling-agent-v2','AgentAppointment'),'WorkOrder':('work-order-v2','AgentWorkOrder'),
 'Accounting':('accounting-agent-v2','AgentInvoice'),'ReviewReferral':('review-referral-v2','AgentReferral'),
 'SalesFollowUp':('sales-follow-up-v2','AgentSalesSequence'),'Permit':('permit-agent-v2','AgentPermit'),
 'OPS_KPI':('ops-kpi-v1','OpsKpiSnapshot'),'OPS_PayrollHR':('ops-payroll-hr-v1','OpsOnboarding'),
 'MKT_Attribution':('mkt-attribution-v1','MktTouchpoint'),'MKT_Reporting':('mkt-reporting-v1','MktReport'),
 'MKT_AdEngine':('mkt-adengine-v1','MktCampaign'),'MKT_PromoBlast':('mkt-promoblast-v1','MktPromo'),
 'MKT_Retargeting':('mkt-retargeting-v1','MktAudience'),'MKT_SEOContent':('mkt-seocontent-v1','MktContent')}

CAL=json.load(open('/opt/app/scripts/_calibrated_payloads.json'))

# Agents whose write FK-references an existing AgentWorkOrder — seed a real parent first.
FK_PARENT={'ReviewReferral':('work_order_id',False), 'Permit':('work_order_id',True)}  # (field, nested-under-data)

def seed_workorder():
    wid=psql(f"INSERT INTO \"AgentWorkOrder\" (\"id\",\"tenantId\",\"status\",\"jobType\",\"description\",\"createdAt\",\"updatedAt\") "
             f"VALUES (gen_random_uuid()::text,'{TENANT}','completed','Installation','smoke-parent',NOW(),NOW()) RETURNING id;")
    # psql -tAc on INSERT...RETURNING appends the "INSERT 0 1" status line — take only the uuid
    return wid.split()[0] if wid else wid

# Mock-sentinel: a row that "landed" but contains fabrication markers is NOT a real row.
# Inspects the agent-created rows (last 5 min, this tenant) for fake-data tells. This turns
# the smoke from "a row persisted" into "a REAL row persisted" — it is expected to flip mock
# agents (Accounting/etc.) RED until they are de-mocked.
MOCK_PATTERNS = [
    (re.compile(r'Customer [A-E]\b'), 'Customer A-E'),
    (re.compile(r'\bMOCK\b', re.I), 'MOCK literal'),
    (re.compile(r'Math\.random|Date\.now|Lorem ipsum', re.I), 'codegen artifact'),
]
def mock_reason(table):
    blob = psql(f"SELECT COALESCE(string_agg(row_to_json(t)::text, ' '), '') FROM \"{table}\" t "
                f"WHERE \"tenantId\"='{TENANT}' AND \"createdAt\" > NOW() - INTERVAL '5 minutes';")
    for rx, label in MOCK_PATTERNS:
        if rx.search(blob): return label
    return None

print(f"=== the platform fleet regression harness — {time.strftime('%Y-%m-%d %H:%M')} ===\n")
passed=0; failed=[]
for agent,(wh,table) in WH.items():
    c=CAL.get(agent,{})
    try: payload=json.loads(c.get('payload','{}'))
    except: failed.append(f'{agent}(bad payload)'); print(f"  FAIL {agent}: bad calibrated payload"); continue
    payload['auth']={'tenant_id':TENANT,'user_id':'smoke','role':'manager'}
    if agent in FK_PARENT:
        field,nested=FK_PARENT[agent]; woid=seed_workorder()
        if nested: payload.setdefault('data',{})[field]=woid
        else: payload[field]=woid
    task=(c.get('task') or '').split()[0]
    if task and task!='always-on' and '.' in task:
        psql(f"INSERT INTO \"TenantTaskSetting\" (id,\"tenantId\",\"taskKey\",enabled,\"createdAt\",\"updatedAt\") VALUES (gen_random_uuid()::text,'{TENANT}','{task}',true,NOW(),NOW()) ON CONFLICT DO NOTHING;")
    before=psql(f"SELECT COUNT(*) FROM \"{table}\" WHERE \"tenantId\"='{TENANT}';")
    resp=fire(wh,payload); time.sleep(1.5)
    after=psql(f"SELECT COUNT(*) FROM \"{table}\" WHERE \"tenantId\"='{TENANT}';")
    succ='"success":true' in resp.replace(' ','')
    grew = before.isdigit() and after.isdigit() and int(after)>int(before)
    mock = mock_reason(table) if grew else None
    ok = succ and grew and not mock
    flag = '' if ok else (f'MOCK-DATA:{mock}' if mock else resp[:130])
    print(f"  {'PASS' if ok else 'FAIL'}  {agent:<16} {table:<18} rows {before}->{after} success={succ}  {flag}")
    if ok: passed+=1
    else: failed.append(agent)
    # cleanup: rows for this tenant created in the last 5 min + the task setting
    psql(f"DELETE FROM \"{table}\" WHERE \"tenantId\"='{TENANT}' AND \"createdAt\" > NOW() - INTERVAL '5 minutes';")
    if task and task!='always-on' and '.' in task:
        psql(f"DELETE FROM \"TenantTaskSetting\" WHERE \"tenantId\"='{TENANT}' AND \"taskKey\"='{task}';")
    if agent in FK_PARENT:
        psql(f"DELETE FROM \"AgentWorkOrder\" WHERE \"tenantId\"='{TENANT}' AND \"description\"='smoke-parent';")

print(f"\n=== {passed}/{len(WH)} agents PASS ===")
if failed: print("FAILED:", ', '.join(failed)); sys.exit(1)
print("ALL GREEN")
sys.exit(0)
