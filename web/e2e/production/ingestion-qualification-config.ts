import { existsSync, readdirSync, statSync } from 'node:fs';
import { resolve } from 'node:path';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SAFE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/;
const SHA_PATTERN = /^[a-f0-9]{40}$/;
const DIGEST_PATTERN = /^[a-f0-9]{64}$/;

const FORMAT_SUFFIXES = {
  pdf: ['.pdf'],
  docx: ['.docx'],
  xlsx: ['.xlsx'],
  markdown: ['.md', '.markdown'],
  text: ['.txt'],
} as const;

export type QualificationFormat = keyof typeof FORMAT_SUFFIXES;

export interface IngestionQualificationConfig {
  accessToken: string;
  authorizationGeneration: number;
  apiReleaseIdentity: string;
  commitSha: string;
  corpus: Readonly<Record<QualificationFormat, string>>;
  corpusDirectory: string;
  deploymentFingerprint: string;
  environmentAId: string;
  environmentBId: string;
  identityVersion: string;
  malformedPdfPath: string;
  providerProfileIdentity: string;
  q16ReportPath: string;
  qualificationRunId: string;
  reportPath: string;
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
  const malformedPdfPath = resolve(corpusDirectory, 'failures', 'malformed.pdf');
  if (!existsSync(malformedPdfPath) || !statSync(malformedPdfPath).isFile()) {
    throw new Error('qualification_corpus_invalid:malformed_pdf');
  }
  const deploymentFingerprint = required('SPINE_E2E_DEPLOYMENT_FINGERPRINT');
  if (!SHA_PATTERN.test(commitSha)) throw new Error('qualification_configuration_invalid:SPINE_E2E_COMMIT_SHA');
  if (!DIGEST_PATTERN.test(deploymentFingerprint)) {
    throw new Error('qualification_configuration_invalid:SPINE_E2E_DEPLOYMENT_FINGERPRINT');
  }

  return Object.freeze({
    accessToken: required('SPINE_E2E_ACCESS_TOKEN'),
    authorizationGeneration,
    apiReleaseIdentity: safeId('SPINE_E2E_API_RELEASE_IDENTITY'),
    commitSha,
    corpus: Object.freeze(corpus),
    corpusDirectory,
    deploymentFingerprint,
    environmentAId: uuid('SPINE_E2E_ENVIRONMENT_A_ID'),
    environmentBId: uuid('SPINE_E2E_ENVIRONMENT_B_ID'),
    identityVersion: safeId('SPINE_E2E_IDENTITY_VERSION'),
    malformedPdfPath,
    providerProfileIdentity: safeId('SPINE_E2E_PROVIDER_PROFILE_IDENTITY'),
    q16ReportPath: resolve(required('SPINE_INGESTION_Q16_REPORT_PATH')),
    qualificationRunId: safeId('SPINE_E2E_QUALIFICATION_RUN_ID'),
    reportPath: resolve(required('SPINE_INGESTION_QUALIFICATION_REPORT_PATH')),
    webReleaseIdentity: safeId('SPINE_E2E_WEB_RELEASE_IDENTITY'),
    workspaceAId: uuid('SPINE_E2E_WORKSPACE_A_ID'),
    workspaceBId: uuid('SPINE_E2E_WORKSPACE_B_ID'),
  });
}
