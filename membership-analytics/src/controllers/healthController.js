const { testConnection } = require('../database/mysql');

async function health(req, res) {
  await testConnection();
  return res.json({
    ok: true,
    database: 'connected'
  });
}

module.exports = {
  health
};


