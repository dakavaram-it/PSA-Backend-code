let appConfig, app;

try {
  ({ appConfig } = require('./config/env'));
  app = require('./app');
} catch (err) {
  console.error('\n[STARTUP ERROR]', err.message);
  console.error('Make sure .env exists. Copy .env.example and fill in the values:\n');
  console.error('  cp .env.example .env\n');
  process.exit(1);
}

app.listen(appConfig.port, () => {
  console.log(`Server listening on port ${appConfig.port}`);
});


