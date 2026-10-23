// 只检查 Git 索引中的待发布文件；不读取用户凭据或运行目录。
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
const files = execFileSync('git', ['ls-files', '-z'], { encoding: 'utf8' }).split('\0').filter(Boolean);
if (!files.length) throw new Error('没有已暂存的文件');
const blocked = /(^|\/)(private|runtime|logs|backups|node_modules|mobile-workspace|qa-artifacts|\.claude|\.codex|\.dsh|\.ssh)(\/|$)|(^|\/)(config\.json|\.env(?:\..*)?|\.DS_Store)$|\.(zip|tgz|secret|pem|key|log|jsonl|sqlite3?|db|png|jpe?g|webp|heic|pdf)$/i;
const patterns = [
  ['personal home path', /\/(?:Users|home)\/[A-Za-z0-9_.-]+\//],
  ['private LAN address', /\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b/],
  ['access token', /\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{24,})\b/],
  ['private key', /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/],
  ['personal email', /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/],
];
const failures = [];
for (const file of files) {
  const st = fs.lstatSync(file);
  if (blocked.test(file) || !st.isFile() || st.size > 1024 * 1024) {
    failures.push(`${file}: forbidden runtime file, link or oversized file`);
    continue;
  }
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    for (const [label, re] of patterns) if (re.test(line)) failures.push(`${file}:${i + 1}: ${label}`);
  });
}
if (failures.length) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else console.log(`发布检查通过：${files.length} 个源文件，未检出运行数据、个人主目录、邮箱或常见密钥格式。`);
