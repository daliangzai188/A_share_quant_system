# Read-only: after production's close pipeline (step 13 done), upload everything the Mac needs
# to build the live QMT-native file (prepare_live.sh): step-13 dataset + decision, raw since
# 2026-10-09, and the production state snapshot.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($s in @('upload_eq_dataset.ps1', 'upload_raw.ps1', 'state_inventory.ps1')) {
  "== $s"
  & powershell -ep bypass -f (Join-Path $here $s)
}
'UPLOAD FOR LIVE DONE'
