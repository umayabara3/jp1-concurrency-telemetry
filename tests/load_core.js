// jp1-telemetry.html の CORE ブロックだけを取り出して Node で読み込む
const fs = require('fs');
const path = require('path');
const vm = require('vm');
function loadCore() {
  const html = fs.readFileSync(path.join(__dirname, '..', 'jp1-telemetry.html'), 'utf8');
  const m = /\/\/ ==CORE-BEGIN==([\s\S]*?)\/\/ ==CORE-END==/.exec(html);
  if (!m) throw new Error('CORE block not found');
  const sandbox = { module: { exports: {} }, TextDecoder, TextEncoder };
  vm.runInNewContext(m[1], sandbox, { filename: 'jp1-telemetry.html#core' });
  return sandbox.module.exports;
}
module.exports = { loadCore };
