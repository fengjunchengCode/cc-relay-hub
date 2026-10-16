#!/usr/bin/env node
// 用法：node grok_ask.mjs <Bot中文名> <问题文件> <输出文件> [超时分钟=15]
// 薄包装：调用 cc-relay-hub grok-sync，复用共享锁和完整回复读取器。
import { spawn } from 'node:child_process';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { homedir } from 'node:os';

const [botName, questionPath, outputPath, minutesArg = '15'] = process.argv.slice(2);
const minutes = Number(minutesArg);
if (!botName || !questionPath || !outputPath || !Number.isFinite(minutes) || minutes <= 0) {
  console.error('用法：node grok_ask.mjs <Bot中文名> <问题文件> <输出文件> [超时分钟=15]');
  process.exit(2);
}

let result;
let exitCode;
try {
  if (!(await readFile(questionPath, 'utf8')).trim()) throw new Error('问题文件为空');
  const cli = process.env.CC_RELAY_HUB_BIN || join(homedir(), '.cc-connect/cc-relay-hub/bin/cc-relay-hub');
  const outcome = await new Promise((accept, reject) => {
    const child = spawn(cli, ['grok-sync', '--bot-name', botName,
      '--message-file', resolve(questionPath), '--timeout', String(Math.ceil(minutes * 60))],
    { stdio: ['ignore', 'pipe', 'inherit'] });
    let stdout = '';
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.on('error', reject);
    child.on('close', (code, signal) => accept({ code, signal, stdout }));
  });
  if (outcome.signal) throw new Error(`同步调用被信号 ${outcome.signal} 终止`);
  if (!/^(OK|TIMEOUT)\r?\n/.test(outcome.stdout)) throw new Error('同步调用未返回 OK/TIMEOUT 状态行');
  if (outcome.code !== 0 && outcome.stdout.startsWith('OK\n')) throw new Error('同步调用退出码与 OK 状态不一致');
  result = outcome.stdout;
  exitCode = outcome.code ?? 2;
} catch (error) {
  result = `TIMEOUT\n[ERROR] ${error.message}\n`;
  console.error(error.message);
  exitCode = 2;
}

await mkdir(dirname(resolve(outputPath)), { recursive: true });
await writeFile(outputPath, result, { encoding: 'utf8', mode: 0o600 });
process.stdout.write(result);
process.exitCode = exitCode;
