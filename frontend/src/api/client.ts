/**
 * ReSecure API Client
 * Strongly-typed HTTP client communicating with FastAPI backend or falling back to mock fixtures.
 */

import {
  ChunkHealthItem,
  ForensicReportResponse,
  HealthCheckResponse,
  JobStatusResponse,
  ProtectFileRequest,
  ProtectFileResponse,
  RecoverFileRequest,
  RecoverFileResponse,
  RecoveryJobRecord,
  RepairFormatRequest,
  RepairFormatResponse,
  ValidateFormatRequest,
  ValidateFormatResponse,
  VerifyFileRequest,
  VerifyFileResponse,
} from '../types/index';

import {
  createMockChunks,
  getMockForensicReport,
  mockAuditRecords,
  mockHealth,
  mockProtectResponse,
  mockRecoverResponse,
  mockRepairFormatResponse,
  mockSafeFailureResponse,
  mockValidateFormatResponse,
  mockVerifyResponse,
} from './mockData';

class ReSecureApiClient {
  private baseUrl: string;
  private forceMock: boolean = false;

  constructor(baseUrl: string = '/api') {
    this.baseUrl = baseUrl;
  }

  public setForceMock(force: boolean) {
    this.forceMock = force;
  }

  public isForcedMock(): boolean {
    return this.forceMock;
  }

  private async request<T>(
    endpoint: string,
    options: RequestInit = {},
    mockFallback: () => T
  ): Promise<T> {
    if (this.forceMock) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      return mockFallback();
    }

    try {
      const url = `${this.baseUrl}${endpoint}`;
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 6000);

      const response = await fetch(url, {
        ...options,
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          ...(options.headers || {}),
        },
      });

      clearTimeout(timeoutId);

      if (!response.ok) {
        // If HTTP 422 Safe Failure or 403 HMAC error, parse custom JSON
        const errJson = await response.json().catch(() => null);
        if (response.status === 422 && errJson?.error_code === 'ERR_EXCEEDS_REDUNDANCY') {
          throw { isSafeFailure: true, data: errJson };
        }
        if (response.status === 403) {
          throw { isHmacMismatch: true, data: errJson || { message: 'HMAC signature mismatch' } };
        }
        throw new Error(errJson?.message || `API error: ${response.status} ${response.statusText}`);
      }

      return (await response.json()) as T;
    } catch (err: unknown) {
      // Re-throw specific protocol errors
      if (err && typeof err === 'object' && ('isSafeFailure' in err || 'isHmacMismatch' in err)) {
        throw err;
      }
      console.warn(`[ReSecure API] Backend unreachable at ${endpoint}. Falling back to deterministic local mock engine.`);
      await new Promise((resolve) => setTimeout(resolve, 300));
      return mockFallback();
    }
  }

  public async getHealth(): Promise<HealthCheckResponse> {
    return this.request('/health', { method: 'GET' }, () => mockHealth);
  }

  public async protectFile(req: ProtectFileRequest): Promise<ProtectFileResponse> {
    return this.request(
      '/protect',
      {
        method: 'POST',
        body: JSON.stringify(req),
      },
      () => {
        const k = 16;
        const redundancy = req.redundancy_percentage || 20;
        const m = Math.max(1, Math.ceil((k * redundancy) / 100));
        return {
          ...mockProtectResponse,
          total_chunks_k: k,
          parity_chunks_m: m,
          redundancy_percentage: redundancy,
          manifest_path: `${req.source_path}.rsmanifest.json`,
          parity_path: `${req.source_path}.rsparity`,
        };
      }
    );
  }

  public async verifyFile(req: VerifyFileRequest): Promise<VerifyFileResponse> {
    return this.request(
      '/verify',
      {
        method: 'POST',
        body: JSON.stringify(req),
      },
      () => {
        // Deterministic mock behavior:
        // If key includes "bad" or "tamper", return HMAC failure
        if (req.secret_key === 'tampered' || req.secret_key === 'bad_key') {
          throw {
            isHmacMismatch: true,
            data: {
              error_code: 'ERR_HMAC_MISMATCH',
              message: 'Manifest HMAC signature mismatch: Unauthorized alteration detected. System failed closed.',
            },
          };
        }
        return {
          ...mockVerifyResponse,
          source_path: req.source_path,
        };
      }
    );
  }

  public async recoverFile(req: RecoverFileRequest & { simulateExceeds?: boolean }): Promise<RecoverFileResponse> {
    if (req.simulateExceeds) {
      throw { isSafeFailure: true, data: mockSafeFailureResponse };
    }

    return this.request(
      '/recover',
      {
        method: 'POST',
        body: JSON.stringify(req),
      },
      () => {
        return {
          ...mockRecoverResponse,
          source_path: req.source_path,
          output_path: req.output_path,
        };
      }
    );
  }

  public async validateFormat(req: ValidateFormatRequest): Promise<ValidateFormatResponse> {
    return this.request(
      '/validate-format',
      {
        method: 'POST',
        body: JSON.stringify(req),
      },
      () => {
        return {
          ...mockValidateFormatResponse,
          format: req.format || 'JSON',
        };
      }
    );
  }

  public async repairFormat(req: RepairFormatRequest): Promise<RepairFormatResponse> {
    return this.request(
      '/repair-format',
      {
        method: 'POST',
        body: JSON.stringify(req),
      },
      () => {
        return {
          ...mockRepairFormatResponse,
          source_path: req.source_path,
          output_path: req.output_path,
          format: req.format || 'JSON',
        };
      }
    );
  }

  public async getJobStatus(jobId: string): Promise<JobStatusResponse> {
    return this.request(
      `/jobs/${jobId}`,
      { method: 'GET' },
      () => ({
        job_id: jobId,
        job_type: 'RECOVER',
        status: 'COMPLETED',
        progress_pct: 100,
        source_path: 'enterprise_financials.csv',
        output_path: '/data/recovered/enterprise_financials.csv',
        verdict: 'VERIFIED_EXACT',
        duration_ms: 112,
        throughput_mb_s: 9.4,
        created_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
      })
    );
  }

  public async getForensicReport(jobId: string): Promise<ForensicReportResponse> {
    return this.request(
      `/reports/${jobId}`,
      { method: 'GET' },
      () => getMockForensicReport(jobId)
    );
  }

  public async listJobs(): Promise<RecoveryJobRecord[]> {
    return this.request(
      '/reports',
      { method: 'GET' },
      () => mockAuditRecords
    );
  }

  public generateSampleCorruptedChunks(damagedCount: number, totalK: number = 16): ChunkHealthItem[] {
    const corruptedIndices = Array.from({ length: damagedCount }, (_, i) => (i * 2 + 1) % totalK);
    return createMockChunks(totalK, corruptedIndices);
  }
}

export const apiClient = new ReSecureApiClient();
