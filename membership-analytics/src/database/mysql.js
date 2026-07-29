const mysql = require('mysql2/promise');
const { dbConfig } = require('../config/env');

const pool = mysql.createPool(dbConfig);

async function testConnection() {
  await pool.query('SELECT 1');
}

module.exports = {
  pool,
  testConnection
};


