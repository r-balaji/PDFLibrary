import Fastify from 'fastify';
import sensible from '@fastify/sensible';
import { loadConfig } from './config.js';
import { registerChunksRoute } from './routes/chunks.js';
import { registerSplitsRoute } from './routes/splits.js';

const config = loadConfig();

const app = Fastify({
  logger: {
    level: config.logLevel,
    transport: process.env.NODE_ENV === 'production'
      ? undefined
      : { target: 'pino-pretty', options: { colorize: true } },
  },
  bodyLimit: 50 * 1024, // 50 KB — request bodies are tiny JSON (Ids + token); bytes move via SF REST
});

await app.register(sensible);

// Auth: every endpoint except /healthz requires the shared secret.
app.addHook('onRequest', async (req, reply) => {
  if (req.url === '/healthz' || req.url === '/') return;
  const header = req.headers.authorization ?? '';
  const expected = `Bearer ${config.apiKey}`;
  if (header !== expected) {
    reply.code(401).send({ error: 'Unauthorized' });
  }
});

app.get('/healthz', async () => ({ ok: true, name: 'pdf-lib-service', version: '0.1.0' }));
app.get('/', async () => ({ name: 'pdf-lib-service', endpoints: ['/v1/chunks', '/v1/splits', '/healthz'] }));

await registerChunksRoute(app, config);
await registerSplitsRoute(app, config);

try {
  await app.listen({ host: '0.0.0.0', port: config.port });
  app.log.info({ port: config.port }, 'pdf-lib-service started');
} catch (err) {
  app.log.error(err);
  process.exit(1);
}
