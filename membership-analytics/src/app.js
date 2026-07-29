const express = require('express');
const path = require('path');
const cors = require('cors');
const authRoutes = require('./routes/authRoutes');
const healthRoutes = require('./routes/healthRoutes');
const nominatedRoutes = require('./routes/nominatedRoutes');
const committeeRoutes = require('./routes/committeeRoutes');
const casesRoutes = require('./routes/casesRoutes');
const pulseRoutes = require('./routes/pulseRoutes');
const meetingsRoutes = require('./routes/meetingsRoutes');
const sirDashboardRoutes = require('./routes/sirDashboardRoutes');
const authMiddleware = require('./middleware/authMiddleware');
const { notFoundHandler, errorHandler } = require('./middleware/errorMiddleware');
const { appConfig } = require('./config/env');

const app = express();
const frontendDistPath = path.join(process.cwd(), 'frontend', 'dist');
const frontendIndexPath = path.join(frontendDistPath, 'index.html');

app.use(cors({ origin: appConfig.corsOrigin, credentials: true }));
app.use(express.json());

// LB rewrites /cde/X → //X (replaces /cde with /), normalize before anything else
app.use((req, _res, next) => {
  req.url = req.url.replace(/\/+/g, '/');
  // Local/direct access keeps the /cde prefix (e.g. /cde/api/*, /cde/auth/*).
  // Normalize those app routes to backend-native paths so whitelist/proxy routes match.
  req.url = req.url.replace(/^\/cde(?=\/(api|auth|health)(\/|$))/, '');
  next();
});

// Serve static assets — no auth needed
app.use(express.static(frontendDistPath));
app.use('/cde', express.static(frontendDistPath));

// Auth middleware and API routes
app.use(authMiddleware);
app.use('/auth', authRoutes);
app.use('/', healthRoutes);

// NodeJS-BFF(src) → FastAPI nominated post APIs
// Final BFF URL example:
// http://localhost:3000/api/nominated-post/departments?enrollmentId=2&boardLevelId=2&locationValue=1
app.use('/api/nominated-post', nominatedRoutes);


app.use('/api/committee', committeeRoutes);

// NodeJS-BFF(src) → FastAPI cases dashboard APIs (/api/cases/* → /api/v1/cases/*)
app.use('/api/cases', casesRoutes);

// NodeJS-BFF(src) → FastAPI pulse trend APIs (/api/pulse/* → /api/v1/pulse/*)
app.use('/api/pulse', pulseRoutes);

// NodeJS-BFF(src) → FastAPI Meetings dashboard APIs (/api/meetings/* → /api/v1/meetings/*)
app.use('/api/meetings', meetingsRoutes);

// NodeJS-BFF(src) → FastAPI SIR Dashboard APIs (/api/sir-dashboard/* → /api/v1/sir-dashboard/*)
app.use('/api/sir-dashboard', sirDashboardRoutes);

// SPA catch-all
app.get('*', (_req, res) => res.sendFile(frontendIndexPath));

app.use(notFoundHandler);
app.use(errorHandler);

module.exports = app;
