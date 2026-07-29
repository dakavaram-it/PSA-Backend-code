const jwt = require('jsonwebtoken');
const tokenService = require('../services/tokenService');
const { jwtConfig } = require('../config/env');

const whitelist = [
  { method: 'GET', path: '/' },
  { method: 'GET', path: '/health' },
  { method: 'POST', path: '/auth/login' },
  { method: 'POST', path: '/auth/refresh' },
  { method: 'POST', path: '/auth/logout' },
  // Local dev: allow all HTTP methods on nominated-post BFF routes without JWT
  { pathPrefix: '/api/nominated-post' },
  // Committee BFF routes (same local-dev posture as nominated-post)
  { pathPrefix: '/api/committee' },
  // Cases dashboard BFF routes (same local-dev posture as nominated-post)
  { pathPrefix: '/api/cases' },
  // Pulse trend BFF routes
  { pathPrefix: '/api/pulse' },
  // Meetings dashboard analytics (read-only, GET) — open like the other dashboards.
  { method: 'GET', pathPrefix: '/api/meetings' },
  // SIR dashboard analytics (voter verification + CUBS, read-only, GET) — open.
  { method: 'GET', pathPrefix: '/api/sir-dashboard' }
];

// function isWhitelisted(req) {
//   return whitelist.some((route) => {
//     return route.method === req.method && route.path === req.path;
//   });
// }
// testing purpose: whitelist all GET requests to /api/nominated-post/* without auth, to allow testing BFF → FastAPI without needing JWTs
function isWhitelisted(req) {
  return whitelist.some((route) => {
    if (route.pathPrefix) {
      if (!req.path.startsWith(route.pathPrefix)) return false;
      return !route.method || route.method === req.method;
    }

    return (
      route.method === req.method &&
      route.path === req.path
    );
  });
}

function authMiddleware(req, res, next) {
  if (isWhitelisted(req)) {
    return next();
  }

  const authorization = req.headers.authorization || '';
  const [scheme, token] = authorization.split(' ');

  // Debug logging (optional)
  if (jwtConfig.debugMode) {
    console.log('Authorization Header:', authorization);
    console.log('Scheme:', scheme);
    console.log('Token:', token ? `${token.substring(0, 20)}...` : 'none');
  }

  if (scheme !== 'Bearer' || !token) {
    if (jwtConfig.debugMode) {
      console.log('Auth failed: Missing or invalid bearer token');
    }
    return res.status(401).json({
      error: 'Missing or invalid bearer token'
    });
  }

  try {
    let payload;

    if (jwtConfig.validateSignature) {
      // Validate signature with HS512
      payload = tokenService.verifyAccessToken(token);
      
      if (jwtConfig.debugMode) {
        console.log('JWT VERIFIED with HS512:', JSON.stringify(payload, null, 2));
      }
    } else {
      // Decode WITHOUT signature validation
      const decoded = jwt.decode(token, { complete: true });
      payload = decoded?.payload;
      
      if (jwtConfig.debugMode) {
        console.log('JWT DECODED (NO SIGNATURE VALIDATION):');
        console.log('Header:', JSON.stringify(decoded?.header, null, 2));
        console.log('Payload:', JSON.stringify(payload, null, 2));
      }
    }
    
    // Attach decoded token to request
    req.user = payload;
    req.tokenPayload = payload;
    
    if (jwtConfig.debugMode) {
      console.log('Request Path:', req.method, req.path);
      console.log('User:', JSON.stringify(payload, null, 2));
    }
    
    return next();
  } catch (error) {
    if (jwtConfig.debugMode) {
      console.log('JWT Error:', error.name, error.message);
    }
    
    if (error.name === 'TokenExpiredError') {
      return res.status(401).json({
        error: 'Access token has expired'
      });
    }
    if (error.name === 'JsonWebTokenError') {
      return res.status(401).json({
        error: 'Invalid access token'
      });
    }
    return res.status(401).json({
      error: 'Invalid or expired access token'
    });
  }
}

module.exports = authMiddleware;


