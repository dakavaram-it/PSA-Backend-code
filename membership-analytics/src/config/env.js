const dotenv = require('dotenv');

dotenv.config();

function requireEnv(name) {
  const value = process.env[name];

  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }

  return value;
}

const appConfig = {
  port: Number(process.env.PORT || 3000),
  corsOrigin: process.env.CORS_ORIGIN || '*',
};

const dbConfig = {
  host: requireEnv('DB_HOST'),
  port: Number(process.env.DB_PORT || 3306),
  user: requireEnv('DB_USER'),
  password: requireEnv('DB_PASSWORD'),
  database: process.env.DB_NAME || 'dakavara_pa',
  waitForConnections: true,
  connectionLimit: 10,
  queueLimit: 0
};

const jwtConfig = {
  accessSecret: requireEnv('JWT_ACCESS_SECRET'),
  refreshSecret: requireEnv('JWT_REFRESH_SECRET'),
  accessExpiresIn: process.env.JWT_ACCESS_EXPIRES_IN || '15m',
  refreshExpiresIn: process.env.JWT_REFRESH_EXPIRES_IN || '7d',
  validateSignature: process.env.JWT_VALIDATE_SIGNATURE !== 'false', // Default: true
  debugMode: process.env.JWT_DEBUG_MODE === 'true' // Default: false
};

module.exports = {
  appConfig,
  dbConfig,
  jwtConfig
};

