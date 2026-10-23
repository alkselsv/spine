import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { readIngestionQualificationConfig } from '../../e2e/production/ingestion-qualification-config';

describe('ingestion qualification configuration', () => {
  it('test_approved_corpus_resolves_five_formats_and_replacement_without_malformed_fixture', () => {
    const corpusDirectory = mkdtempSync(join(tmpdir(), 'spine-issue561-corpus-'));
    for (const name of ['source.pdf', 'source.docx', 'source.xlsx', 'source.md', 'source.txt']) {
      writeFileSync(join(corpusDirectory, name), 'synthetic qualification content');
    }
    mkdirSync(join(corpusDirectory, 'lifecycle'));
    writeFileSync(join(corpusDirectory, 'lifecycle', 'replacement.txt'), 'approved changed content');

    const config = readIngestionQualificationConfig({
      SPINE_E2E_ACCESS_TOKEN: 'secret-not-recorded',
      SPINE_E2E_API_RELEASE_IDENTITY: 'api-v1',
      SPINE_E2E_TRUSTED_UPLOAD_AUTHORITY_REFERENCE: 'authority-approval-17',
      SPINE_E2E_AUTHORIZATION_GENERATION: '17',
      SPINE_E2E_COMMIT_SHA: 'a'.repeat(40),
      SPINE_E2E_DEPLOYMENT_FINGERPRINT: 'b'.repeat(64),
      SPINE_E2E_ENVIRONMENT_A_ID: '10000000-0000-4000-8000-000000000001',
      SPINE_E2E_ENVIRONMENT_B_ID: '10000000-0000-4000-8000-000000000002',
      SPINE_E2E_IDENTITY_VERSION: 'identity-v1',
      SPINE_E2E_PROVIDER_PROFILE_IDENTITY: 'provider-local-8935297e3def5e',
      SPINE_E2E_QUALIFICATION_RUN_ID: 'issue561-run-1',
      SPINE_E2E_WEB_RELEASE_IDENTITY: 'web-v1',
      SPINE_E2E_WORKSPACE_A_ID: '20000000-0000-4000-8000-000000000001',
      SPINE_E2E_WORKSPACE_B_ID: '20000000-0000-4000-8000-000000000002',
      SPINE_INGESTION_Q16_REPORT_PATH: join(corpusDirectory, 'q16.json'),
      SPINE_INGESTION_QUALIFICATION_CORPUS_APPROVAL_REFERENCE: 'corpus-approval-42',
      SPINE_INGESTION_QUALIFICATION_CORPUS_DIR: corpusDirectory,
      SPINE_INGESTION_QUALIFICATION_REPORT_PATH: join(corpusDirectory, 'report.json'),
    });

    expect(config.replacementPath).toBe(join(corpusDirectory, 'lifecycle', 'replacement.txt'));
    expect(config.corpus.text).toBe(join(corpusDirectory, 'source.txt'));
    expect(config.corpusApprovalReference).toBe('corpus-approval-42');
    expect(config.trustedUploadAuthorityReference).toBe('authority-approval-17');
  });
});
