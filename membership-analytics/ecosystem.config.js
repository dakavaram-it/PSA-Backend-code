const path = require('path');

const APP_ROOT = __dirname;
const PYTHON_APP = path.join(APP_ROOT, 'backend-python');

module.exports = {
  apps: [
    {
      name: 'membership-analytics',
      script: 'src/server.js',
      cwd: APP_ROOT,
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '300M',
      env_file: path.join(APP_ROOT, '.env'),
    },
    {
      name: 'membership-analytics-python',
      script: 'python3',
      args: '-m uvicorn app.main:app --host 0.0.0.0 --port 8001',
      cwd: PYTHON_APP,
      autorestart: true,
      watch: false,
      // raised from 400M: holds the in-memory voter matrix (~150MB) + constituency
      // cache + state-wide snapshot for the dynamic Panel & Volatility view and fast drill-downs.
      max_memory_restart: '1200M',
      env_file: path.join(PYTHON_APP, '.env'),
      env: {
        // site-packages for installed deps + app dir so `app.main` resolves
        PYTHONPATH: `/usr/local/lib/python3.11/site-packages:${PYTHON_APP}`,
      },
    },
  ],
};
