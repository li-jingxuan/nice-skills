const { once } = require('node:events');
const path = require('node:path');

// Upstream logs can contain login keys and response bodies. Stdout is reserved for readiness.
console.log = console.info = console.warn = console.error = console.debug = () => {};

async function start() {
  const directory = process.argv[2];
  process.env.HOST = '127.0.0.1';
  process.env.PORT = '0';
  const { startService } = require(path.join(directory, 'server.js'));
  const { service } = await startService();
  if (!service.listening) await once(service, 'listening');

  const { address, port } = service.address();
  if (address !== '127.0.0.1') throw new Error('Unexpected API address');
  process.stdout.write(`${JSON.stringify({ port })}\n`);
}

// The parent owns the other end of this pipe, including during request handling.
process.stdin.on('end', () => process.exit(0));
process.stdin.on('error', () => process.exit(1));
process.stdin.resume();
start().catch(() => process.exit(1));
