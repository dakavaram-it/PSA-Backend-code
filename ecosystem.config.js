const APP_ROOT = __dirname;

module.exports = {
  apps: [
    {
      // One process: gateway.py mounts both project backends
      // (/portal-frontend-code and /admin-dashboard) in a single ASGI app.
      name: 'psa-backend',
      script: 'python3',
      args: '-m uvicorn gateway:app --host 0.0.0.0 --port 6644',
      cwd: APP_ROOT,
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '500M',
      // No env_file: gateway.py load_dotenv()s each project's own .env in order,
      // with override=True, because the two backends read DB_HOST/DB_USER at
      // import time but point at different databases.
      env: {
        // site-packages for installed deps + repo root so `gateway` resolves
        PYTHONPATH: `/usr/local/lib/python3.11/site-packages:${APP_ROOT}`,
      },
    },
  ],
};
