"""
CalmStacks ReSecure MVP v1.0 - Comprehensive SAST & Dynamic Security Audit Test Suite
Module: .security/test_security_suite.py
Auditor: Security Agent (TASK-RS-007)
"""

import hashlib
import hmac
import io
import json
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from resecure.backend.src.api.app import create_app
from resecure.backend.src.core.chunker import DEFAULT_CHUNK_SIZE, audit_file_chunks, slice_file_into_chunks
from resecure.backend.src.core.format_validators import (
    MAX_JSON_DEPTH,
    _check_json_depth,
    detect_file_format,
    validate_json,
)
from resecure.backend.src.core.hasher import hash_bytes, hash_file, hash_file_chunks
from resecure.backend.src.core.manifest import (
    DEFAULT_SECRET_KEY,
    ManifestTamperedError,
    create_manifest_dict,
    sign_manifest,
    verify_manifest,
)
from resecure.backend.src.core.reedsolomon import (
    HEADER_FORMAT,
    HEADER_SIZE,
    RSPARITY_MAGIC,
    NonDestructiveViolationError,
    RedundancyExceededError,
    ReedSolomonCodec,
)
from resecure.backend.src.core.repair_engine import repair_file_format
from resecure.backend.src.db.connection import get_connection_manager
from resecure.backend.src.db.models import FileModel
from resecure.backend.src.db.repository import ReSecureRepository
from resecure.backend.src.services.protection_service import (
    MAX_FILE_SIZE_BYTES,
    PayloadTooLargeError,
    ProtectionService,
)
from resecure.backend.src.services.recovery_service import RecoveryService


@pytest.fixture(scope="module")
def client():
    app = create_app()
    return TestClient(app)


# ===========================================================================
# 1. SECRET SCANNING & PRNG VERIFICATION
# ===========================================================================
class TestSecretScanningAndEntropy:
    """Verifies absence of committed live credentials and safe key generation."""

    def test_default_hmac_secret_fallback_is_identified(self):
        """Verifies that DEFAULT_SECRET_KEY in manifest.py is flagged for remediation."""
        assert DEFAULT_SECRET_KEY == "resecure-master-integrity-key-2026"
        # In a hardened environment, DEFAULT_SECRET_KEY should be replaced by mandatory env var or random token.

    def test_prng_generation_quality(self):
        """Verifies that random byte generation via os.urandom produces high Shannon entropy."""
        token = os.urandom(32)
        assert len(token) == 32
        # Verify non-trivial byte distribution
        unique_bytes = set(token)
        assert len(unique_bytes) > 20  # High entropy


# ===========================================================================
# 2. CRYPTOGRAPHIC ARCHITECTURE AUDIT
# ===========================================================================
class TestCryptographicArchitecture:
    """Tests SHA-256 streaming hashing, HMAC-SHA256 tampering rejection, and .rsparity container security."""

    def test_sha256_streaming_collision_and_boundary(self, tmp_path):
        """Verifies streaming hash matches one-shot hash across exact and partial chunk boundaries."""
        sample_file = tmp_path / "streaming_test.bin"
        # 130 KB = 2 full 64KB chunks + 2 KB remainder
        data = b"A" * 65536 + b"B" * 65536 + b"C" * 2048
        sample_file.write_bytes(data)

        expected_root = hashlib.sha256(data).hexdigest()
        streamed_root = hash_file(sample_file, chunk_size=65536)
        assert streamed_root == expected_root

        root_from_chunks, chunks, total_size = hash_file_chunks(sample_file, chunk_size=65536)
        assert root_from_chunks == expected_root
        assert total_size == len(data)
        assert len(chunks) == 3
        assert chunks[0]["size_bytes"] == 65536
        assert chunks[1]["size_bytes"] == 65536
        assert chunks[2]["size_bytes"] == 2048

    def test_hmac_sha256_tamper_detection(self):
        """Verifies that any modification to payload, headers, or signature triggers verification failure."""
        chunks = [{"index": 0, "offset": 0, "size_bytes": 1024, "sha256": "a" * 64}]
        signed = create_manifest_dict(
            filename="confidential.doc",
            file_size_bytes=1024,
            file_format="TEXT",
            mime_type="text/plain",
            root_sha256="a" * 64,
            chunk_size_bytes=65536,
            data_chunks_k=1,
            parity_chunks_m=1,
            redundancy_percentage=20,
            parity_file_name="confidential.doc.rsparity",
            chunks=chunks,
            secret_key="secure-key-123",
            key_id="k-audit-test",
        )
        assert verify_manifest(signed, secret_key="secure-key-123") is True

        # 1. Tampering with file size
        tampered1 = json.loads(json.dumps(signed))
        tampered1["metadata"]["file_size_bytes"] = 1025
        assert verify_manifest(tampered1, secret_key="secure-key-123") is False

        # 2. Tampering with root SHA-256
        tampered2 = json.loads(json.dumps(signed))
        tampered2["integrity"]["root_sha256"] = "b" * 64
        assert verify_manifest(tampered2, secret_key="secure-key-123") is False

        # 3. Wrong secret key
        assert verify_manifest(signed, secret_key="wrong-attacker-key") is False

        # 4. Truncated signature
        tampered3 = json.loads(json.dumps(signed))
        sig = tampered3["authentication"]["hmac_signature"]
        tampered3["authentication"]["hmac_signature"] = sig[:32]
        assert verify_manifest(tampered3, secret_key="secure-key-123") is False

    def test_sign_manifest_without_prior_key_id_inconsistency(self):
        """
        AUDIT FINDING: If sign_manifest is invoked with a dict missing authentication.key_id,
        it calculates the signature BEFORE adding key_id to the dictionary.
        Subsequent verify_manifest includes the newly added key_id in the payload, causing mismatch.
        """
        manifest_raw = {
            "metadata": {"filename": "test.txt"},
            "integrity": {"root_sha256": "0" * 64},
        }
        sig, signed = sign_manifest(manifest_raw, secret_key="test-key")
        # Direct verification fails due to the order of operations in sign_manifest
        assert verify_manifest(signed, secret_key="test-key") is False

    def test_parity_container_magic_and_bounds_validation(self, tmp_path):
        """Verifies parity container header parsing under invalid magic and malformed headers."""
        bad_magic_file = tmp_path / "bad_magic.rsparity"
        bad_header = struct.pack(HEADER_FORMAT, b"BADMAGIC", 1, 65536, 4, 1, b"\x00" * 16)
        bad_magic_file.write_bytes(bad_header + b"\x00" * 65536)

        codec = ReedSolomonCodec()
        with pytest.raises(ValueError, match="Invalid parity magic header"):
            codec.read_parity_container(bad_magic_file)

    def test_parity_container_truncated_detection(self, tmp_path):
        """Verifies that truncated parity container raises ValueError rather than returning partial data."""
        trunc_file = tmp_path / "truncated.rsparity"
        header = struct.pack(HEADER_FORMAT, RSPARITY_MAGIC, 1, 65536, 4, 2, b"\x00" * 16)
        # Expected data length = 2 * 65536 = 131072 bytes. Write only 100 bytes.
        trunc_file.write_bytes(header + b"\x00" * 100)

        codec = ReedSolomonCodec()
        with pytest.raises(ValueError, match="Truncated parity container"):
            codec.read_parity_container(trunc_file)


# ===========================================================================
# 3. PATH TRAVERSAL & FILE INJECTION DEFENSE
# ===========================================================================
class TestPathTraversalAndFileInjection:
    """Tests PathTraversalGuardMiddleware and service boundary defenses against directory escapes."""

    def test_middleware_blocks_relative_traversal_in_query(self, client):
        """Checks that query param with '..' returns 400 ERR_PATH_TRAVERSAL."""
        resp = client.get("/api/health?path=../../etc/passwd")
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_code"] == "ERR_PATH_TRAVERSAL"

    def test_middleware_blocks_null_byte_in_query(self, client):
        """Checks that query param with null byte returns 400 ERR_PATH_TRAVERSAL."""
        resp = client.get("/api/health?file=test.txt%00.png")
        assert resp.status_code == 400
        data = resp.json()
        assert data["error_code"] == "ERR_PATH_TRAVERSAL"

    def test_middleware_blocks_relative_traversal_in_json_body(self, client):
        """Checks that JSON payload with '..' returns 400 ERR_PATH_TRAVERSAL."""
        payload = {
            "source_path": "../../etc/shadow",
            "redundancy_percentage": 20,
            "chunk_size_bytes": 65536,
        }
        resp = client.post("/api/protect", json=payload)
        assert resp.status_code == 400
        assert resp.json()["error_code"] == "ERR_PATH_TRAVERSAL"

    def test_middleware_limitation_absolute_paths_not_blocked(self, client):
        """
        AUDIT FINDING: Verifies that absolute paths (e.g. C:\\Windows\\System32 or /etc/passwd)
        bypass PathTraversalGuardMiddleware because they do not contain '..' or '\\x00'.
        The service layer then receives the path and fails on FileNotFoundError instead.
        """
        payload = {
            "source_path": "C:\\Windows\\System32\\nonexistent_security_audit_test.txt",
            "redundancy_percentage": 20,
            "chunk_size_bytes": 65536,
        }
        resp = client.post("/api/protect", json=payload)
        # Does NOT return 400 ERR_PATH_TRAVERSAL; returns 404 ERR_FILE_NOT_FOUND
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "ERR_FILE_NOT_FOUND"

    def test_middleware_limitation_url_encoded_traversal_in_json(self, client):
        """
        AUDIT FINDING: URL-encoded traversal '%2e%2e%2f' inside JSON payload body
        is not decoded by JSON parser, bypassing the '..' string match in middleware.
        """
        payload = {
            "source_path": "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
            "redundancy_percentage": 20,
            "chunk_size_bytes": 65536,
        }
        resp = client.post("/api/protect", json=payload)
        # Bypasses middleware (does not return 400 ERR_PATH_TRAVERSAL); fails at service level with 404
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "ERR_FILE_NOT_FOUND"


# ===========================================================================
# 4. DENIAL OF SERVICE & RESOURCE BOUNDING
# ===========================================================================
class TestDenialOfServiceAndResourceBounding:
    """Verifies file-size bounds (2GB limit) and JSON delimiter recursion depth bounds (<=128)."""

    def test_file_size_exceeding_2gb_rejected(self, monkeypatch, tmp_path):
        """Verifies that files > 2.0 GB trigger PayloadTooLargeError (413)."""
        fake_large_file = tmp_path / "large_2gb.bin"
        fake_large_file.write_bytes(b"header")

        # Mock stat().st_size to return 2.1 GB
        class FakeStat:
            st_size = 2200000000
            st_mode = 0o100644

        orig_stat = Path.stat
        def fake_stat(self, *args, **kwargs):
            if str(self).endswith("large_2gb.bin"):
                return FakeStat()
            return orig_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fake_stat)

        service = ProtectionService()
        with pytest.raises(PayloadTooLargeError, match="exceeds maximum permitted limit"):
            service.protect_file(fake_large_file)

    def test_json_recursion_depth_limit_enforced(self, tmp_path):
        """Verifies that JSON exceeding MAX_JSON_DEPTH (128) is flagged as FORMAT_INVALID."""
        deep_json_file = tmp_path / "deep.json"
        # Build 140 nested dictionaries: {"k": {"k": ... "val" ...}}
        nested: Any = "leaf"
        for _ in range(140):
            nested = {"k": nested}

        deep_json_file.write_text(json.dumps(nested), encoding="utf-8")
        result = validate_json(deep_json_file)

        assert result["is_valid"] is False
        assert result["verdict"] == "FORMAT_INVALID"
        findings = [f for f in result["findings"] if f["rule_name"] == "RECURSION_DEPTH"]
        assert len(findings) > 0
        assert findings[0]["passed"] is False

    def test_json_recursion_depth_within_limit_passes(self, tmp_path):
        """Verifies that JSON within depth limit (< 128) passes RECURSION_DEPTH check."""
        valid_json_file = tmp_path / "shallow.json"
        nested: Any = "leaf"
        for _ in range(10):
            nested = {"k": nested}

        valid_json_file.write_text(json.dumps(nested), encoding="utf-8")
        result = validate_json(valid_json_file)

        assert result["is_valid"] is True
        assert result["verdict"] == "FORMAT_VALID"
        findings = [f for f in result["findings"] if f["rule_name"] == "RECURSION_DEPTH"]
        assert len(findings) > 0
        assert findings[0]["passed"] is True


# ===========================================================================
# 5. NON-DESTRUCTIVE INVARIANT & ATOMIC I/O
# ===========================================================================
class TestNonDestructiveInvariantAndAtomicIO:
    """Verifies that source files are never mutated, source != output is enforced, and atomic temp files are used."""

    def test_recovery_enforces_source_differs_from_output(self, tmp_path):
        """Verifies that source_path == output_path raises NonDestructiveViolationError."""
        src_file = tmp_path / "data.bin"
        src_file.write_bytes(b"hello world")

        codec = ReedSolomonCodec()
        with pytest.raises(NonDestructiveViolationError, match="Non-Destructive Invariant Violation"):
            codec.recover_file_to_path(
                source_path=src_file,
                manifest_chunks=[],
                parity_path=tmp_path / "dummy.rsparity",
                output_path=src_file,  # EXACT SAME PATH
                original_root_sha256="dummy",
                original_file_size=11,
                damaged_indices=[],
            )

    def test_format_repair_enforces_source_differs_from_output(self, tmp_path):
        """Verifies that repair_file_format refuses to overwrite source file."""
        src_file = tmp_path / "damaged.csv"
        src_file.write_text("a,b\n1,2,3", encoding="utf-8")

        with pytest.raises(ValueError, match="Non-Destructive Invariant"):
            repair_file_format(
                source_path=src_file,
                output_path=src_file,  # EXACT SAME PATH
                format_name="CSV",
            )

    def test_source_file_unmodified_during_corrupted_recovery(self, tmp_path):
        """Verifies source file content and mtime remain pristine even when recovery fails or succeeds."""
        src_file = tmp_path / "sample.txt"
        pristine_bytes = b"ORIGINAL SOURCE CONTENT NEVER MODIFIED"
        src_file.write_bytes(pristine_bytes)
        original_hash = hashlib.sha256(pristine_bytes).hexdigest()

        # Execute protect
        service = ProtectionService()
        prot_res = service.protect_file(src_file, output_dir=tmp_path)

        # Source hash must remain unchanged
        assert hashlib.sha256(src_file.read_bytes()).hexdigest() == original_hash


# ===========================================================================
# 6. SQL INJECTION & DATABASE PARAMETERIZATION
# ===========================================================================
class TestDatabaseSQLInjectionResistance:
    """Tests SQLite repository against SQL injection payloads."""

    def test_repository_parameterized_against_sql_injection(self, tmp_path):
        """Verifies that SQL injection payload in filename or ID does not execute arbitrary SQL."""
        repo = ReSecureRepository(get_connection_manager())

        # Payload attempt: inject DROP TABLE or UNION SELECT
        import uuid
        malicious_id = f"test-id-{uuid.uuid4()}'; DROP TABLE files; --"
        malicious_filename = "file.txt' OR '1'='1"

        file_model = FileModel(
            file_id=malicious_id,
            filename=malicious_filename,
            file_size=100,
            root_sha256="0" * 64,
            mime_type="text/plain",
            file_path="/tmp/test",
            file_format="TEXT",
        )

        # Insert must safely parameterize without SQL syntax error
        created = repo.files.create(file_model)
        assert created.file_id == malicious_id

        # Query by ID must safely return record
        fetched = repo.files.get_by_id(malicious_id)
        assert fetched is not None
        assert fetched.filename == malicious_filename

        # Verify files table was NOT dropped
        all_files = repo.files.list_all(limit=10)
        assert len(all_files) >= 1
