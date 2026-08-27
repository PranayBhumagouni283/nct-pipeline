import sys
sys.path.insert(0, r'C:\Users\LAPTOP\Downloads\NCT_Record_History_Changes')
sys.path.insert(0, r'C:\Users\LAPTOP\Downloads\NCT_Combined_Pipeline')

import json
import combined_pipeline as cp

print("=== PRE-FLIGHT CHECK ===\n")

# 1. Dept init
print("[1] Department setup...")
cp.init_dept('ADC')
print()

# 2. Tracking list
print("[2] Tracking list...")
nct_ids = cp.load_nct_ids_from_excel(str(cp.TRACKING_LIST))
print(f"  NCT IDs loaded : {len(nct_ids)}")
print(f"  Sample         : {nct_ids[:3]}")
print()

# 3. Keywords
print("[3] Keywords...")
kws = cp.load_keywords()
print()

# 4. State
print("[4] State...")
state = cp.load_state()
lrd = state["last_run_date"]
etag = state["etag"]
print(f"  last_run_date : {lrd if lrd else 'never (first run)'}")
print(f"  etag          : {'set' if etag else 'none (first run)'}")
print()

# 5. Alert config
print("[5] Alert config...")
acfg = json.loads(cp.ALERT_CONFIG.read_text(encoding="utf-8"))
print(f"  enabled   : {acfg['enabled']}")
print(f"  dept_name : {acfg['dept_name']}")
print(f"  to        : {acfg['to']}")
print(f"  cc        : {acfg.get('cc', [])}")
print()

# 6. SMTP config
print("[6] SMTP config...")
scfg = json.loads(cp.SMTP_CONFIG.read_text(encoding="utf-8"))
configured = scfg['smtp_user'] != 'your-sender@gmail.com'
print(f"  smtp_host  : {scfg['smtp_host']}:{scfg['smtp_port']}")
print(f"  smtp_user  : {scfg['smtp_user']}")
print(f"  configured : {'YES' if configured else 'NO - still placeholder values'}")
print()

# 7. Dependencies
print("[7] Python dependencies...")
for mod in ['requests', 'openpyxl', 'pandas']:
    try:
        __import__(mod)
        print(f"  {mod:<12} OK")
    except ImportError:
        print(f"  {mod:<12} MISSING - run: pip install {mod}")
print()

# 8. NCT script imports
print("[8] NCT_Record_History_Changes imports...")
try:
    from NCT_Changes_Tracker import get_history_list, get_latest_comparison, _write_sheet
    from CT_PULL import fetch_study_data, build_raw_row
    print("  All imports OK")
except Exception as e:
    print(f"  FAILED: {e}")
print()

# Summary
print("=" * 40)
issues = []
if len(nct_ids) == 0:
    issues.append("Tracking list is empty")
if len(kws) == 0:
    issues.append("No keywords loaded")
if not acfg['enabled']:
    issues.append("Alert is disabled in alert_config.json")
if not configured:
    issues.append("SMTP not configured (still has placeholder values)")
if acfg['to'] == ["adc-team@company.com"]:
    issues.append("Alert recipients still have placeholder email addresses")

if issues:
    print("ISSUES TO FIX BEFORE RUNNING:")
    for i, issue in enumerate(issues, 1):
        print(f"  {i}. {issue}")
else:
    print("ALL CHECKS PASSED - Ready to run:")
    print()
    print("  python combined_pipeline.py ADC")
print()
