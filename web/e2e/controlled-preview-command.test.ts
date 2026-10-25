import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { describe, expect, test } from 'vitest';

type CommandResult = {
  exitCode: number | null;
  output: string;
};

function runControlledQualification(previewPort: number): Promise<CommandResult> {
  return new Promise((resolve, reject) => {
    const child = spawn('corepack', ['pnpm', 'test:e2e:run'], {
      cwd: process.cwd(),
      env: {
        ...process.env,
        SPINE_CONTROLLED_PREVIEW_PORT: String(previewPort),
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    child.stdout.on('data', (chunk: Buffer) => {
      output += chunk.toString();
    });
    child.stderr.on('data', (chunk: Buffer) => {
      output += chunk.toString();
    });
    child.on('error', reject);
    child.on('close', (exitCode) => resolve({ exitCode, output }));
  });
}

describe('controlled browser qualification command', () => {
  test('test_unrelated_listener_stops_before_browser_execution', async () => {
    const requestHeaders: Array<Record<string, string | string[] | undefined>> = [];
    const unrelatedServer = createServer((request, response) => {
      requestHeaders.push(request.headers);
      response.writeHead(200, { 'content-type': 'text/plain' });
      response.end('unrelated service');
    });
    await new Promise<void>((resolve, reject) => {
      unrelatedServer.once('error', reject);
      unrelatedServer.listen(0, '127.0.0.1', resolve);
    });
    const address = unrelatedServer.address();
    if (!address || typeof address === 'string') {
      throw new Error('qualification_test_listener_address_unavailable');
    }

    try {
      const result = await runControlledQualification(address.port);

      expect(result.exitCode).toBe(1);
      expect(result.output).toContain('$ vite build');
      expect(result.output).toContain(`http://127.0.0.1:${address.port} is already used`);
      expect(result.output).not.toContain('Running 30 tests');
      expect(requestHeaders.length).toBeGreaterThanOrEqual(1);
      expect(requestHeaders.every((headers) => headers['sec-ch-ua'] === undefined)).toBe(true);
    } finally {
      await new Promise<void>((resolve, reject) => {
        unrelatedServer.close((error) => error ? reject(error) : resolve());
      });
    }
  }, 30_000);
});
