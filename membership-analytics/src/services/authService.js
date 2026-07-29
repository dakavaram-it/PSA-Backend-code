const userRepository = require('../repositories/userRepository');
const tokenService = require('./tokenService');

async function login(username, password) {
  const user = await userRepository.findByUsername(username);

  if (!user || user.password !== password) {
    const error = new Error('Invalid credentials');
    error.statusCode = 401;
    throw error;
  }

  return tokenService.issueTokens(user);
}

function refresh(refreshToken) {
  return tokenService.rotateRefreshToken(refreshToken);
}

function logout(refreshToken) {
  tokenService.revokeRefreshToken(refreshToken);
  return { message: 'Logged out successfully' };
}

function getCurrentUser(accessToken) {
  return tokenService.verifyAccessToken(accessToken);
}

module.exports = {
  login,
  refresh,
  logout,
  getCurrentUser
};


