import { resolve } from 'node:path';

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SAFE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/;
const SHA_PATTERN = /^[a-f0-9]{40}$/;
const DIGEST_PATTERN = /^[a-f0-9]{64}$/;

export interface LiveQualificationConfig {
  accessToken: string;
  answerableQuestion: string;
  apiReleaseIdentity: string;
  cancelQuestion: string;
  commitSha: string;
  deploymentFingerprint: string;
  documentPath: string;
  environmentAId: string;
  environmentBId: string;
  identityVersion: string;
  providerProfileIdentity: string;
  qualificationRunId: string;
  reportPath: string;
  unanswerableQuestion: string;
  webReleaseIdentity: string;
  workspaceAId: string;
  workspaceBId: string;
}

export function readLiveQualificationConfig(environment: NodeJS.ProcessEnv): LiveQualificationConfig {
  const required = (name: string): string => {
    const value = environment[name];
    if (!value) throw new Error(`qualification_configuration_missing:${name}`);
    return value;
  };
  const safeId = (name: string): string => {
    const value = required(name);
    if (!SAFE_ID_PATTERN.test(value)) throw new Error(`qualification_configuration_invalid:${name}`);
    return value;
  };
  const uuid = (name: string): string => {
    const value = required(name);
    if (!UUID_PATTERN.test(value)) throw new Error(`qualification_configuration_invalid:${name}`);
    return value;
  };
  const question = (name: string): string => {
    const value = required(name);
    if (!value.trim() || value.length > 4_000) throw new Error(`qualification_configuration_invalid:${name}`);
    return value;
  };
  const documentPath = resolve(required('SPINE_E2E_DOCUMENT_PATH'));
  if (!/\.(?:docx|md|markdown|pdf|txt|xlsx)$/i.test(documentPath)) {
    throw new Error('qualification_configuration_invalid:SPINE_E2E_DOCUMENT_PATH');
  }
  const commitSha = required('SPINE_E2E_COMMIT_SHA');
  const deploymentFingerprint = required('SPINE_E2E_DEPLOYMENT_FINGERPRINT');
  if (!SHA_PATTERN.test(commitSha)) throw new Error('qualification_configuration_invalid:SPINE_E2E_COMMIT_SHA');
  if (!DIGEST_PATTERN.test(deploymentFingerprint)) {
    throw new Error('qualification_configuration_invalid:SPINE_E2E_DEPLOYMENT_FINGERPRINT');
  }

  return Object.freeze({
    accessToken: required('SPINE_LOCAL_AUTH_BEARER_CREDENTIAL'),
    answerableQuestion: question('SPINE_E2E_ANSWERABLE_QUESTION'),
    apiReleaseIdentity: safeId('SPINE_RUNTIME_RELEASE_IDENTITY'),
    cancelQuestion: question('SPINE_E2E_CANCEL_QUESTION'),
    commitSha,
    deploymentFingerprint,
    documentPath,
    environmentAId: uuid('SPINE_ENVIRONMENT_A_ID'),
    environmentBId: uuid('SPINE_ENVIRONMENT_B_ID'),
    identityVersion: safeId('SPINE_LOCAL_AUTH_CONFIGURATION_VERSION'),
    providerProfileIdentity: safeId('SPINE_E2E_PROVIDER_PROFILE_IDENTITY'),
    qualificationRunId: safeId('SPINE_E2E_QUALIFICATION_RUN_ID'),
    reportPath: resolve(environment.SPINE_E2E_REPORT_PATH ?? '../docs/qualification/issue-508-governed-qa-browser-report.json'),
    unanswerableQuestion: question('SPINE_E2E_UNANSWERABLE_QUESTION'),
    webReleaseIdentity: safeId('SPINE_E2E_WEB_RELEASE_IDENTITY'),
    workspaceAId: uuid('SPINE_WORKSPACE_A_ID'),
    workspaceBId: uuid('SPINE_WORKSPACE_B_ID'),
  });
}
