const APP_ROOT = __dirname;

module.exports = {
  apps: [
    {
      // One process: gateway.py mounts every project backend
      // (/portal-frontend-code, /portal-frontend-code-2, /admin-dashboard,
      // /portal-dashboard and /pc-meetings) in a single ASGI app.
      name: 'psa-backend',
      script: 'python3',
      args: '-m uvicorn gateway:app --host 0.0.0.0 --port 6644',
      cwd: APP_ROOT,
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      // No env_file: gateway.py load_dotenv()s each project's own .env in order,
      // with override=True, because every backend reads DB_HOST/DB_USER at
      // import time but they point at different databases (and, for
      // admin-dashboard and portal-dashboard, different tables in one database).
      env: {
        // site-packages for installed deps + repo root so `gateway` resolves
        PYTHONPATH: `/usr/local/lib/python3.11/site-packages:${APP_ROOT}`,
      },
    },
  ],
};
