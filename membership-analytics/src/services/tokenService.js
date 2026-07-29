const jwt = require('jsonwebtoken');
const { jwtConfig } = require('../config/env');

const refreshTokenStore = new Map();

function buildUserPayload(user) {
  return {
    username: user.username
  };
}

function generateAccessToken(payload) {
  return jwt.sign(payload, jwtConfig.accessSecret, {
    algorithm: 'HS512',
    expiresIn: jwtConfig.accessExpiresIn
  });
}

function generateRefreshToken(payload) {
  return jwt.sign(payload, jwtConfig.refreshSecret, {
    algorithm: 'HS512',
    expiresIn: jwtConfig.refreshExpiresIn
  });
}

function issueTokens(user) {
  const payload = buildUserPayload(user);
  const accessToken = generateAccessToken(payload);
  const refreshToken = generateRefreshToken(payload);

  refreshTokenStore.set(refreshToken, payload.username);

  return {
    accessToken,
    refreshToken,
    tokenType: 'Bearer',
    expiresIn: jwtConfig.accessExpiresIn
  };
}

function rotateRefreshToken(refreshToken) {
  if (!refreshTokenStore.has(refreshToken)) {
    const error = new Error('Invalid refresh token');
    error.statusCode = 401;
    throw error;
  }

  try {
    const decoded = jwt.verify(refreshToken, jwtConfig.refreshSecret, {
      algorithms: ['HS512']
    });
    const payload = { username: decoded.username };
    const accessToken = generateAccessToken(payload);
    const newRefreshToken = generateRefreshToken(payload);

    refreshTokenStore.delete(refreshToken);
    refreshTokenStore.set(newRefreshToken, payload.username);

    return {
      accessToken,
      refreshToken: newRefreshToken,
      tokenType: 'Bearer',
      expiresIn: jwtConfig.accessExpiresIn
    };
  } catch (error) {
    refreshTokenStore.delete(refreshToken);
    const tokenError = new Error('Invalid or expired refresh token');
    tokenError.statusCode = 401;
    throw tokenError;
  }
}

function revokeRefreshToken(refreshToken) {
  if (refreshToken) {
    refreshTokenStore.delete(refreshToken);
  }
}

function verifyAccessToken(token) {
  return jwt.verify(token, jwtConfig.accessSecret, {
    algorithms: ['HS512']
  });
}

module.exports = {
  issueTokens,
  rotateRefreshToken,
  revokeRefreshToken,
  verifyAccessToken
};


