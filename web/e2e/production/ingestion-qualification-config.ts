import { existsSync, readdirSync, statSync } from 'node:fs';
import { resolve } from 'node:path';

import type { QualificationFormat } from '../../src/qualification/ingestion-qualification-report';
import {
  COMMIT_SHA_PATTERN,
  DIGEST_PATTERN,
  SAFE_ID_PATTERN,
  UUID_PATTERN,
} from '../../src/qualification/safe-metadata';

const FORMAT_SUFFIXES = {
  pdf: ['.pdf'],
  docx: ['.docx'],
  xlsx: ['.xlsx'],
  markdown: ['.md', '.markdown'],
  text: ['.txt'],
} as const;

export interface IngestionQualificationConfig {
  accessToken: string;
  authorizationGeneration: number;
  apiReleaseIdentity: string;
  corpusApprovalReference: string;
  commitSha: string;
  corpus: Readonly<Record<QualificationFormat, string>>;
  corpusDirectory: string;
  deploymentFingerprint: string;
  environmentAId: string;
  environmentBId: string;
  identityVersion: string;
  providerProfileIdentity: string;
  q16ReportPath: string;
  qualificationRunId: string;
  replacementPath: string;
  reportPath: string;
  trustedUploadAuthorityReference: string;
  webReleaseIdentity: string;
  workspaceAId: string;
  workspaceBId: string;
}

export function readIngestionQualificationConfig(
  environment: NodeJS.ProcessEnv,
): IngestionQualificationConfig {
  const required = (name: string): string => {
    const value = environment[name];
    if (!value) throw new Error(`qualification_configuration_missing:${name}`);
    return value;
  };
  const uuid = (name: string): string => {
    const value = required(name);
    if (!UUID_PATTERN.test(value)) throw new Error(`qualification_configuration_invalid:${name}`);
    return value;
  };
  const safeId = (name: string): string => {
    const value = required(name);
    if (!SAFE_ID_PATTERN.test(value)) throw new Error(`qualification_configuration_invalid:${name}`);
    return value;
  };

  const corpusDirectory = resolve(required('SPINE_INGESTION_QUALIFICATION_CORPUS_DIR'));
  if (!existsSync(corpusDirectory) || !statSync(corpusDirectory).isDirectory()) {
    throw new Error('qualification_configuration_invalid:SPINE_INGESTION_QUALIFICATION_CORPUS_DIR');
  }
  const names = readdirSync(corpusDirectory);
  const corpus = Object.fromEntries(
    Object.entries(FORMAT_SUFFIXES).map(([format, suffixes]) => {
      const matches = names.filter((name) => suffixes.some((suffix) => name.toLowerCase().endsWith(suffix)));
      if (matches.length !== 1) {
        throw new Error(`qualification_corpus_invalid:${format}`);
      }
      return [format, resolve(corpusDirectory, matches[0]!)];
    }),
  ) as Record<QualificationFormat, string>;
  const commitSha = required('SPINE_E2E_COMMIT_SHA');
  const authorizationGeneration = Number(required('SPINE_E2E_AUTHORIZATION_GENERATION'));
  if (!Number.isSafeInteger(authorizationGeneration) || authorizationGeneration < 1) {
    throw new Error('qualification_configuration_invalid:SPINE_E2E_AUTHORIZATION_GENERATION');
  }
  const replacementPath = resolve(corpusDirectory, 'lifecycle', 'replacement.txt');
  if (!existsSync(replacementPath) || !statSync(replacementPath).isFile()) {
    throw new Error('qualification_corpus_invalid:replacement');
  }
  const deploymentFingerprint = required('SPINE_E2E_DEPLOYMENT_FINGERPRINT');
  if (!COMMIT_SHA_PATTERN.test(commitSha)) throw new Error('qualification_configuration_invalid:SPINE_E2E_COMMIT_SHA');
  if (!DIGEST_PATTERN.test(deploymentFingerprint)) {
    throw new Error('qualification_configuration_invalid:SPINE_E2E_DEPLOYMENT_FINGERPRINT');
  }

  return Object.freeze({
    accessToken: required('SPINE_LOCAL_AUTH_BEARER_CREDENTIAL'),
    authorizationGeneration,
    apiReleaseIdentity: safeId('SPINE_RUNTIME_RELEASE_IDENTITY'),
    commitSha,
    corpus: Object.freeze(corpus),
    corpusApprovalReference: safeId('SPINE_INGESTION_QUALIFICATION_CORPUS_APPROVAL_REFERENCE'),
    corpusDirectory,
    deploymentFingerprint,
    environmentAId: uuid('SPINE_ENVIRONMENT_A_ID'),
    environmentBId: uuid('SPINE_ENVIRONMENT_B_ID'),
    identityVersion: safeId('SPINE_LOCAL_AUTH_CONFIGURATION_VERSION'),
    providerProfileIdentity: safeId('SPINE_E2E_PROVIDER_PROFILE_IDENTITY'),
    q16ReportPath: resolve(required('SPINE_INGESTION_Q16_REPORT_PATH')),
    qualificationRunId: safeId('SPINE_E2E_QUALIFICATION_RUN_ID'),
    replacementPath,
    reportPath: resolve(required('SPINE_INGESTION_QUALIFICATION_REPORT_PATH')),
    trustedUploadAuthorityReference: safeId('SPINE_E2E_TRUSTED_UPLOAD_AUTHORITY_REFERENCE'),
    webReleaseIdentity: safeId('SPINE_E2E_WEB_RELEASE_IDENTITY'),
    workspaceAId: uuid('SPINE_WORKSPACE_A_ID'),
    workspaceBId: uuid('SPINE_WORKSPACE_B_ID'),
  });
}
