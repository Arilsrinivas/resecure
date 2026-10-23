# ReSecure QA & Architecture Report: PDF One-Click Repair Pipeline Fix

**Task ID:** BUG-RS-009  
**Branch:** `fix/pdf-repair-pipeline`  
**Date:** 2026-10-23  
**Auditor:** CalmStacks Agent Factory (QA & Lead Systems Engineer)  
**Status:** VERIFIED & MERGED  

---

## 1. Executive Summary & Root Cause Analysis

A critical bug was reported in the **One-Click Repair** workflow:
- The user uploaded a corrupted PDF (`SES module 2 notes (1).pdf`, 1,721,322 bytes).
- ReSecure claimed the file was repaired and offered a download link.
- When the user attempted to open or re-upload the repaired file, it was reported as empty (0 bytes).
- The user supplied the original, healthy 37-page PDF (`SES module 2 notes.pdf`, 1,721,322 bytes) as a reference.

### Root Cause Diagnosis

Through bit-level forensic inspection of the uploaded damaged file, original reference file, and execution traces, the root cause was isolated across three components:

1. **Damaged File Characteristics (Total Entropy Loss):**
   - The corrupted file uploaded by the user (`SES module 2 notes (1).pdf`) consisted of **1,721,322 consecutive null bytes (`0x00`)** with zero information entropy (SHA-256: `9b20e996509348cb724d4c9d0cd704bc8c8babe7796f835585447b83c5a37d51`).
   - The healthy reference file (`SES module 2 notes.pdf`) was 1,721,322 bytes, 37 pages, valid PDF-1.4 (SHA-256: `ec98147f130e89142a8e8b0cd4b8119fc1d7c411904adc9b503b59cf6a6147ea`).

2. **Format Misclassification Bug:**
   - In `detect_file_format()`, the format detection heuristic sampled the first 4,096 bytes and attempted `sample.decode('utf-8')`.
   - Because `\x00` (null byte) is a valid UTF-8 code point (`U+0000`), Python's UTF-8 decoder did not raise a `UnicodeDecodeError`.
   - Furthermore, PDF was not an explicitly recognized format in `detect_file_format()`.
   - As a result, the zero-filled binary PDF was erroneously classified as **`TEXT`**.

3. **Empty Output Generation Bug:**
   - The file was dispatched to `repair_text()`, which executed `content_bytes.replace(b"\x00", b"")`.
   - Stripping all null bytes from a file composed 100% of null bytes produced an empty byte string `b""` (0 bytes).
   - `repair_file_format()` wrote this 0-byte string to disk (`repaired_...pdf`) and reported `mutations_count: 1`.
   - `one_click_repair` in `routes.py` inspected `mutations_count > 0`, declared `verdict: "REPAIRED"`, and generated a download link for the 0-byte file.

---

## 2. Seven-Stage Pipeline Audit & Remediations

| Pipeline Stage | Defect Identified | Remediation Implemented | Verification Result |
| :--- | :--- | :--- | :--- |
| **1. File Reception & Staging** | Upload stream chunks buffered without forced flush. | Added explicit `out_f.flush()` and `os.fsync(out_f.fileno())` for input, manifest, and parity uploads. | All uploaded files fully flushed before processing. |
| **2. PDF Corruption Analysis** | Null bytes misinterpreted as valid UTF-8 text; PDF format not supported. | Added ISO 32000 PDF magic byte detection (`%PDF-`), extension matching, null-ratio guard (>5% nulls classified as `BINARY`, not text), and integrated `pypdf.PdfReader` deep structural validator. | Zero-filled PDF flagged with `rule_name: "PDF_ZERO_FILL_WIPED"`, severity `ERROR`. |
| **3. Recovery Engine Execution** | Zero-wiped file sent to text repair. | Implemented dedicated `repair_pdf()` engine with preamble cleanup, header synthesis, EOF reconstruction, and `pypdf` catalog validation. | PDF files routed exclusively to PDF validator and repair engine. |
| **4. Recovered Byte Generation** | Stripping nulls generated 0 bytes without failure check. | Enforced invariant: if `repaired_bytes` is empty (`len == 0`), recovery engine returns `verdict: "UNRECOVERABLE"` with zero mutations. Total entropy loss cannot be salvaged without parity. | Empty byte strings immediately halted. |
| **5. Output Writing & Flushing** | 0-byte files written to disk and left in staging directory. | If `output_bytes == 0` or verdict is `UNRECOVERABLE`, output file is never written, or if previously created, unlinked (`unlink(missing_ok=True)`). Guaranteed `flush()` and `fsync()`. | No 0-byte files ever created on disk. |
| **6. Backend Response & Download** | Download URL generated despite unrecoverable state. | `one_click_repair` verifies `out_repaired.is_file() and out_repaired.stat().st_size > 0` before declaring repair. Sets `download_url: None` on failure. | Download URL is strictly `None` when recovery is unrecoverable. |
| **7. Download Endpoint Protection** | Download endpoint could serve 0-byte files if requested. | Added guard in `download_repaired_file()`: if `target.stat().st_size == 0`, raises HTTP 422 Unprocessable Content. | 0-byte downloads are strictly blocked. |

---

## 3. Cryptographic & Mathematical Recovery Comparison

### Scenario A: Zero-Wiped Damaged Input (`SES module 2 notes (1).pdf`)
- **Input Size:** 1,721,322 bytes
- **Entropy:** 0.00 bits/byte (100% `0x00`)
- **Input SHA-256:** `9b20e996509348cb724d4c9d0cd704bc8c8babe7796f835585447b83c5a37d51`
- **Reference SHA-256:** `ec98147f130e89142a8e8b0cd4b8119fc1d7c411904adc9b503b59cf6a6147ea`
- **Result:** Heuristic salvage correctly reports **`UNRECOVERABLE`**.
- **Explanation:** In accordance with Shannon information theory, a file with zero surviving information cannot be reconstructed without independent redundancy (parity). ReSecure honestly reports this limitation rather than emitting an empty or corrupted file.

### Scenario B: Reed–Solomon Exact Bit-for-Bit Recovery (`SES module 2 notes.pdf`)
- **Input File:** 37-page healthy PDF (1,721,322 bytes)
- **Protection Parameters:** 64KB chunking ($K = 27$ data chunks), 20% redundancy ($M = 6$ parity shards).
- **Simulated Damage:** Damaged chunks 3 and 12 with multi-byte overwrite ($E = 2 \le M$).
- **Reconstruction Output:**
  - Recovered SHA-256: `ec98147f130e89142a8e8b0cd4b8119fc1d7c411904adc9b503b59cf6a6147ea`
  - Exact Match: **`True`**
  - Bit-for-bit Invariance: **`100% IDENTICAL`**
  - Page Count Verified via `pypdf`: **`37 / 37 pages parsed`**

---

## 4. Verification Evidence & Test Execution Logs

### Automated Test Runs
1. **Dedicated PDF Pipeline Tests (`resecure/backend/tests/test_pdf_pipeline.py`):**
   - `test_pdf_pipeline_zero_fill_damaged_returns_unrecoverable` → **PASSED**
   - `test_pdf_pipeline_pristine_pdf` → **PASSED**
   - `test_pdf_pipeline_repairable_pdf` → **PASSED**
   - `test_pdf_pipeline_reed_solomon_exact_recovery` → **PASSED**
   - `test_pdf_pipeline_non_destructive` → **PASSED**
   - `test_download_endpoint_blocks_zero_byte_file` → **PASSED**
   - **Result: 6/6 PASSED**

2. **Full Backend Test Suite (`resecure/backend/tests/`):**
   - **Result: 59/59 PASSED (0 failures, 0 skipped)**

3. **Acceptance Gates Suite (`tests/test_resecure_acceptance_gates.py`):**
   - **Result: 12/12 PASSED (0 failures)**

4. **Master End-to-End Suite (`Test-ReSecureE2E.ps1`):**
   - Acceptance Gates: **PASS (12/12)**
   - Backend Tests: **PASS (59/59)**
   - AI/ML Evals: **PASS (16/16)**
   - Frontend Invariant Tests: **PASS (10/10)**
   - Frontend Production Build: **PASS (dist/ verified)**
   - Static Security & Secret Scan: **PASS (0 leaks across 3,929 files)**
   - Documentation & Contracts: **PASS (All 6 verified)**
   - **Overall Pass Rate: 100% (97 / 97 tests passed)**

5. **Live API Smoke Verification:**
   - Upload zero-wiped PDF → HTTP 200, `verdict: "UNRECOVERABLE"`, `download_url: None`, `mutations_count: 0`.
   - Upload healthy 37-page PDF → HTTP 200, `verdict: "PRISTINE"`, `download_url: valid`, downloaded file verified at 1,721,322 bytes and 37 pages.
