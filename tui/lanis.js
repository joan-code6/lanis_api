#!/usr/bin/env node
import { spawn } from 'child_process';

const args = process.argv.slice(2);
const child = spawn('npx', ['tsx', 'src/cli.tsx', ...args], {
  stdio: 'inherit',
  shell: true
});

child.on('exit', (code) => {
  process.exit(code || 0);
});