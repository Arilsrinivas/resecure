# CalmStacks Agent Factory — Feature Handoff Report
## One-Click Repair Default Landing Page

- **Task ID:** TASK-RS-009
- **Epic:** `resecure-one-click-repair`
- **Branch:** `feature/one-click-repair`
- **Status:** READY_FOR_REVIEW
- **Date:** 2026-10-23

---

### Executive Summary

Implemented a dedicated **One-Click Repair** default landing page for the ReSecure application. When users open ReSecure or navigate to the root route, they are immediately greeted by a streamlined, cybersecurity-grade interface designed to analyze any uploaded file, automatically identify its format, evaluate structural integrity, and execute available recovery/repair algorithms—without requiring manual configuration of hashes, chunk sizes, parity settings, or manifests.

This change is **strictly additive**: all existing functionality (Protect & Parity, Integrity Scanner, Recovery Studio, Format Repair, Forensic Reports) is preserved, fully accessible, and seamlessly linked.

---

### Architecture & Engine Reuse

1. **Backend Endpoint (`POST /api/one-click-repair`):**
   - Ingests uploaded file via multipart/form-data or local staging path.
   - Computes streaming SHA-256 root hash.
   - Reuses `detect_file_format` for automated MIME/format detection (JSON, CSV, Plain Text, PNG, JPEG, Binary).
   - If optional companion manifest and parity files are uploaded or staged, attempts exact Reed-Solomon Galois Field $GF(2^8)$ reconstruction.
   - Reuses `validate_format` to detect structural anomalies (ragged rows, unclosed brackets, missing EOF/IEND markers, trailing commas).
   - Invokes `repair_file_format` when syntax/structural flaws exist.
   - Performs post-repair verification to ensure repaired files pass format validation.
   - Staged safely in `data/staging/` with isolated naming; source file is untouched (**Non-Destructive Invariant Guarantee**).
   - Generates `/api/download-repaired/{job_id}` endpoint.

2. **Download Endpoint (`GET /api/download-repaired/{job_id}`):**
   - Validates UUID job format.
   - Strictly jailed to staging directory with path traversal defense.
   - Delivers repaired output as `application/octet-stream` with content disposition filename.

3. **Frontend Landing Page Component (`OneClickRepair.tsx`):**
   - Default active tab (`activeTab = 'ONE_CLICK'`) on startup.
   - Tab re-ordering: One-Click Repair is #1, followed by 2. Protect & Parity, 3. Integrity Scanner, 4. Recovery Studio, 5. Format Repair, 6. Forensic Reports.
   - Obsidian Slate visual theme (`#0A0F1D`, `#111827`, `#1E293B`, `#06B6D4`).
   - Drag-and-drop file upload zone + system file picker.
   - Supported formats badge grid (JSON, CSV, Text, PNG, JPEG, RS Parity).
   - Real-time progress bar (Header Analysis -> Format Detection -> Structural Repair -> Hash Invariant Check).
   - Honest status reporting:
     - `EXACT_RECOVERED`: 100% bit-for-bit SHA-256 match certificate.
     - `REPAIRED`: Partial heuristic salvage with full mutation disclosure log and change list.
     - `PRISTINE`: File already healthy, no mutations applied.
     - `UNRECOVERABLE`: Safe failure executed; damage exceeds heuristic capability, directs user to Recovery Studio.
     - `UNSUPPORTED`: Graceful unsupported format warning.
   - Working file download button.

---

### Verification & Testing Evidence

All CalmStacks Agent Factory stage gates passed:

1. **Backend Unit & Integration Tests:**
   - Command: `resecure\backend\venv\Scripts\python.exe -m pytest resecure/backend/tests/`
   - Result: **53 / 53 passed** (including 5 new tests in `test_one_click_repair.py`).
2. **Frontend Invariant & Contract Tests:**
   - Command: `node --experimental-strip-types resecure/frontend/tests/verify-frontend.mjs`
   - Result: **10 / 10 passed**.
3. **Frontend Production Build:**
   - Command: `npm run build`
   - Result: Zero TypeScript errors; Vite production bundle built cleanly in < 1s.
4. **Master 12 Acceptance Gates & E2E Validation:**
   - Command: `powershell -File .\scripts\validation\Test-ReSecureE2E.ps1`
   - Result: **7 / 7 checks passed, 91 / 91 automated tests passed, 0 failures**.
5. **Live Process Verification:**
   - Live backend API on port 8000 and frontend on port 3000 verified via real HTTP request execution.
