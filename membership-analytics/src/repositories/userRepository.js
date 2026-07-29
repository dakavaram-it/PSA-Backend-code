const { pool } = require('../database/mysql');

async function findByUsername(username) {
  const [rows] = await pool.execute(
    'SELECT username, password FROM user WHERE username = ? LIMIT 1',
    [username]
  );

  return rows[0] || null;
}

module.exports = {
  findByUsername
};

