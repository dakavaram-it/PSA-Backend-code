const authService = require('../services/authService');

async function login(req, res) {
  const { username, password } = req.body || {};

  if (!username || !password) {
    return res.status(400).json({
      error: 'username and password are required'
    });
  }

  const tokens = await authService.login(username, password);
  return res.json(tokens);
}

async function refresh(req, res) {
  const { refreshToken } = req.body || {};

  if (!refreshToken) {
    return res.status(400).json({
      error: 'refreshToken is required'
    });
  }

  const tokens = authService.refresh(refreshToken);
  return res.json(tokens);
}

async function logout(req, res) {
  const { refreshToken } = req.body || {};
  const result = authService.logout(refreshToken);

  return res.json(result);
}

async function me(req, res) {
  return res.json({ user: req.user });
}

module.exports = {
  login,
  refresh,
  logout,
  me
};


