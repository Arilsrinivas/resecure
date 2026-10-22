"""
CalmStacks ReSecure Application Tier - Recovery Service
System: ReSecure (MVP v1.0)
Module: resecure.backend.src.services.recovery_service
"""

from __future__ import annotations

import datetime
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..core.chunker import audit_file_chunks
from ..core.manifest import ManifestTamperedError, read_manifest_file, verify_manifest
from ..core.reedsolomon import (
    NonDestructiveViolationError,
    RedundancyExceededError,
    ReedSolomonCodec,
)
from ..db.connection import get_connection_manager
from ..db.models import RecoveryJobModel
from ..db.repository import ReSecureRepository


class RecoveryService:
    """Orchestrates Galois Field GF(2^8) Reed-Solomon file reconstruction and strict verification."""

    def __init__(self, repo: Optional[ReSecureRepository] = None) -> None:
        self.repo = repo or ReSecureRepository(get_connection_manager())

    def recover_file(
        self,
        source_path: Union[str, Path],
        manifest_path: Union[str, Path],
        parity_path: Union[str, Path],
        output_path: Union[str, Path],
        secret_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes exact file recovery:
        1. Validates HMAC of manifest.
        2. Asserts source_path != output_path.
        3. Identifies damaged chunk indices.
        4. If damage > parity: records safe failure and raises RedundancyExceededError.
        5. If damage <= parity: reconstructs file, verifies SHA-256 match, commits output, persists job.
        """
        start_time = time.perf_counter()
        src = Path(source_path).resolve()
        mf = Path(manifest_path).resolve()
        par = Path(parity_path).resolve()
        out = Path(output_path).resolve()

        if src == out:
            raise NonDestructiveViolationError(
                f"Non-Destructive Invariant Violation: output_path '{out}' must not equal source_path '{src}'"
            )

        if not mf.is_file():
            raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

        if not par.is_file():
            raise FileNotFoundError(f"Parity container file not found: {parity_path}")

        # 1. Manifest HMAC verification
        manifest_dict = read_manifest_file(mf)
        is_hmac_valid = verify_manifest(manifest_dict, secret_key=secret_key)
        if not is_hmac_valid:
            raise ManifestTamperedError(
                f"Security Alert: Manifest HMAC verification failed for '{mf}'."
            )

        meta = manifest_dict.get("metadata", {})
        original_size = meta.get("file_size_bytes", 0)
        ec = manifest_dict.get("erasure_coding", {})
        chunk_size = ec.get("chunk_size_bytes", 65536)
        integrity = manifest_dict.get("integrity", {})
        root_sha256 = integrity.get("root_sha256", "")
        manifest_chunks = integrity.get("chunks", [])

        # 2. Audit damaged chunk indices
        audit = audit_file_chunks(
            file_path=src,
            manifest_chunks=manifest_chunks,
            original_file_size=original_size,
            chunk_size=chunk_size,
        )
        damaged_indices = audit["corrupted_indices"]

        # 3. Read parity container header
        codec = ReedSolomonCodec(chunk_size=chunk_size)
        c_size, k_hdr, parity_chunks_m, _ = codec.read_parity_container(par)

        job_id = str(uuid.uuid4())
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # 4. Safe failure check
        if len(damaged_indices) > parity_chunks_m:
            deficit = len(damaged_indices) - parity_chunks_m
            duration_ms = int((time.perf_counter() - start_time) * 1000)

            # Persist unrecoverable job state
            failed_job = RecoveryJobModel(
                job_id=job_id,
                job_type="RECOVER",
                status="UNRECOVERABLE",
                corrupted_chunks_count=len(damaged_indices),
                recovered_chunks_count=0,
                original_sha256=root_sha256,
                error_code="ERR_EXCEEDS_REDUNDANCY",
                error_message=f"Damage ({len(damaged_indices)} chunks) exceeds parity shards ({parity_chunks_m} chunks).",
                started_at=now_iso,
                completed_at=now_iso,
                duration_ms=duration_ms,
                progress_pct=0.0,
                source_path=str(src),
                output_path=str(out),
                parity_consumed=0,
                verdict="UNRECOVERABLE_EXACT",
            )
            self.repo.jobs.create(failed_job)

            raise RedundancyExceededError(
                damaged_count=len(damaged_indices),
                parity_available=parity_chunks_m,
                deficit=deficit,
                damaged_indices=damaged_indices,
            )

        # 5. Execute exact recovery
        recovery_meta = codec.recover_file_to_path(
            source_path=src,
            manifest_chunks=manifest_chunks,
            parity_path=par,
            output_path=out,
            original_root_sha256=root_sha256,
            original_file_size=original_size,
            damaged_indices=damaged_indices,
        )

        duration_ms = max(1, int((time.perf_counter() - start_time) * 1000))
        size_mb = original_size / (1024 * 1024)
        throughput_mb_s = round(size_mb / (duration_ms / 1000.0), 2)

        # Persist completed recovery job
        completed_job = RecoveryJobModel(
            job_id=job_id,
            job_type="RECOVER",
            status="COMPLETED",
            corrupted_chunks_count=len(damaged_indices),
            recovered_chunks_count=len(damaged_indices),
            original_sha256=root_sha256,
            recovered_sha256=recovery_meta["recovered_root_sha256"],
            sha256_match=1,
            output_path=str(out),
            started_at=now_iso,
            completed_at=now_iso,
            duration_ms=duration_ms,
            progress_pct=100.0,
            source_path=str(src),
            parity_consumed=recovery_meta["parity_shards_consumed"],
            verdict="VERIFIED_EXACT",
        )
        self.repo.jobs.create(completed_job)

        return {
            "job_id": job_id,
            "source_path": str(src),
            "output_path": str(out),
            "original_root_sha256": root_sha256,
            "recovered_root_sha256": recovery_meta["recovered_root_sha256"],
            "hash_verified_exact": True,
            "verdict": "VERIFIED_EXACT",
            "parity_shards_consumed": recovery_meta["parity_shards_consumed"],
            "parity_shards_remaining": recovery_meta["parity_shards_remaining"],
            "duration_ms": duration_ms,
            "throughput_mb_s": throughput_mb_s,
        }
